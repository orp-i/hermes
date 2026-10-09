from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .unified_config import get_path, load_unified_config


BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class PortalConfig:
    app_name: str
    public_base_url: str
    database_path: Path
    workspace_path: Path
    secret_key: str
    access_token_hash: str
    admin_token_hash: str
    max_active_sessions: int
    max_sessions_per_user: int
    session_ttl_minutes: int
    max_upload_mb: int
    max_files_per_session: int
    allowed_upload_extensions: set[str]
    allowed_commands: set[str]
    hermes_api_base_url: str
    hermes_model: str
    hermes_api_timeout_seconds: int
    llm_default_provider: str
    llm_providers: dict
    endpoint_model_mapping: dict
    model_routing_enabled: bool
    model_default: str
    model_simple: str
    model_code: str
    model_strong: str
    model_long_context: str
    search_enabled: bool
    search_require_sources: bool
    device_cookie_name: str = "hermes_device"
    session_cookie_name: str = "hermes_session"


def _csv_env(name: str) -> set[str]:
    raw = os.getenv(name, "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def _sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


#: Stands in for an unset access code. No input hashes to this, so an empty
#: configured token denies every login instead of accepting an empty one.
UNSET_TOKEN_HASH = "unset"


def _token_hash(name: str, fallback: str) -> str:
    value = os.getenv(name, fallback).strip()
    if not value:
        return UNSET_TOKEN_HASH
    if len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower()):
        return value.lower()
    return _sha256(value)


def load_config() -> PortalConfig:
    unified = load_unified_config()
    secret_key = os.getenv("HERMES_SECRET_KEY", str(get_path(unified, "auth.secret_key", "")))
    if not secret_key:
        secret_file = os.getenv("HERMES_SECRET_FILE", "/data/secret_key")
        path = Path(secret_file)
        if path.exists():
            secret_key = path.read_text(encoding="utf-8").strip()
    if not secret_key:
        # Last resort for a container started without a writable config mount. Sessions
        # signed with this key stop validating on restart, which is the intended nudge.
        import secrets as _secrets

        secret_key = _secrets.token_urlsafe(24)

    return PortalConfig(
        app_name=os.getenv("HERMES_APP_NAME", "Hermes"),
        public_base_url=os.getenv("HERMES_PUBLIC_BASE_URL", str(get_path(unified, "server.public_base_url", "http://localhost:5020"))),
        database_path=Path(os.getenv("HERMES_DATABASE", "/data/hermes.db")),
        workspace_path=Path(os.getenv("HERMES_WORKSPACE", "/workspace")),
        secret_key=secret_key,
        access_token_hash=_token_hash("HERMES_ACCESS_TOKEN", str(get_path(unified, "auth.user_access_token", ""))),
        admin_token_hash=_token_hash("HERMES_ADMIN_TOKEN", str(get_path(unified, "auth.admin_token", ""))),
        max_active_sessions=int(os.getenv("HERMES_MAX_ACTIVE_SESSIONS", str(get_path(unified, "limits.max_active_sessions", 2)))),
        max_sessions_per_user=int(os.getenv("HERMES_MAX_SESSIONS_PER_USER", str(get_path(unified, "limits.max_sessions_per_user", 1)))),
        session_ttl_minutes=int(os.getenv("HERMES_SESSION_TTL_MINUTES", str(get_path(unified, "limits.session_ttl_minutes", 30)))),
        max_upload_mb=int(os.getenv("HERMES_MAX_UPLOAD_MB", str(get_path(unified, "limits.max_upload_mb", 100)))),
        max_files_per_session=int(os.getenv("HERMES_MAX_FILES_PER_SESSION", str(get_path(unified, "limits.max_files_per_session", 20)))),
        allowed_upload_extensions=_csv_env(
            "HERMES_ALLOWED_UPLOAD_EXTENSIONS"
        )
        or set(get_path(unified, "tools.allowed_upload_extensions", []))
        or {
            ".txt", ".md", ".rst", ".log", ".csv", ".tsv",
            ".json", ".jsonl", ".yaml", ".yml", ".toml",
            ".xml", ".html", ".htm",
            ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
            ".odt", ".ods", ".odp", ".rtf", ".pdf",
            ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".svg",
            ".zip", ".gz", ".tar", ".tgz", ".7z",
            ".py", ".js", ".ts", ".ipynb",
        },
        allowed_commands=_csv_env("HERMES_ALLOWED_COMMANDS")
        or set(get_path(unified, "tools.allowed_commands", []))
        or {"python", "python3", "git", "rg", "cat", "ls", "pwd", "jq", "node", "npm"},
        hermes_api_base_url=os.getenv("HERMES_API_BASE_URL", "http://127.0.0.1:8642/v1"),
        hermes_model=os.getenv("HERMES_MODEL", str(get_path(unified, "llm.default_model", "hermes-agent"))),
        hermes_api_timeout_seconds=int(os.getenv("HERMES_API_TIMEOUT_SECONDS", str(get_path(unified, "llm.api_timeout_seconds", 300)))),
        llm_default_provider=os.getenv("HERMES_LLM_DEFAULT_PROVIDER", str(get_path(unified, "llm.default_provider", "default"))),
        llm_providers=get_path(unified, "llm.providers", {}) if isinstance(get_path(unified, "llm.providers", {}), dict) else {},
        endpoint_model_mapping=get_path(unified, "llm.endpoint_model_mapping", {}) if isinstance(get_path(unified, "llm.endpoint_model_mapping", {}), dict) else {},
        model_routing_enabled=os.getenv("HERMES_MODEL_ROUTING", str(get_path(unified, "models.routing_enabled", False))).lower() in {"1", "true", "yes", "on"},
        model_default=os.getenv("HERMES_MODEL_DEFAULT", str(get_path(unified, "models.default", "hermes-agent"))),
        model_simple=os.getenv("HERMES_MODEL_SIMPLE", str(get_path(unified, "models.simple", ""))),
        model_code=os.getenv("HERMES_MODEL_CODE", str(get_path(unified, "models.code", ""))),
        model_strong=os.getenv("HERMES_MODEL_STRONG", str(get_path(unified, "models.strong", ""))),
        model_long_context=os.getenv("HERMES_MODEL_LONG_CONTEXT", str(get_path(unified, "models.long_context", ""))),
        search_enabled=os.getenv("HERMES_SEARCH_ENABLED", str(get_path(unified, "search.enabled", True))).lower() in {"1", "true", "yes", "on"},
        search_require_sources=os.getenv("HERMES_SEARCH_REQUIRE_SOURCES", str(get_path(unified, "search.require_sources", True))).lower() in {"1", "true", "yes", "on"},
    )


def load_json_file(path: Path, fallback: list[dict]) -> list[dict]:
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))
