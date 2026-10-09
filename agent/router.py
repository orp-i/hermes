from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Protocol

import yaml
from openai import OpenAI

import hermes_config

logger = logging.getLogger(__name__)

_ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*))?\}$")
_CONFIG_PATH = Path(os.getenv("HERMES_UNIFIED_CONFIG", "/config/hermes.yaml"))


class Provider(Protocol):
    """A chat-completion provider that speaks the OpenAI tool-use protocol."""

    name: str
    model: str

    def complete(self, messages: list[dict], tools: list[dict] | None = None, **kwargs: Any): ...

    def stream(self, messages: list[dict], tools: list[dict] | None = None, **kwargs: Any): ...


class OpenAICompatibleProvider:
    """Generic provider for any OpenAI-compatible endpoint (DeepSeek, Qwen, GLM, Moonshot, Ollama, vLLM)."""

    def __init__(
        self,
        *,
        name: str,
        model: str,
        base_url: str,
        api_key_env: str,
        api_key: str | None = None,
        default_temperature: float = 0.2,
        timeout: float = 120.0,
    ):
        key = api_key or os.getenv(api_key_env)
        if not key:
            raise RuntimeError(
                f"Missing API key for provider {name!r}: set ${api_key_env}"
            )
        self.name = name
        self.model = model
        self.client = OpenAI(api_key=key, base_url=base_url, timeout=timeout)
        self.default_temperature = default_temperature

    def _build_params(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        kwargs.setdefault("temperature", self.default_temperature)
        params: dict[str, Any] = {"model": self.model, "messages": messages, **kwargs}
        if tools:
            params["tools"] = tools
            params.setdefault("tool_choice", "auto")
        return params

    def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ):
        params = self._build_params(messages, tools, kwargs)
        return self.client.chat.completions.create(**params)

    def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ):
        params = self._build_params(messages, tools, kwargs)
        params["stream"] = True
        # Ask the server to emit a final usage chunk if it supports it.
        params.setdefault("stream_options", {"include_usage": True})
        return self.client.chat.completions.create(**params)


def _provider_field(provider_id: str, field: str, default: str = "") -> str:
    """One field of a provider as the config page stores it under llm.providers."""
    return str(hermes_config.value(f"llm.providers.{provider_id}.{field}", default) or default)


def deepseek_chat(api_key: str | None = None) -> Provider:
    return OpenAICompatibleProvider(
        name="deepseek-chat",
        model=_provider_field("deepseek", "default_model", "deepseek-chat"),
        base_url=os.getenv("DEEPSEEK_BASE_URL", "")
        or _provider_field("deepseek", "base_url", "https://api.deepseek.com/v1"),
        api_key_env="DEEPSEEK_API_KEY",
        api_key=api_key
        or hermes_config.credential(
            "llm.providers.deepseek.api_key", "DEEPSEEK_API_KEY", legacy=("deepseek", "api_key")
        ),
    )


def deepseek_reasoner(api_key: str | None = None) -> Provider:
    return OpenAICompatibleProvider(
        name="deepseek-reasoner",
        model="deepseek-reasoner",
        base_url=os.getenv("DEEPSEEK_BASE_URL", "")
        or _provider_field("deepseek", "base_url", "https://api.deepseek.com/v1"),
        api_key_env="DEEPSEEK_API_KEY",
        api_key=api_key
        or hermes_config.credential(
            "llm.providers.deepseek.api_key", "DEEPSEEK_API_KEY", legacy=("deepseek", "api_key")
        ),
    )


def gateway(api_key: str | None = None) -> Provider:
    """The local OpenAI-compatible router, which resolves credentials from the config file itself."""
    return OpenAICompatibleProvider(
        name="gateway",
        model=os.getenv("HERMES_GATEWAY_MODEL", "chat"),
        base_url=os.getenv("HERMES_GATEWAY_BASE_URL", "http://llm-router:8642/v1"),
        api_key_env="HERMES_GATEWAY_API_KEY",
        api_key=api_key or os.getenv("HERMES_GATEWAY_API_KEY", "local-gateway"),
        timeout=float(os.getenv("HERMES_GATEWAY_TIMEOUT", "300")),
    )


def xiaomi_mimo_pro(api_key: str | None = None) -> Provider:
    return OpenAICompatibleProvider(
        name="xiaomi-mimo-pro",
        model=os.getenv("HERMES_MAX_ITERATION_REVIEW_MODEL", "")
        or _provider_field("xiaomi", "default_model", "mimo-v2.5-pro"),
        base_url=os.getenv("XIAOMI_BASE_URL", "")
        or _provider_field("xiaomi", "base_url", "https://api.xiaomimimo.com/v1"),
        api_key_env="XIAOMI_API_KEY",
        api_key=api_key
        or hermes_config.credential(
            "llm.providers.xiaomi.api_key", "XIAOMI_API_KEY", legacy=("xiaomi", "api_key")
        ),
        timeout=float(os.getenv("HERMES_MAX_ITERATION_REVIEW_TIMEOUT", "120")),
    )


