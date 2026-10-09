from __future__ import annotations

import os
import json
import multiprocessing as mp
import queue
from typing import Protocol

import hermes_config


class SearchBackend(Protocol):
    name: str

    def search(self, query: str, max_results: int = 5, **kwargs) -> list[dict]: ...


class DuckDuckGoBackend:
    name = "duckduckgo"

    def search(self, query: str, max_results: int = 5, **kwargs) -> list[dict]:
        from ddgs import DDGS

        with DDGS() as ddgs:
            results = ddgs.text(query, max_results=max_results, region="wt-wt")
        return [
            {
                "title": (r.get("title") or "").strip(),
                "url": (r.get("href") or r.get("url") or "").strip(),
                "snippet": (r.get("body") or "").strip(),
            }
            for r in results
        ]


class TavilyBackend:
    name = "tavily"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        search_depth: str | None = None,
        timeout: float | None = None,
    ):
        self.api_key = api_key or hermes_config.credential(
            "search.tavily_api_key", "TAVILY_API_KEY", legacy=("tavily", "api_key")
        )
        self.search_depth = search_depth or os.getenv("TAVILY_SEARCH_DEPTH", "basic")
        self.timeout = timeout or float(os.getenv("TAVILY_TIMEOUT", "20"))

    def search(self, query: str, max_results: int = 5, **kwargs) -> list[dict]:
        if not self.api_key:
            raise RuntimeError(
                "Missing Tavily API key: set it on the admin config page "
                "(search.tavily_api_key) or via the TAVILY_API_KEY environment variable"
            )

        import httpx

        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": kwargs.get("search_depth") or self.search_depth,
            "include_answer": bool(kwargs.get("include_answer", True)),
            "include_images": bool(kwargs.get("include_images", True)),
            "include_raw_content": bool(kwargs.get("include_raw_content", False)),
        }
        if kwargs.get("topic"):
            payload["topic"] = kwargs["topic"]
        if kwargs.get("time_range"):
            payload["time_range"] = kwargs["time_range"]
        if kwargs.get("include_domains"):
            payload["include_domains"] = kwargs["include_domains"]
        if kwargs.get("exclude_domains"):
            payload["exclude_domains"] = kwargs["exclude_domains"]

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post("https://api.tavily.com/search", json=payload)
            response.raise_for_status()
            data = response.json()

        results = []
        if data.get("answer"):
            results.append(
                {
                    "title": "Tavily answer",
                    "url": "",
                    "snippet": str(data.get("answer") or "").strip(),
                }
            )
        images = data.get("images") if isinstance(data.get("images"), list) else []
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            result_images = item.get("images") if isinstance(item.get("images"), list) else []
            results.append(
                {
                    "title": str(item.get("title") or "").strip(),
                    "url": str(item.get("url") or "").strip(),
                    "snippet": str(item.get("content") or item.get("snippet") or "").strip(),
                    "raw_content": str(item.get("raw_content") or "").strip(),
                    "images": result_images,
                }
            )
        if images:
            for i, url in enumerate(images[:max_results], 1):
                results.append({"title": f"Tavily image {i}", "url": str(url), "snippet": "", "images": [str(url)]})
        return results[: max_results + 1]


def _search_worker(out: mp.Queue, backend: SearchBackend, query: str, max_results: int, kwargs: dict) -> None:
    try:
        out.put({"ok": True, "results": backend.search(query, max_results=max_results, **kwargs)})
    except Exception as exc:
        out.put({"ok": False, "error": f"{backend.name}: {exc}"})


def _search_with_timeout(
    backend: SearchBackend,
    query: str,
    *,
    max_results: int,
    timeout: float,
    **kwargs,
) -> tuple[list[dict] | None, str | None]:
    out: mp.Queue = mp.Queue(maxsize=1)
    proc = mp.Process(target=_search_worker, args=(out, backend, query, max_results, kwargs))
    proc.start()
    proc.join(timeout=max(1.0, timeout))
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=1)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=1)
        return None, f"{backend.name}: timed out after {timeout:.1f}s"
    try:
        payload = out.get_nowait()
    except queue.Empty:
        return None, f"{backend.name}: returned no result payload"
    if payload.get("ok"):
        return list(payload.get("results") or []), None
    return None, str(payload.get("error") or f"{backend.name}: failed")


class MultiBackend:
    def __init__(self, name: str, backends: list[SearchBackend]):
        self.name = name
        self.backends = backends

    def search(self, query: str, max_results: int = 5, **kwargs) -> list[dict]:
        errors = []
        per_backend_timeout = float(
            kwargs.pop("per_backend_timeout", None)
            or os.getenv("HERMES_SEARCH_BACKEND_TIMEOUT_SEC", "8")
        )
        for backend in self.backends:
            results, error = _search_with_timeout(
                backend,
                query,
                max_results=max_results,
                timeout=per_backend_timeout,
                **kwargs,
            )
            if error:
                errors.append(error)
                continue
            if results:
                return results
        if errors:
            raise RuntimeError("; ".join(errors))
        return []


_FACTORIES = {
    "duckduckgo": DuckDuckGoBackend,
    "tavily": TavilyBackend,
    "tavily+duckduckgo": lambda: MultiBackend("tavily+duckduckgo", [TavilyBackend(), DuckDuckGoBackend()]),
    "tavily+ddg": lambda: MultiBackend("tavily+ddg", [TavilyBackend(), DuckDuckGoBackend()]),
    # Reserved for future:
    # "brave":    lambda: BraveBackend(api_key=os.getenv("BRAVE_SEARCH_API_KEY")),
    # "searxng":  lambda: SearXNGBackend(base_url=os.getenv("SEARXNG_BASE_URL")),
}


def get_backend(name: str | None = None) -> SearchBackend:
    name = name or os.getenv("HERMES_SEARCH_BACKEND", "") or str(
        hermes_config.value("search.backend", "duckduckgo")
    )
    factory = _FACTORIES.get(name)
    if not factory:
        raise ValueError(f"Unknown search backend {name!r}. Available: {', '.join(_FACTORIES)}")
    return factory()


def register_backend(name: str, factory) -> None:
    _FACTORIES[name] = factory
