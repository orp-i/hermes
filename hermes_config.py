"""Single read path for the operator-editable config file.

Everything an operator needs to set lives in one YAML file (``/config/hermes.yaml``
in the containers, ``./config/hermes.yaml`` on the host) and is editable from the
admin config page at ``/admin/config``. The web layer owns writing that file; the
agent, the tool backends and the LLM router read values back through this module,
so a fresh clone runs without ``.env`` and without ``model.env``.

Lookup order for a credential is deliberate: the config file first, because that
is what the page writes, then the environment variable, which lets a container
orchestrator inject a value without a mounted file, and only then the legacy
``model.env`` JSON, kept so existing deployments keep working.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

ENV_PLACEHOLDER = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*))?\}$")

#: Legacy JSON credential store, superseded by the config page. Read-only.
LEGACY_MODEL_ENV_PATHS = ("model.env", "/srv/hermes-access/model.env", "/config/model.env")

_cache: dict[str, Any] = {}
_cache_mtime: float | None = None


def config_path() -> Path:
    return Path(os.getenv("HERMES_UNIFIED_CONFIG", "/config/hermes.yaml"))


def expand_env(value: Any) -> Any:
    """Resolve a bare ``${VAR}`` / ``${VAR:-default}`` placeholder, pass anything else through."""
    if isinstance(value, str):
        match = ENV_PLACEHOLDER.fullmatch(value.strip())
        if match:
            name, fallback = match.groups()
            return os.getenv(name, fallback or "")
    return value


def load() -> dict[str, Any]:
    """Parsed config file, re-read whenever its mtime changes so page edits apply without a restart."""
    global _cache, _cache_mtime

    path = config_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _cache_mtime == mtime and _cache:
        return _cache

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return _cache
    if not isinstance(data, dict):
        return _cache

    _cache, _cache_mtime = data, mtime
    return data


def value(dotted: str, default: Any = "") -> Any:
    current: Any = load()
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return default
        current = current.get(part)
        if current is None:
            return default
    return expand_env(current)


def legacy_model_env(provider: str, key: str) -> str:
    """Value from the deprecated ``model.env`` JSON, or an empty string."""
    for path in LEGACY_MODEL_ENV_PATHS:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        section = data.get(provider)
        if isinstance(section, dict) and section.get(key):
            return str(section[key])
    return ""


def credential(dotted: str, *env_names: str, legacy: tuple[str, str] | None = None) -> str:
    """A secret resolved config page first, then environment, then legacy ``model.env``."""
    found = value(dotted, "")
    if found:
        return str(found)
    for name in env_names:
        if name and os.getenv(name):
            return os.environ[name]
    if legacy:
        return legacy_model_env(*legacy)
    return ""
