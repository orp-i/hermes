"""Fallback and tiered provider wrappers.

FallbackProvider — try a list of providers in order; on retryable errors,
move to the next. AuthenticationError and PermissionDeniedError are *not*
retried because switching providers won't fix bad credentials.

TieredProvider — pick a primary by detected tier (chat / plan) before
applying fallback within that tier.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError
from openai import AuthenticationError, NotFoundError, PermissionDeniedError
from openai import RateLimitError

from .router import Provider

logger = logging.getLogger(__name__)

NON_RETRYABLE = (AuthenticationError, PermissionDeniedError)
RETRYABLE = (
    RateLimitError,
    APITimeoutError,
    APIConnectionError,
    NotFoundError,  # model not found → try next
    APIStatusError,  # 5xx and other server errors
)

PLAN_KEYWORDS = (
    # English
    "design", "architecture", "complex", "compare", "comparison",
    "deep analysis", "trade-off", "tradeoff", "reason about", "plan",
    "multi-step", "step by step", "analyze in detail",
    # Chinese
    "设计", "架构", "复杂", "比较", "对比", "权衡", "深度分析",
    "多步骤", "逐步", "详细分析", "推理", "规划",
)
PLAN_LENGTH_THRESHOLD = int(os.getenv("HERMES_TIERED_PLAN_LEN", "2000"))


class FallbackProvider:
    """Wrap a list of providers; try sequentially on retryable errors."""

    def __init__(self, name: str, providers: list[Provider]):
        if not providers:
            raise ValueError("FallbackProvider needs at least one provider")
        self.name = name
        self.providers = providers
        self.model = providers[0].model
        self.last_used: Provider | None = None

    def complete(self, messages: list[dict], tools: list[dict] | None = None, **kwargs: Any):
        last_exc: Exception | None = None
        for i, prov in enumerate(self.providers):
            try:
                resp = prov.complete(messages, tools=tools, **kwargs)
                if i > 0:
                    logger.warning(
                        "provider %r recovered task after %d failure(s)",
                        prov.name,
                        i,
                    )
                self.last_used = prov
                return resp
            except NON_RETRYABLE:
                raise
            except RETRYABLE as exc:
                logger.warning("provider %r failed (%s: %s), trying next",
                               prov.name, type(exc).__name__, str(exc)[:120])
                last_exc = exc
                continue
            except Exception as exc:
                logger.warning("provider %r raised unexpected (%s: %s), trying next",
                               prov.name, type(exc).__name__, str(exc)[:120])
                last_exc = exc
                continue
        raise last_exc or RuntimeError(f"FallbackProvider {self.name!r}: all providers failed")

    def stream(self, messages: list[dict], tools: list[dict] | None = None, **kwargs: Any):
        """Stream from the first provider that successfully starts a stream.

        We pull the first chunk eagerly to validate the connection — failures
        AFTER any chunk has been yielded are not recoverable (the agent has
        already begun consuming partial output).
        """
        last_exc: Exception | None = None
        for i, prov in enumerate(self.providers):
            try:
                stream = prov.stream(messages, tools=tools, **kwargs)
                iterator = iter(stream)
                # Validate connection by pulling the first chunk.
                first = next(iterator)
            except StopIteration:
                self.last_used = prov
                return iter(())
            except NON_RETRYABLE:
                raise
            except (RETRYABLE, Exception) as exc:
                logger.warning("provider %r stream failed to start (%s: %s), trying next",
                               prov.name, type(exc).__name__, str(exc)[:120])
                last_exc = exc
                continue
            if i > 0:
                logger.warning("provider %r recovered stream after %d failure(s)", prov.name, i)
            self.last_used = prov

            def _iter():
                yield first
                yield from iterator

            return _iter()
        raise last_exc or RuntimeError(f"FallbackProvider {self.name!r}: all providers failed")


class TieredProvider:
    """Pick a primary by simple tier detection, then apply per-tier fallback."""

    def __init__(self, name: str, tiers: dict[str, Provider]):
        if not tiers or "chat" not in tiers:
            raise ValueError("TieredProvider needs at least a 'chat' tier")
        self.name = name
        self.tiers = tiers
        self.model = "tiered"
        self.last_tier: str = "chat"

    @staticmethod
    def detect_tier(messages: list[dict]) -> str:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content") or ""
                if isinstance(content, list):
                    content = " ".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
                text = str(content)
                low = text.lower()
                if len(text) >= PLAN_LENGTH_THRESHOLD:
                    return "plan"
                if any(kw in low for kw in PLAN_KEYWORDS):
                    return "plan"
                return "chat"
        return "chat"

    def _pick(self, messages: list[dict]) -> Provider:
        tier = self.detect_tier(messages)
        if tier not in self.tiers:
            tier = "chat"
        self.last_tier = tier
        prov = self.tiers[tier]
        logger.info("TieredProvider routing to tier=%s provider=%s", tier, prov.name)
        return prov

    def complete(self, messages: list[dict], tools: list[dict] | None = None, **kwargs: Any):
        return self._pick(messages).complete(messages, tools=tools, **kwargs)

    def stream(self, messages: list[dict], tools: list[dict] | None = None, **kwargs: Any):
        return self._pick(messages).stream(messages, tools=tools, **kwargs)
