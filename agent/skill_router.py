"""LLM-based skill router (no embeddings).

Pattern adapted from picoclaw's intent_router: ask a lightweight model to emit
a strict JSON object listing which skills to activate, fall back to keyword
matching if the LLM call fails or returns garbage.
"""
from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any

from .router import Provider

logger = logging.getLogger(__name__)

_CACHE_ENABLED = os.getenv("HERMES_SKILL_ROUTER_CACHE_ENABLED", "1") not in {"0", "false", "False"}
_CACHE_TTL_SEC = float(os.getenv("HERMES_SKILL_ROUTER_CACHE_TTL_SEC", "600"))
_CACHE_MAX = int(os.getenv("HERMES_SKILL_ROUTER_CACHE_SIZE", "256"))

_cache: "collections.OrderedDict[str, tuple[list[str], str, float]]" = collections.OrderedDict()
_cache_lock = threading.Lock()
_cache_stats = {"hit": 0, "miss": 0, "expired": 0}


def _catalog_signature(skills: list[dict]) -> str:
    parts = []
    for s in sorted(skills, key=lambda x: x.get("name") or ""):
        name = s.get("name") or ""
        if not name:
            continue
        desc = (s.get("description") or "").strip().replace("\n", " ")
        always = "1" if s.get("always") else "0"
        parts.append(f"{name}::{desc}::{always}")
    return "\n".join(parts)


def _cache_key(user_message: str, skills: list[dict]) -> str:
    blob = (user_message or "").strip() + "||" + _catalog_signature(skills)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_lookup(key: str) -> tuple[list[str], str] | None:
    if not _CACHE_ENABLED:
        return None
    now = time.time()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            _cache_stats["miss"] += 1
            return None
        activated, reasoning, expires_at = entry
        if expires_at < now:
            _cache.pop(key, None)
            _cache_stats["expired"] += 1
            return None
        _cache.move_to_end(key)
        _cache_stats["hit"] += 1
        return list(activated), reasoning


def _cache_store(key: str, activated: list[str], reasoning: str) -> None:
    if not _CACHE_ENABLED:
        return
    expires_at = time.time() + _CACHE_TTL_SEC
    with _cache_lock:
        _cache[key] = (list(activated), reasoning, expires_at)
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


def cache_stats() -> dict[str, int]:
    with _cache_lock:
        return {**_cache_stats, "size": len(_cache)}


def cache_clear() -> None:
    with _cache_lock:
        _cache.clear()


_ROUTER_SYSTEM = """You are a skill router. Given a user message and a catalog of skills, decide which skills (zero or more) should be activated.

Activate a skill ONLY if it materially helps with the user's request. Activating a skill injects its full body into the agent's system prompt — don't activate skills that are merely tangentially related.

You MUST respond with a single JSON object, no prose, no markdown fences:
{"activate": ["<skill_name>", ...], "reasoning": "<= 15 words"}

If no skill is a clear fit, return {"activate": [], "reasoning": "no specialized skill needed"}.
Skill names must match exactly. Do not invent skills.
"""


def _build_user_prompt(user_message: str, skills: list[dict]) -> str:
    if not skills:
        return f"User message:\n{user_message}\n\nSkill catalog: (empty)"
    lines = ["Skill catalog:"]
    for s in skills:
        name = s.get("name", "")
        desc = (s.get("description") or "").strip().replace("\n", " ")
        lines.append(f"- {name}: {desc}")
    lines.append("")
    lines.append("User message:")
    lines.append(user_message)
    return "\n".join(lines)


def _parse_router_json(text: str, valid_names: set[str]) -> tuple[list[str], str]:
    """Best-effort JSON extraction. Returns (activate_names, reasoning)."""
    raw = (text or "").strip()
    if "```" in raw:
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
        if match:
            raw = match.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError(f"Router returned non-JSON: {raw[:200]}")
        data = json.loads(match.group())

    activate_raw = data.get("activate") or []
    if not isinstance(activate_raw, list):
        activate_raw = []
    activated = [str(n) for n in activate_raw if isinstance(n, (str, bytes)) and str(n) in valid_names]
    reasoning = str(data.get("reasoning", "")).strip()
    return activated, reasoning


def llm_select_skills(
    user_message: str,
    skills: list[dict],
    provider: Provider,
    *,
    max_tokens: int = 200,
    timeout_warn_ms: int = 4000,
) -> dict[str, Any]:
    """Ask the provider which skills to activate. Returns a dict with selected skills."""
    if not skills:
        return {"selected": [], "router": "none", "reasoning": "no skills configured"}

    valid_names = {s.get("name", "") for s in skills if s.get("name")}
    by_name = {s["name"]: s for s in skills if s.get("name")}

    cache_key = _cache_key(user_message, skills)
    cached = _cache_lookup(cache_key)
    if cached is not None:
        activated, reasoning = cached
        selected = [by_name[n] for n in activated if n in by_name]
        return {
            "selected": selected,
            "router": "cache",
            "reasoning": reasoning,
            "latency_ms": 0,
            "cached": True,
        }

    messages = [
        {"role": "system", "content": _ROUTER_SYSTEM},
        {"role": "user", "content": _build_user_prompt(user_message, skills)},
    ]

    t0 = time.time()
    try:
        kwargs: dict[str, Any] = {
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if getattr(provider, "name", "") == "gateway":
            kwargs["model"] = "router"
        # Disable tool advertisement for the router call — pure JSON classification.
        # Some OpenAI-compatible servers (DeepSeek included) accept response_format={"type":"json_object"}.
        try:
            resp = provider.complete(
                messages,
                tools=None,
                **kwargs,
                response_format={"type": "json_object"},
            )
        except Exception:
            resp = provider.complete(messages, tools=None, **kwargs)
        text = (resp.choices[0].message.content or "").strip()
        activated, reasoning = _parse_router_json(text, valid_names)
    except Exception as exc:
        logger.warning("LLM skill router failed, fallback to keyword: %s", exc)
        return _keyword_fallback(user_message, skills, error=str(exc))

    elapsed_ms = int((time.time() - t0) * 1000)
    if elapsed_ms > timeout_warn_ms:
        logger.warning("LLM skill router slow: %dms", elapsed_ms)

    _cache_store(cache_key, activated, reasoning)

    selected = [by_name[n] for n in activated if n in by_name]
    return {
        "selected": selected,
        "router": "llm",
        "reasoning": reasoning,
        "latency_ms": elapsed_ms,
        "cached": False,
    }


def _keyword_fallback(user_message: str, skills: list[dict], *, error: str | None = None) -> dict[str, Any]:
    text = user_message.lower()
    selected: list[dict] = []
    for s in skills:
        if s.get("always"):
            selected.append(s)
            continue
        triggers = s.get("triggers") or []
        if any(str(t).lower() in text for t in triggers):
            selected.append(s)
    out: dict[str, Any] = {"selected": selected, "router": "keyword", "reasoning": "llm unavailable"}
    if error:
        out["router_error"] = error
    return out
