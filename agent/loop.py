from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import hermes_config

from . import db, sandbox
from .context_compaction import compact_messages, estimate_messages_tokens
from .report_skill_optimizer import maybe_optimize_report_skill
from .router import Provider, get_provider
from .skill_router import llm_select_skills
from .skills_loader import load_skills, render_system_prompt
from .tool_executor import (
    PreparedToolCall,
    after_tool_call,
    before_tool_call,
    can_execute_parallel,
    parse_tool_call,
    tool_execution_mode,
)
from .tools import DISPATCH, TOOL_SCHEMAS, TOOL_SPECS

DEFAULT_SYSTEM_PROMPT = """\
You are the Hermes Agent, running inside a long-lived Docker sandbox container.
You can call tools to:
- run bash commands (state — installed pip packages, files, cwd within work/ — persists across calls)
- read and write files in the session workspace
- render Markdown reports into PDF and DOCX
- search the web

Workspace layout (inside the sandbox):
- /workspace/sessions/<session_id>/uploads/   # user-uploaded files (read)
- /workspace/sessions/<session_id>/work/      # your scratch / cwd by default
- /workspace/sessions/<session_id>/outputs/   # files written here become user-downloadable
- /workspace/sessions/<session_id>/logs/      # command logs
- /code (read-only)                           # the host hermes project, for "check current code" tasks

Rules:
- Always inspect the environment with ls / pwd before assuming paths.
- Put downloadable artifacts in outputs/. Intermediate scratch goes in work/.
- Refuse to write to /code; it is the host source mount.
- For bioinformatics tasks: the sandbox has biopython, pysam, samtools, bcftools, bwa, bowtie2, minimap2, seqkit, bedtools pre-installed.
- For final analysis reports: produce client-ready consulting deliverables, not theoretical essays. The default audience is a CRO consulting client unless the user says otherwise. Reports must be decision-oriented, evidence-backed, and operational: executive summary, client question, data/materials reviewed, methods actually used, key findings with numbers, business/scientific implications, recommended actions, risks/limitations, and next steps. Avoid generic textbook background unless it directly changes a recommendation. The completion standard is three user-downloadable files in outputs/: the source Markdown, a PDF, and a DOCX. After the first render_report call, perform at least one quality/layout revision: call review_report_layout on the rendered PDF, address multimodal feedback and any render_report quality warnings in the Markdown, then call render_report again. Perform a second review/revision/render pass only when warnings remain, the layout review requests another pass, or the report still looks client-unready.
- Reply in the language the user used.
- Your training data has a knowledge cutoff. For anything time-sensitive (news, releases, prices, sports, who-is-X-now, "latest"), DO NOT answer from memory — call web_search. When you do, include the current year in the query if relevant.
"""

AGENT_TZ_NAME = os.getenv("HERMES_AGENT_TZ", "Asia/Shanghai")
_TZ_OFFSETS = {"Asia/Shanghai": 8, "UTC": 0}


def _current_time_block() -> str:
    now_utc = datetime.now(timezone.utc)
    offset_hours = _TZ_OFFSETS.get(AGENT_TZ_NAME, 0)
    local = now_utc.astimezone(timezone(timedelta(hours=offset_hours)))
    weekday = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][local.weekday()]
    return (
        "Current time:\n"
        f"- {local.strftime('%Y-%m-%d %H:%M')} {AGENT_TZ_NAME} ({weekday})\n"
        f"- {now_utc.strftime('%Y-%m-%d %H:%M')} UTC\n"
        "When users say 'today', 'this week', 'recently', resolve them against the time above."
    )

MAX_ITERATIONS = int(os.getenv("HERMES_AGENT_MAX_ITERS", "16"))
MAX_ITERATION_REVIEW_EXTRA_ITERS = int(os.getenv("HERMES_MAX_ITERATION_REVIEW_EXTRA_ITERS", "4"))
MAX_ITERATION_REVIEW_MAX_ROUNDS = int(os.getenv("HERMES_MAX_ITERATION_REVIEW_MAX_ROUNDS", "2"))
AGENT_TIMEOUT_SEC = int(os.getenv("HERMES_AGENT_TIMEOUT_SEC", "1200"))
TOOL_RESULT_TRUNCATE = int(os.getenv("HERMES_TOOL_RESULT_TRUNCATE", "16000"))
LONG_CONTEXT_ROUTE_THRESHOLD = int(os.getenv("HERMES_LONG_CONTEXT_ROUTE_LEN", "12000"))
ROUTE_SELECTOR_TIMEOUT = float(os.getenv("HERMES_ROUTE_SELECTOR_TIMEOUT", "30"))
TAVILY_SEARCH_ROUND_LIMIT = int(os.getenv("HERMES_TAVILY_MAX_SEARCH_ROUNDS", "4"))
VALID_GATEWAY_ROUTES = {"chat", "code", "plan", "long_context", "multimodal"}
IMAGE_URL_RE = re.compile(r"https?://\S+\.(?:png|jpe?g|webp|gif)(?:\?\S*)?", re.IGNORECASE)
CONTEXT_COMPACTION_ENABLED = os.getenv("HERMES_CONTEXT_COMPACTION_ENABLED", "1") not in {"0", "false", "False"}
CONTEXT_SUMMARY_TIMEOUT = float(os.getenv("HERMES_CONTEXT_SUMMARY_TIMEOUT", "30"))

