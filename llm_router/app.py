from __future__ import annotations

import json
import os
import hmac
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import ConfigCache, resolve_provider


ROUTER_TOKEN = os.getenv("HERMES_LLM_ROUTER_TOKEN", "")
cache = ConfigCache()
app = FastAPI(title="hermes-llm-router", version="0.1.0")


def _check_auth(authorization: str | None) -> None:
    if not ROUTER_TOKEN:
        return
    expected = f"Bearer {ROUTER_TOKEN}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Invalid router token")


def _provider_headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _upstream_url(base_url: str) -> str:
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": "hermes_llm_router_error"}},
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
def list_models(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _check_auth(authorization)
    config = cache.get()
    data = []
    for provider_id, provider in sorted(config.providers.items()):
        if not isinstance(provider, dict):
            continue
        model = str(provider.get("default_model") or provider.get("model") or config.default_model)
        data.append(
            {
                "id": provider_id,
                "object": "model",
                "owned_by": provider_id,
                "root": model,
            }
        )
    return {"object": "list", "data": data}


@app.post("/admin/reload")
def reload_config(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _check_auth(authorization)
    config = cache.reload()
    return {
        "status": "ok",
        "default_provider": config.default_provider,
        "providers": sorted(config.providers),
        "endpoint_model_mapping": config.endpoint_mapping,
    }


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse | StreamingResponse:
    _check_auth(authorization)
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return _error(400, "Request body must be JSON")
    if not isinstance(payload, dict):
        return _error(400, "Request body must be a JSON object")

    try:
        provider = resolve_provider(cache.get(), str(payload.get("model") or "default"))
    except ValueError as exc:
        return _error(400, str(exc))

    upstream_payload = dict(payload)
    upstream_payload["model"] = provider.model
    url = _upstream_url(provider.base_url)
    headers = _provider_headers(provider.api_key)
    timeout = httpx.Timeout(provider.timeout, connect=30.0)

    if upstream_payload.get("stream"):
        return StreamingResponse(
            _stream_upstream(url, headers, upstream_payload, timeout),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Hermes-Provider": provider.provider_id,
                "X-Hermes-Model": provider.model,
            },
        )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=upstream_payload)
    except httpx.HTTPError as exc:
        return _error(502, f"Upstream provider {provider.provider_id!r} failed: {exc}")

    return JSONResponse(
        status_code=response.status_code,
        content=_json_or_error(response),
        headers={
            "X-Hermes-Provider": provider.provider_id,
            "X-Hermes-Model": provider.model,
        },
    )


async def _stream_upstream(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: httpx.Timeout,
):
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code >= 400:
                    text = await response.aread()
                    message = text.decode("utf-8", errors="replace")[:1000]
                    yield _sse_error(response.status_code, message)
                    return
                async for chunk in response.aiter_bytes():
                    if chunk:
                        yield chunk
    except httpx.HTTPError as exc:
        yield _sse_error(502, str(exc))


def _sse_error(status_code: int, message: str) -> bytes:
    payload = {
        "error": {
            "message": f"Upstream stream failed ({status_code}): {message}",
            "type": "hermes_llm_router_error",
        }
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _json_or_error(response: httpx.Response) -> Any:
    try:
        return response.json()
    except json.JSONDecodeError:
        return {
            "error": {
                "message": response.text[:1000],
                "type": "upstream_non_json_response",
            }
        }