def fallback_default() -> Provider:
    """Sequential fallback: deepseek-chat → deepseek-reasoner. Add qwen/glm here once keys are configured."""
    from .fallback import FallbackProvider

    return FallbackProvider("fallback-default", [deepseek_chat(), deepseek_reasoner()])


def tiered_default() -> Provider:
    """Tier routing: short/simple → chat, long/complex → reasoner. Each tier has its own fallback list."""
    from .fallback import FallbackProvider, TieredProvider

    chat_chain = FallbackProvider("chat-chain", [deepseek_chat(), deepseek_reasoner()])
    plan_chain = FallbackProvider("plan-chain", [deepseek_reasoner(), deepseek_chat()])
    return TieredProvider("tiered-default", {"chat": chat_chain, "plan": plan_chain})


_FACTORIES = {
    "gateway": gateway,
    "deepseek-chat": deepseek_chat,
    "deepseek-reasoner": deepseek_reasoner,
    "xiaomi-mimo-pro": xiaomi_mimo_pro,
    "fallback": fallback_default,
    "tiered": tiered_default,
}


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        match = _ENV_PATTERN.fullmatch(value.strip())
        if match:
            name, fallback = match.groups()
            return os.getenv(name, fallback or "")
    return value


_yaml_factories_cache: dict[str, Any] = {}
_yaml_cache_mtime: float | None = None


def _load_yaml_providers() -> dict[str, Any]:
    """Read hermes.yaml `llm.providers` and return factory closures keyed by provider id.

    Re-reads the file when its mtime changes so config edits take effect without restart.
    """
    global _yaml_cache_mtime, _yaml_factories_cache

    if not _CONFIG_PATH.exists():
        return {}
    try:
        mtime = _CONFIG_PATH.stat().st_mtime
    except OSError:
        return {}

    if _yaml_cache_mtime == mtime and _yaml_factories_cache:
        return _yaml_factories_cache

    try:
        raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Failed to read provider config from %s: %s", _CONFIG_PATH, exc)
        return _yaml_factories_cache

    llm_section = raw.get("llm") if isinstance(raw, dict) else None
    providers = (llm_section or {}).get("providers") if isinstance(llm_section, dict) else None
    if not isinstance(providers, dict):
        _yaml_cache_mtime = mtime
        _yaml_factories_cache = {}
        return {}

    factories: dict[str, Any] = {}
    default_timeout = float(_expand_env((llm_section or {}).get("api_timeout_seconds")) or 300)
    for provider_id, cfg in providers.items():
        if not isinstance(cfg, dict):
            continue
        base_url = str(_expand_env(cfg.get("base_url") or "")).rstrip("/")
        api_key = str(_expand_env(cfg.get("api_key") or ""))
        model = str(_expand_env(cfg.get("default_model") or "")) or provider_id
        name = str(cfg.get("name") or provider_id)
        timeout = float(_expand_env(cfg.get("timeout")) or default_timeout)
        if not base_url or not api_key:
            # Skip providers missing credentials/endpoint — they would crash at first call.
            logger.info("Skipping yaml provider %r: base_url or api_key empty", provider_id)
            continue
        factories[provider_id] = (
            lambda _name=name, _model=model, _base=base_url, _key=api_key, _t=timeout: OpenAICompatibleProvider(
                name=_name,
                model=_model,
                base_url=_base,
                api_key_env="",
                api_key=_key,
                timeout=_t,
            )
        )

    _yaml_cache_mtime = mtime
    _yaml_factories_cache = factories
    return factories


def list_providers() -> list[str]:
    """All provider names available via get_provider — built-in plus yaml-defined."""
    return sorted(set(_FACTORIES) | set(_load_yaml_providers()))


def get_provider(name: str | None = None) -> Provider:
    name = name or os.getenv("HERMES_AGENT_PROVIDER", "gateway")
    factory = _FACTORIES.get(name)
    if factory is None:
        yaml_factories = _load_yaml_providers()
        factory = yaml_factories.get(name)
    if factory is None:
        available = ", ".join(list_providers()) or "(none)"
        raise ValueError(f"Unknown provider {name!r}. Available: {available}")
    return factory()


def register_provider(name: str, factory) -> None:
    _FACTORIES[name] = factory