SENTINEL_TIMEOUT = "[hermes-timeout]"
SENTINEL_MAX_ITERS = "[hermes-max-iterations]"
SENTINEL_PROVIDER_FAIL = "[hermes-provider-failure]"


def _session_outputs_dir(session_id: str) -> Path:
    root = Path(os.getenv("HERMES_SANDBOX_WORKSPACE", "/workspace"))
    outputs = root / "sessions" / session_id / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    return outputs


def _write_recovery_output(session_id: str, label: str, content: str) -> None:
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_label = re.sub(r"[^A-Za-z0-9_.-]", "-", label).strip("-") or "recovery"
        path = _session_outputs_dir(session_id) / f"hermes-{safe_label}-{ts}.md"
        path.write_text(content.strip() + "\n", encoding="utf-8")
    except OSError:
        pass


def _format_tool_result(result: dict) -> str:
    parts = []
    if result.get("stdout"):
        parts.append(result["stdout"])
    if result.get("stderr"):
        parts.append(f"[stderr]\n{result['stderr']}")
    parts.append(f"[exit_code={result.get('exit_code', 0)} duration_ms={result.get('duration_ms', 0)}]")
    text = "\n".join(parts)
    if len(text) > TOOL_RESULT_TRUNCATE:
        text = text[:TOOL_RESULT_TRUNCATE] + f"\n...[truncated; full length {len(text)}]"
    return text


