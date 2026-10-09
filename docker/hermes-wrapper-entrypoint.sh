#!/bin/bash
set -euo pipefail

export HERMES_HOME="${HERMES_HOME:-/opt/data}"
export HERMES_UNIFIED_CONFIG="${HERMES_UNIFIED_CONFIG:-/config/hermes.yaml}"
export HERMES_WORKSPACE="${HERMES_WORKSPACE:-/workspace}"

mkdir -p "$HERMES_HOME" "$HERMES_WORKSPACE" /data

# Load portal-relevant settings from the unified YAML config (auth, limits, search).
# Model provider settings are read dynamically by the llm-router service.
if [ -f "$HERMES_UNIFIED_CONFIG" ]; then
  eval "$(python - <<'PY'
import shlex
import os
from pathlib import Path

import yaml

path = Path(os.environ.get("HERMES_UNIFIED_CONFIG", "/config/hermes.yaml"))
cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def get(dotted, default=""):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part, default)
    return cur


mapping = {
    "HERMES_PUBLIC_BASE_URL": get("server.public_base_url"),
    "HERMES_ACCESS_TOKEN": get("auth.user_access_token"),
    "HERMES_ADMIN_TOKEN": get("auth.admin_token"),
    "HERMES_SECRET_KEY": get("auth.secret_key"),
    "HERMES_MAX_ACTIVE_SESSIONS": get("limits.max_active_sessions"),
    "HERMES_MAX_SESSIONS_PER_USER": get("limits.max_sessions_per_user"),
    "HERMES_SESSION_TTL_MINUTES": get("limits.session_ttl_minutes"),
    "HERMES_MAX_UPLOAD_MB": get("limits.max_upload_mb"),
    "HERMES_MAX_FILES_PER_SESSION": get("limits.max_files_per_session"),
    "HERMES_SEARCH_ENABLED": "1" if get("search.enabled", True) else "0",
    "HERMES_SEARCH_BACKEND": get("search.backend", "tavily+duckduckgo"),
    "HERMES_SEARCH_REQUIRE_SOURCES": "1" if get("search.require_sources", True) else "0",
    "HERMES_TAVILY_MAX_SEARCH_ROUNDS": get("search.tavily_max_rounds", 4),
    "HERMES_MAX_ITERATION_REVIEW_PROVIDER": get("agent.max_iteration_review_provider", "xiaomi-mimo-pro"),
    "HERMES_MAX_ITERATION_REVIEW_MODEL": get("agent.max_iteration_review_model", "mimo-v2.5-pro"),
    "HERMES_MAX_ITERATION_REVIEW_EXTRA_ITERS": get("agent.max_iteration_review_extra_iters", 4),
    "HERMES_MAX_ITERATION_REVIEW_MAX_ROUNDS": get("agent.max_iteration_review_max_rounds", 2),
}
exts = get("tools.allowed_upload_extensions", [])
if isinstance(exts, list) and exts:
    mapping["HERMES_ALLOWED_UPLOAD_EXTENSIONS"] = ",".join(str(x) for x in exts)

for key, value in mapping.items():
    if value is None or value == "":
        continue
    print(f"export {key}={shlex.quote(str(value))}")
PY
)"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
