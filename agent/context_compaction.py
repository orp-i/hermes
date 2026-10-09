from __future__ import annotations

import json
import os
from typing import Any


DEFAULT_MAX_CONTEXT_TOKENS = int(os.getenv("HERMES_CONTEXT_MAX_TOKENS", "48000"))
DEFAULT_RESERVE_TOKENS = int(os.getenv("HERMES_CONTEXT_RESERVE_TOKENS", "12000"))
DEFAULT_KEEP_RECENT_TOKENS = int(os.getenv("HERMES_CONTEXT_KEEP_RECENT_TOKENS", "16000"))
DEFAULT_MAX_SUMMARY_CHARS = int(os.getenv("HERMES_CONTEXT_SUMMARY_MAX_CHARS", "12000"))


def estimate_tokens_for_text(text: str) -> int:
    return max(1, (len(text or "") + 3) // 4)


def estimate_message_tokens(message: dict[str, Any]) -> int:
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") in {"image_url", "input_image"}:
                    parts.append("[image]")
                else:
                    parts.append(json.dumps(item, ensure_ascii=False)[:1000])
        text = "\n".join(parts)
    else:
        text = ""
    if message.get("tool_calls"):
        text += "\n" + json.dumps(message.get("tool_calls"), ensure_ascii=False)
    return estimate_tokens_for_text(text) + 6


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


def _message_text(message: dict[str, Any], limit: int = 1600) -> str:
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, dict) and item.get("type") in {"image_url", "input_image"}:
                parts.append("[image]")
        text = "\n".join(parts)
    else:
        text = ""
    text = " ".join(text.split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _summarize_deterministically(messages: list[dict[str, Any]], max_chars: int) -> str:
    lines = [
        "Earlier conversation was compacted to save context. Preserve these facts and decisions:",
        "",
    ]
    for i, message in enumerate(messages, 1):
        role = str(message.get("role") or "unknown")
        text = _message_text(message)
        if not text:
            continue
        lines.append(f"{i}. {role}: {text}")
        if len("\n".join(lines)) >= max_chars:
            lines.append("...[compaction summary truncated]")
            break
    return "\n".join(lines)[:max_chars]


def _summarize_with_provider(
    provider: Any,
    messages: list[dict[str, Any]],
    *,
    max_chars: int,
    timeout: float,
) -> str | None:
    transcript = []
    for message in messages:
        role = str(message.get("role") or "unknown")
        text = _message_text(message, limit=2200)
        if text:
            transcript.append(f"{role}: {text}")
    if not transcript:
        return None

    prompt = (
        "Summarize the earlier Hermes conversation for continuation. "
        "Keep user goals, constraints, files mentioned, decisions made, tool findings, errors, and pending tasks. "
        "Do not invent details. Use concise bullet points."
    )
    try:
        kwargs: dict[str, Any] = {
            "temperature": 0,
            "timeout": timeout,
        }
        if getattr(provider, "name", "") == "gateway":
            kwargs["model"] = "router"
        resp = provider.complete(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "\n\n".join(transcript)[: max_chars * 3]},
            ],
            tools=None,
            **kwargs,
        )
        choice = (getattr(resp, "choices", None) or [None])[0]
        message = getattr(choice, "message", None) if choice is not None else None
        summary = str(getattr(message, "content", "") or "").strip()
        return summary[:max_chars] if summary else None
    except Exception:
        return None


def compact_messages(
    messages: list[dict[str, Any]],
    *,
    provider: Any | None = None,
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
    reserve_tokens: int = DEFAULT_RESERVE_TOKENS,
    keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS,
    max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
    summary_timeout: float = 30.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Compact old messages when estimated context exceeds the usable budget.

    The first system message, if present, is always preserved. Recent messages are
    kept verbatim up to keep_recent_tokens. Older messages are replaced by one
    synthetic user message containing a continuation summary.
    """
    if not messages:
        return messages, {"compacted": False, "tokens_before": 0, "tokens_after": 0}

    tokens_before = estimate_messages_tokens(messages)
    usable_tokens = max(1, max_context_tokens - reserve_tokens)
    if tokens_before <= usable_tokens:
        return messages, {
            "compacted": False,
            "tokens_before": tokens_before,
            "tokens_after": tokens_before,
            "usable_tokens": usable_tokens,
        }

    system_messages: list[dict[str, Any]] = []
    body = messages
    if messages and messages[0].get("role") == "system":
        system_messages = [messages[0]]
        body = messages[1:]

    recent: list[dict[str, Any]] = []
    recent_tokens = 0
    for message in reversed(body):
        cost = estimate_message_tokens(message)
        if recent and recent_tokens + cost > keep_recent_tokens:
            break
        recent.append(message)
        recent_tokens += cost
    recent.reverse()

    old_count = max(0, len(body) - len(recent))
    old_messages = body[:old_count]
    if not old_messages:
        return messages, {
            "compacted": False,
            "tokens_before": tokens_before,
            "tokens_after": tokens_before,
            "usable_tokens": usable_tokens,
            "reason": "no old messages available",
        }

    system_tokens = estimate_messages_tokens(system_messages)
    summary_budget_tokens = max(256, usable_tokens - system_tokens - recent_tokens - 128)
    summary_char_limit = max(1024, min(max_summary_chars, summary_budget_tokens * 4))
    summary = None
    if provider is not None and os.getenv("HERMES_CONTEXT_LLM_SUMMARY", "1") not in {"0", "false", "False"}:
        summary = _summarize_with_provider(
            provider,
            old_messages,
            max_chars=summary_char_limit,
            timeout=summary_timeout,
        )
    if not summary:
        summary = _summarize_deterministically(old_messages, summary_char_limit)

    summary_message = {
        "role": "user",
        "content": (
            "[Hermes context compaction summary]\n"
            f"Compacted {old_count} earlier message(s). Continue the conversation using this summary:\n\n"
            f"{summary}"
        ),
    }
    compacted = [*system_messages, summary_message, *recent]
    tokens_after = estimate_messages_tokens(compacted)
    return compacted, {
        "compacted": True,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "usable_tokens": usable_tokens,
        "old_messages": old_count,
        "kept_recent_messages": len(recent),
        "summary_chars": len(summary),
        "_summary": summary,
    }