def _field(obj: Any, *names: str) -> Any:
    """Read standard or provider-specific fields from SDK models/dicts."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        for name in names:
            if name in obj:
                return obj.get(name)
        return None
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    extra = getattr(obj, "model_extra", None)
    if isinstance(extra, dict):
        for name in names:
            if extra.get(name) is not None:
                return extra.get(name)
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            data = dump(exclude_none=False)
        except TypeError:
            data = dump()
        if isinstance(data, dict):
            for name in names:
                if data.get(name) is not None:
                    return data.get(name)
    return None


def _accumulate_stream(
    stream_iter: Iterator[Any],
) -> Iterator[dict]:
    """Consume a provider stream and yield normalized events.

    Each event is a dict with keys describing what changed. The caller is
    expected to also use the final 'assistant_message' event to know the full
    text and tool_calls of this assistant turn.
    """
    accum_content_parts: list[str] = []
    accum_reasoning_parts: list[str] = []
    accum_tool_calls: dict[int, dict] = {}
    usage: dict | None = None

    for chunk in stream_iter:
        # Some providers emit a final usage-only chunk with no choices.
        chunk_usage = _field(chunk, "usage")
        if chunk_usage is not None:
            usage = {
                "prompt_tokens": _field(chunk_usage, "prompt_tokens"),
                "completion_tokens": _field(chunk_usage, "completion_tokens"),
            }

        choices = _field(chunk, "choices") or []
        if not choices:
            continue
        delta = _field(choices[0], "delta")
        if delta is None:
            continue

        reasoning_delta = _field(delta, "reasoning_content", "reasoning")
        if reasoning_delta:
            accum_reasoning_parts.append(reasoning_delta)

        content_delta = _field(delta, "content")
        if content_delta:
            accum_content_parts.append(content_delta)
            yield {"type": "assistant_chunk", "delta": content_delta}

        tc_deltas = _field(delta, "tool_calls") or []
        for tcd in tc_deltas:
            idx = _field(tcd, "index") or 0
            slot = accum_tool_calls.setdefault(
                idx,
                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
            )
            tcd_id = _field(tcd, "id")
            if tcd_id:
                slot["id"] = tcd_id
            tcd_fn = _field(tcd, "function")
            if tcd_fn is not None:
                fn_name = _field(tcd_fn, "name")
                fn_args = _field(tcd_fn, "arguments")
                if fn_name:
                    slot["function"]["name"] += fn_name
                if fn_args:
                    slot["function"]["arguments"] += fn_args
                    yield {
                        "type": "tool_args_chunk",
                        "tool_index": idx,
                        "delta": fn_args,
                    }

    content = "".join(accum_content_parts)
    reasoning_content = "".join(accum_reasoning_parts)
    tool_calls = [accum_tool_calls[i] for i in sorted(accum_tool_calls)] or None
    yield {
        "type": "assistant_message",
        "content": content,
        "reasoning_content": reasoning_content,
        "tool_calls": tool_calls,
        "usage": usage,
    }


def _last_assistant_content(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("content"):
            return str(msg["content"])
    return ""


def _message_text(messages: list[dict]) -> str:
    parts = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
    return "\n".join(parts)


def _normalise_history_messages(history_messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in history_messages or []:
        role = str(msg.get("role") or "").strip().lower()
        if role == "hermes":
            role = "assistant"
        if role not in {"user", "assistant"}:
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content.strip()
            if text:
                out.append({"role": role, "content": text})
        elif isinstance(content, list):
            out.append({"role": role, "content": content})
    return out


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated; full length {len(text)}]"


def _compact_messages_for_review(messages: list[dict], limit: int = 18) -> list[dict[str, str]]:
    compact: list[dict[str, str]] = []
    for msg in messages[-limit:]:
        role = str(msg.get("role") or "")
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = json.dumps(content, ensure_ascii=False)
        else:
            text = ""
        if msg.get("tool_calls"):
            text += "\nTool calls: " + json.dumps(msg.get("tool_calls"), ensure_ascii=False)[:2000]
        compact.append({"role": role, "content": _truncate_text(text, 2500)})
    return compact


def _parse_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end < start:
            return None
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _has_multimodal_content(messages: list[dict]) -> bool:
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") in {"image_url", "input_image"}:
                    return True
    return False


def _image_urls_from_text(text: str, limit: int = 4) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    in_images = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "images:":
            in_images = True
            continue
        if stripped.startswith("- "):
            url = stripped[2:].strip()
            if url.startswith(("http://", "https://")) and url not in seen:
                seen.add(url)
                urls.append(url)
                if len(urls) >= limit:
                    return urls
            if in_images:
                continue
        if in_images and stripped and not stripped.startswith("- "):
            in_images = False
    for match in IMAGE_URL_RE.finditer(text):
        url = match.group(0).rstrip(").,]")
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def _with_search_images_for_multimodal(messages: list[dict]) -> list[dict]:
    urls = _image_urls_from_text(_message_text(messages), limit=4)
    if not urls:
        return messages
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": "Use these search result images for visual recognition if they are relevant to the user's request.",
        }
    ]
    content.extend({"type": "image_url", "image_url": {"url": url}} for url in urls)
    return [*messages, {"role": "user", "content": content}]


def _route_from_router_response(text: str) -> str | None:
    data = _parse_json_object(text)
    if not data:
        return None
    route = str(data.get("route") or "").strip()
    if route == "vision":
        route = "multimodal"
    return route if route in VALID_GATEWAY_ROUTES else None


def _deterministic_route_for_text(text: str) -> str | None:
    lowered = text.lower()
    code_markers = (
        "traceback", "stack trace", "pytest", "docker", "docker compose", "git diff", "git status",
        "function", "class ", "import ", "def ", "typescript", "javascript", "python", "fastapi",
        "代码", "报错", "调试", "仓库", "实现", "重构", "测试", "脚本",
    )
    plan_markers = (
        "architecture", "design", "strategy", "tradeoff", "migration", "roadmap", "compare",
        "方案", "架构", "设计", "规划", "迁移", "对比", "权衡", "优化", "评估",
    )
    if any(marker in lowered for marker in code_markers):
        return "code"
    if len(text) >= 3000 or any(marker in lowered for marker in plan_markers):
        return "plan"
    return None


def _gateway_route_for_messages(provider: Provider, messages: list[dict]) -> str:
    if _has_multimodal_content(messages):
        return "multimodal"

    route_source_messages = [msg for msg in messages if msg.get("role") != "system"]
    text = _message_text(route_source_messages)
    if len(text) >= LONG_CONTEXT_ROUTE_THRESHOLD:
        return "long_context"
    deterministic = _deterministic_route_for_text(text)
    if deterministic:
        return deterministic

    prompt = (
        "Classify the next Hermes agent model route. Return only JSON like "
        '{"route":"chat","reason":"short reason"}. '
        "Allowed routes: chat for ordinary conversation and simple Q&A; code for programming, debugging, shell, "
        "data/file transformations, or repository work; plan for complex analysis, strategy, comparison, design, "
        "or multi-step reasoning; long_context for very long documents or many files; multimodal for image, screenshot, "
        "chart, OCR, visual inspection, or search results whose image URLs need visual recognition. "
        "Use semantic understanding of the conversation, not keyword matching."
    )
    sample = text[-8000:]
    route_messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": sample},
    ]
    try:
        resp = provider.complete(
            route_messages,
            tools=None,
            model="router",
            temperature=0,
            timeout=ROUTE_SELECTOR_TIMEOUT,
            response_format={"type": "json_object"},
        )
    except Exception:
        try:
            resp = provider.complete(
                route_messages,
                tools=None,
                model="router",
                temperature=0,
                timeout=ROUTE_SELECTOR_TIMEOUT,
            )
        except Exception:
            return "chat"
    choice = (getattr(resp, "choices", None) or [None])[0]
    message = getattr(choice, "message", None) if choice is not None else None
    route = _route_from_router_response(str(getattr(message, "content", "") or ""))
    return route or "chat"


def _max_iteration_review(messages: list[dict], *, iteration: int, review_round: int) -> dict[str, Any]:
    prompt = (
        "You are the Hermes max-iteration supervisor. The main agent reached its iteration budget. "
        "Decide whether it should continue briefly or stop with the best final/next-step answer. "
        "Continue only when there is concrete unfinished work that can likely complete in a few more tool calls. "
        "Stop when the agent is looping, repeatedly searching, waiting on unavailable information, or already has enough evidence. "
        "Return only JSON with keys: action ('continue' or 'final'), extra_iterations (integer 1-4), "
        "instruction (short instruction for the agent if continuing), final_answer (answer to user if stopping), reason."
    )
    review_messages = [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "max_iterations": MAX_ITERATIONS,
                    "current_iteration": iteration,
                    "review_round": review_round,
                    "recent_messages": _compact_messages_for_review(messages),
                },
                ensure_ascii=False,
            ),
        },
    ]
    # Config page first, then environment. Empty means reuse the agent's own provider,
    # so a deployment with a single configured model needs no extra credential.
    reviewer_id = os.getenv("HERMES_MAX_ITERATION_REVIEW_PROVIDER", "") or str(
        hermes_config.value("agent.max_iteration_review_provider", "")
    )
    reviewer = get_provider(reviewer_id or None)
    try:
        resp = reviewer.complete(
            review_messages,
            tools=None,
            temperature=0,
            response_format={"type": "json_object"},
        )
    except Exception:
        resp = reviewer.complete(review_messages, tools=None, temperature=0)
    choice = (getattr(resp, "choices", None) or [None])[0]
    message = getattr(choice, "message", None) if choice is not None else None
    data = _parse_json_object(str(getattr(message, "content", "") or ""))
    if not data:
        raise RuntimeError("max-iteration reviewer returned non-JSON output")
    action = str(data.get("action") or "final").strip().lower()
    data["action"] = "continue" if action == "continue" else "final"
    try:
        extra = int(data.get("extra_iterations") or 1)
    except (TypeError, ValueError):
        extra = 1
    data["extra_iterations"] = max(1, min(extra, MAX_ITERATION_REVIEW_EXTRA_ITERS))
    return data


def _uses_tavily_backend() -> bool:
    return "tavily" in os.getenv("HERMES_SEARCH_BACKEND", "tavily+duckduckgo").lower()


def stream_run(
    *,
    session_id: str,
    user_message: str,
    db_path: Path,
    skills_dir: Path,
    provider: Provider | None = None,
    extra_system: str | None = None,
    image_inputs: list[dict[str, str]] | None = None,
    history_messages: list[dict[str, Any]] | None = None,
) -> Iterator[dict]:
    """Run a user-turn through the agent, yielding events as work happens.

    Final event is always {"type": "done", "final": str}.
    """
    db.init(db_path)
    sandbox.ensure_session_dirs(session_id)
    yield {"type": "agent_start", "session_id": session_id}

    skills = load_skills(skills_dir)
    provider = provider or get_provider()

    routing = llm_select_skills(user_message, skills, provider)
    selected = routing["selected"]
    db.log_message(
        db_path,
        session_id,
        "system",
        content=(
            f"[skill-router={routing.get('router')}] "
            f"activated={[s['name'] for s in selected]} "
            f"reasoning={routing.get('reasoning', '')!r}"
        ),
    )
    yield {
        "type": "skill_router",
        "router": routing.get("router"),
        "activated": [s["name"] for s in selected],
        "reasoning": routing.get("reasoning", ""),
        "cached": bool(routing.get("cached")),
        "latency_ms": routing.get("latency_ms"),
    }

    base_prompt = DEFAULT_SYSTEM_PROMPT + "\n\n" + _current_time_block()
    if extra_system:
        base_prompt = base_prompt + "\n\n" + extra_system
    system = render_system_prompt(base_prompt, skills, selected)

    user_content: str | list[dict[str, Any]] = user_message
    if image_inputs:
        user_content = [{"type": "text", "text": user_message}]
        for image in image_inputs:
            url = image.get("url")
            if url:
                user_content.append({"type": "image_url", "image_url": {"url": url}})

    history = _normalise_history_messages(history_messages)
    messages: list[dict] = [{"role": "system", "content": system}, *history, {"role": "user", "content": user_content}]
    compact_meta = {
        "compacted": False,
        "tokens_before": estimate_messages_tokens(messages),
        "tokens_after": estimate_messages_tokens(messages),
    }
    if CONTEXT_COMPACTION_ENABLED:
        messages, compact_meta = compact_messages(
            messages,
            provider=provider,
            summary_timeout=CONTEXT_SUMMARY_TIMEOUT,
        )
    compact_summary = compact_meta.pop("_summary", None)
    db.log_context_compaction(db_path, session_id, compact_meta, summary=compact_summary)
    db.log_message(
        db_path,
        session_id,
        "system",
        content=f"context compacted={compact_meta.get('compacted')} meta={json.dumps(compact_meta, ensure_ascii=False)[:1000]}",
    )
    yield {"type": "context_compaction", **compact_meta}
    db.log_message(db_path, session_id, "user", content=user_message)
    yield {"type": "message_start", "role": "user", "content": user_message}
    yield {"type": "message_end", "role": "user", "content": user_message}

    deadline = time.time() + AGENT_TIMEOUT_SEC
    final_text: str | None = None
    tavily_search_rounds = 0
    gateway_route_cache: str | None = None
    current_route: str | None = None

    def _emit_timeout(iteration: int) -> dict:
        last = _last_assistant_content(messages)
        msg = (
            f"{SENTINEL_TIMEOUT} Agent reached overall wallclock limit "
            f"({AGENT_TIMEOUT_SEC}s) after {iteration} iteration(s)."
        )
        if last:
            msg += f"\n\nLast partial assistant output:\n{last[:1500]}"
        db.log_message(
            db_path, session_id, "system",
            content=f"timeout iteration={iteration}",
        )
        _write_recovery_output(
            session_id,
            "timeout",
            "# Hermes timeout recovery\n\n"
            f"- Timeout: {AGENT_TIMEOUT_SEC}s\n"
            f"- Iteration: {iteration}\n\n"
            f"{msg}",
        )
        return {"type": "timeout", "message": msg, "iteration": iteration}

    try:
        iteration = 0
        allowed_iterations = MAX_ITERATIONS
        review_round = 0
        while True:
            while iteration < allowed_iterations:
                if time.time() > deadline:
                    ev = _emit_timeout(iteration)
                    final_text = ev["message"]
                    yield ev
                    return

                yield {
                    "type": "iteration_start",
                    "iteration": iteration,
                    "deadline_remaining_s": max(0, int(deadline - time.time())),
                }
                yield {"type": "turn_start", "iteration": iteration}
                yield {"type": "message_start", "role": "assistant", "iteration": iteration}

                try:
                    stream_kwargs = {}
                    stream_messages = messages
                    if getattr(provider, "name", "") == "gateway":
                        route = None
                        used_cached_route = False
                        if (
                            gateway_route_cache
                            and not _has_multimodal_content(messages)
                            and len(_message_text(messages)) < LONG_CONTEXT_ROUTE_THRESHOLD
                        ):
                            route = gateway_route_cache
                            used_cached_route = True
                        if route is None:
                            route = _gateway_route_for_messages(provider, messages)
                            if route not in {"multimodal", "long_context"}:
                                gateway_route_cache = route
                        stream_kwargs["model"] = route
                        current_route = route
                        db.log_route(
                            db_path,
                            session_id,
                            route=route,
                            iteration=iteration,
                            cached=used_cached_route,
                            tokens_estimate=estimate_messages_tokens(messages),
                            metadata={"provider": getattr(provider, "name", "")},
                        )
                        yield {"type": "route_selected", "route": route, "cached": used_cached_route}
                        if route == "multimodal" and not _has_multimodal_content(messages):
                            stream_messages = _with_search_images_for_multimodal(messages)
                    stream_iter = provider.stream(stream_messages, tools=TOOL_SCHEMAS, **stream_kwargs)
                except Exception as exc:
                    err = f"{SENTINEL_PROVIDER_FAIL} Provider {getattr(provider, 'name', '?')!r} stream init failed: {exc}"
                    db.log_message(db_path, session_id, "system", content=err)
                    _write_recovery_output(session_id, "provider-error", f"# Hermes provider error\n\n{err}")
                    final_text = err
                    yield {"type": "provider_error", "message": err}
                    return

                assistant_message_event: dict | None = None
                try:
                    for ev in _accumulate_stream(stream_iter):
                        if ev["type"] == "assistant_message":
                            assistant_message_event = ev
                        elif ev["type"] == "assistant_chunk":
                            yield {
                                "type": "message_update",
                                "role": "assistant",
                                "delta": ev.get("delta", ""),
                                "iteration": iteration,
                            }
                        yield ev
                except Exception as exc:
                    err = f"{SENTINEL_PROVIDER_FAIL} Provider stream raised mid-flight: {exc}"
                    db.log_message(db_path, session_id, "system", content=err)
                    _write_recovery_output(session_id, "provider-error", f"# Hermes provider error\n\n{err}")
                    final_text = err
                    yield {"type": "provider_error", "message": err}
                    return

                if assistant_message_event is None:
                    # No content and no tool calls — empty stream; treat as terminal.
                    final_text = ""
                    yield {"type": "final", "content": ""}
                    return

                content = assistant_message_event.get("content") or ""
                reasoning_content = assistant_message_event.get("reasoning_content") or ""
                tool_calls = assistant_message_event.get("tool_calls")
                usage = assistant_message_event.get("usage") or {}
                yield {
                    "type": "message_end",
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                    "iteration": iteration,
                }

                assistant_msg: dict = {"role": "assistant", "content": content}
                if reasoning_content:
                    assistant_msg["reasoning_content"] = reasoning_content
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                messages.append(assistant_msg)
                db.log_message(
                    db_path, session_id, "assistant",
                    content=content or None,
                    tool_calls=tool_calls,
                    model=getattr(provider, "model", None),
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                )

                if not tool_calls:
                    final_text = content
                    yield {"type": "turn_end", "iteration": iteration, "tool_results": []}
                    yield {"type": "final", "content": content}
                    return

                pending_search_image_urls: list[str] = []
                prepared_tool_calls: list[PreparedToolCall] = []
                for index, tc in enumerate(tool_calls):
                    if time.time() > deadline:
                        ev = _emit_timeout(iteration)
                        final_text = ev["message"]
                        yield ev
                        return

                    tc_id, name, args = parse_tool_call(index, tc)

                    # Clamp bash timeout to remaining wallclock so a single tool
                    # can't blow past HERMES_AGENT_TIMEOUT_SEC.
                    remaining = max(1, int(deadline - time.time()))
                    if name == "bash":
                        requested = int(args.get("timeout") or 120)
                        args["timeout"] = min(requested, remaining)

                    pre_result = before_tool_call(name, args, session_id=session_id)
                    if name == "web_search" and _uses_tavily_backend() and pre_result is None:
                        if tavily_search_rounds >= TAVILY_SEARCH_ROUND_LIMIT:
                            pre_result = {
                                "stdout": "",
                                "stderr": (
                                    f"Tavily search round limit reached ({TAVILY_SEARCH_ROUND_LIMIT}). "
                                    "Do not issue more Tavily searches in this turn; use existing results, "
                                    "answer with source limitations, or ask the user for a narrower target."
                                ),
                                "exit_code": 2,
                                "duration_ms": 0,
                            }
                        else:
                            tavily_search_rounds += 1

                    call = PreparedToolCall(index=index, id=tc_id, name=name, args=args, pre_result=pre_result)
                    prepared_tool_calls.append(call)
                    yield {"type": "tool_call", "id": tc_id, "name": name, "args": args}
                    yield {
                        "type": "tool_execution_start",
                        "id": tc_id,
                        "name": name,
                        "args": args,
                        "execution_mode": tool_execution_mode(name),
                    }

                def _execute_one(call: PreparedToolCall) -> dict[str, Any]:
                    name = call.name
                    args = call.args
                    spec = TOOL_SPECS.get(name)
                    tool_start = time.time()
                    pre_result = call.pre_result
                    if pre_result is not None:
                        result = {
                            **pre_result,
                            "duration_ms": int(pre_result.get("duration_ms") or (time.time() - tool_start) * 1000),
                        }
                    else:
                        handler = DISPATCH.get(name)
                        if handler is None:
                            result = {
                                "stdout": "",
                                "stderr": f"Unknown tool: {name}",
                                "exit_code": 1,
                                "duration_ms": int((time.time() - tool_start) * 1000),
                            }
                        else:
                            try:
                                result = handler(args, session_id=session_id)
                            except Exception as exc:
                                result = {
                                    "stdout": "",
                                    "stderr": f"Tool {name} raised: {exc}",
                                    "exit_code": 1,
                                    "duration_ms": int((time.time() - tool_start) * 1000),
                                }
                    result = after_tool_call(name, args, result, session_id=session_id)
                    db.log_execution(
                        db_path, session_id, name, args,
                        stdout=result.get("stdout", ""),
                        stderr=result.get("stderr", ""),
                        exit_code=int(result.get("exit_code", 0)),
                        duration_ms=int(result.get("duration_ms", 0)),
                        tool_call_id=call.id,
                        route=current_route,
                        iteration=iteration,
                        execution_mode=tool_execution_mode(name),
                        readonly=spec.readonly if spec else None,
                        blocked_by_policy=pre_result is not None,
                        policy_reason=str(pre_result.get("stderr") or "") if pre_result else None,
                        timed_out=bool(result.get("timed_out", False)),
                    )
                    if name == "review_report_layout" and int(result.get("exit_code", 0)) == 0:
                        optimization = maybe_optimize_report_skill(db_path)
                        if optimization.get("triggered"):
                            db.log_message(
                                db_path,
                                session_id,
                                "system",
                                content=(
                                    "report-skill-auto-optimized "
                                    f"review_count={optimization.get('review_count')} "
                                    f"path={optimization.get('generated_skill_path')}"
                                ),
                            )
                    tool_text = _format_tool_result(result)
                    if result.get("timed_out"):
                        _write_recovery_output(
                            session_id,
                            f"{name}-tool-timeout",
                            "# Hermes tool timeout recovery\n\n"
                            f"- Tool: {name}\n"
                            f"- Tool call id: {call.id}\n"
                            f"- Duration ms: {result.get('duration_ms', 0)}\n\n"
                            "## Arguments\n\n"
                            f"```json\n{json.dumps(args, ensure_ascii=False, indent=2)}\n```\n\n"
                            "## Captured output\n\n"
                            f"{tool_text}",
                        )
                    return {
                        "index": call.index,
                        "id": call.id,
                        "name": call.name,
                        "args": call.args,
                        "pre_result": call.pre_result,
                        "result": result,
                        "tool_text": tool_text,
                    }

                can_parallel = can_execute_parallel(prepared_tool_calls)
                completed_tool_calls: list[dict[str, Any]] = []
                if can_parallel:
                    try:
                        configured_workers = int(os.getenv("HERMES_PARALLEL_TOOL_WORKERS", "4"))
                    except ValueError:
                        configured_workers = 4
                    configured_workers = max(1, configured_workers)
                    max_workers = min(len(prepared_tool_calls), configured_workers)
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        futures = [executor.submit(_execute_one, call) for call in prepared_tool_calls]
                        for future in as_completed(futures):
                            completed = future.result()
                            completed_tool_calls.append(completed)
                            result = completed["result"]
                            yield {
                                "type": "tool_execution_end",
                                "id": completed["id"],
                                "name": completed["name"],
                                "exit_code": int(result.get("exit_code", 0)),
                                "duration_ms": int(result.get("duration_ms", 0)),
                                "timed_out": bool(result.get("timed_out", False)),
                            }
                            yield {
                                "type": "tool_result",
                                "id": completed["id"],
                                "name": completed["name"],
                                "stdout": result.get("stdout", ""),
                                "stderr": result.get("stderr", ""),
                                "exit_code": int(result.get("exit_code", 0)),
                                "duration_ms": int(result.get("duration_ms", 0)),
                                "timed_out": bool(result.get("timed_out", False)),
                            }
                else:
                    for call in prepared_tool_calls:
                        completed = _execute_one(call)
                        completed_tool_calls.append(completed)
                        result = completed["result"]
                        yield {
                            "type": "tool_execution_end",
                            "id": completed["id"],
                            "name": completed["name"],
                            "exit_code": int(result.get("exit_code", 0)),
                            "duration_ms": int(result.get("duration_ms", 0)),
                            "timed_out": bool(result.get("timed_out", False)),
                        }
                        yield {
                            "type": "tool_result",
                            "id": completed["id"],
                            "name": completed["name"],
                            "stdout": result.get("stdout", ""),
                            "stderr": result.get("stderr", ""),
                            "exit_code": int(result.get("exit_code", 0)),
                            "duration_ms": int(result.get("duration_ms", 0)),
                            "timed_out": bool(result.get("timed_out", False)),
                        }

                ordered_tool_calls = sorted(completed_tool_calls, key=lambda item: int(item["index"]))
                for completed in ordered_tool_calls:
                    tool_text = completed["tool_text"]
                    messages.append({"role": "tool", "tool_call_id": completed["id"], "content": tool_text})
                    db.log_message(db_path, session_id, "tool", content=tool_text, tool_call_id=completed["id"])
                    if completed["name"] == "web_search":
                        image_urls = _image_urls_from_text(tool_text)
                        if image_urls:
                            pending_search_image_urls.extend(image_urls)

                yield {
                    "type": "turn_end",
                    "iteration": iteration,
                    "tool_results": [
                        {
                            "id": item["id"],
                            "name": item["name"],
                            "exit_code": int(item["result"].get("exit_code", 0)),
                            "duration_ms": int(item["result"].get("duration_ms", 0)),
                        }
                        for item in ordered_tool_calls
                    ],
                }

                if pending_search_image_urls:
                    unique_urls = list(dict.fromkeys(pending_search_image_urls))[:4]
                    lines = ["Search returned image URLs. Use them only if visual recognition is needed:"]
                    lines.extend(f"- {url}" for url in unique_urls)
                    messages.append({"role": "user", "content": "\n".join(lines)})

                iteration += 1

            last = _last_assistant_content(messages)
            if review_round < MAX_ITERATION_REVIEW_MAX_ROUNDS and time.time() <= deadline:
                try:
                    decision = _max_iteration_review(messages, iteration=iteration, review_round=review_round + 1)
                except Exception as exc:
                    decision = {
                        "action": "final",
                        "reason": f"max-iteration reviewer failed: {exc}",
                        "final_answer": "",
                    }
                db.log_message(
                    db_path,
                    session_id,
                    "system",
                    content=f"max-iteration-review round={review_round + 1} decision={json.dumps(decision, ensure_ascii=False)[:1000]}",
                )
                yield {
                    "type": "max_iters_review",
                    "iteration": iteration,
                    "review_round": review_round + 1,
                    "decision": decision,
                }
                if decision.get("action") == "continue":
                    review_round += 1
                    extra_iterations = int(decision.get("extra_iterations") or 1)
                    allowed_iterations += max(1, min(extra_iterations, MAX_ITERATION_REVIEW_EXTRA_ITERS))
                    instruction = str(decision.get("instruction") or "").strip()
                    if not instruction:
                        instruction = "Continue briefly, avoid repeating prior failed actions, and produce a final answer as soon as possible."
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Max-iteration supervisor allowed a short continuation. "
                                f"Instruction: {instruction}"
                            ),
                        }
                    )
                    continue
                final_answer = str(decision.get("final_answer") or "").strip()
                if not final_answer:
                    reason = str(decision.get("reason") or "supervisor recommended stopping").strip()
                    final_answer = reason
                    if last:
                        final_answer += f"\n\nLast partial assistant output:\n{last[:1500]}"
                final_text = final_answer
                yield {"type": "final", "content": final_answer}
                return

            msg = "Hermes reached the supervised continuation budget."
            if last:
                msg += f"\n\nLast partial assistant output:\n{last[:1500]}"
            db.log_message(db_path, session_id, "system", content=msg[:500])
            _write_recovery_output(session_id, "max-iterations", f"# Hermes max-iterations recovery\n\n{msg}")
            final_text = msg
            yield {"type": "final", "content": msg}
            return
    finally:
        yield {"type": "agent_end", "session_id": session_id, "final": final_text or ""}
        yield {"type": "done", "final": final_text or ""}


def run(
    *,
    session_id: str,
    user_message: str,
    db_path: Path,
    skills_dir: Path,
    provider: Provider | None = None,
    extra_system: str | None = None,
    image_inputs: list[dict[str, str]] | None = None,
    history_messages: list[dict[str, Any]] | None = None,
) -> str:
    """Non-streaming wrapper. Consumes stream_run and returns the final text."""
    final_text = ""
    for event in stream_run(
        session_id=session_id,
        user_message=user_message,
        db_path=db_path,
        skills_dir=skills_dir,
        provider=provider,
        extra_system=extra_system,
        image_inputs=image_inputs,
        history_messages=history_messages,
    ):
        kind = event.get("type")
        if kind == "final":
            final_text = event.get("content", "")
        elif kind in ("timeout", "max_iters", "provider_error"):
            final_text = event.get("message", "")
        elif kind == "done":
            return event.get("final", final_text)
    return final_text
