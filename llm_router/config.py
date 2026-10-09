from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

import hermes_config

CONFIG_PATH = Path(os.getenv("HERMES_UNIFIED_CONFIG", "/config/hermes.yaml"))
ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*))?\}$")
ROUTE_ALIASES = {"router", "chat", "code", "plan", "long_context", "multimodal", "vision", "default"}


@dataclass(frozen=True)
class ProviderConfig:
    provider_id: str
    name: str
    base_url: str
    api_key: str
    model: str
    timeout: float


@dataclass(frozen=True)
class RouterConfig:
    providers: dict[str, dict[str, Any]]
    default_provider: str
    endpoint_mapping: dict[str, str]
    default_model: str
    timeout: float


class ConfigCache:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self._mtime: float | None = None
        self._config: RouterConfig | None = None

    def get(self) -> RouterConfig:
        mtime = self.path.stat().st_mtime if self.path.exists() else None
        if self._config is None or mtime != self._mtime:
            self._config = load_router_config(self.path)
            self._mtime = mtime
        return self._config

    def reload(self) -> RouterConfig:
        self._config = load_router_config(self.path)
        self._mtime = self.path.stat().st_mtime if self.path.exists() else None
        return self._config


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        match = ENV_PATTERN.fullmatch(value.strip())
        if match:
            name, fallback = match.groups()
            return os.getenv(name, fallback or "")
    return value


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def load_router_config(path: Path = CONFIG_PATH) -> RouterConfig:
    raw = _load_yaml(path)
    llm = _as_dict(raw.get("llm"))
    providers = _as_dict(llm.get("providers"))
    mapping = _as_dict(llm.get("endpoint_model_mapping"))
    timeout = float(llm.get("api_timeout_seconds") or os.getenv("HERMES_LLM_ROUTER_TIMEOUT", "300"))
    return RouterConfig(
        providers=providers,
        default_provider=str(llm.get("default_provider") or "default"),
        endpoint_mapping={str(k): str(v) for k, v in mapping.items() if v},
        default_model=str(llm.get("default_model") or "hermes-agent"),
        timeout=timeout,
    )


def resolve_provider(config: RouterConfig, requested_model: str) -> ProviderConfig:
    model = (requested_model or "").strip() or "default"
    route = model if model in ROUTE_ALIASES else "default"
    provider_id = config.endpoint_mapping.get(route) or config.default_provider
    provider = config.providers.get(provider_id)
    if not isinstance(provider, dict):
        available = ", ".join(sorted(config.providers)) or "(none)"
        raise ValueError(f"Unknown provider {provider_id!r}. Available providers: {available}")

    provider_model = (
        str(provider.get("default_model") or provider.get("model") or "").strip()
    )
    if model not in ROUTE_ALIASES and model and model != config.default_model:
        provider_model = model
    if not provider_model:
        provider_model = config.default_model

    base_url = str(_expand_env(provider.get("base_url") or "")).rstrip("/")
    if not base_url:
        raise ValueError(f"Provider {provider_id!r} has no base_url")

    api_key = str(_expand_env(provider.get("api_key") or ""))
    if not api_key:
        env_name = str(provider.get("api_key_env") or "").strip()
        api_key = os.getenv(env_name, "") if env_name else ""
    if not api_key:
        api_key = hermes_config.legacy_model_env(str(provider.get("provider") or provider_id).split("_", 1)[0], "api_key")
    if not api_key:
        api_key = hermes_config.legacy_model_env(provider_id.split("_", 1)[0], "api_key")

    return ProviderConfig(
        provider_id=provider_id,
        name=str(provider.get("name") or provider_id),
        base_url=base_url,
        api_key=api_key,
        model=provider_model,
        timeout=float(provider.get("timeout") or config.timeout),
    )
