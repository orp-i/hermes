from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = Path(os.getenv("HERMES_UNIFIED_CONFIG", "/config/hermes.yaml"))

SECRET_FIELDS = {
    "auth.user_access_token",
    "auth.admin_token",
    "auth.secret_key",
    "llm.api_key",
    "search.exa_api_key",
    "search.firecrawl_api_key",
    "search.tavily_api_key",
}

#: Tokens generated on first run when the config file does not yet carry them.
BOOTSTRAP_TOKEN_FIELDS = ("auth.user_access_token", "auth.admin_token", "auth.secret_key")

FIELD_SPECS = [
    ("server.public_base_url", "公开访问地址", "浏览器访问 Hermes 的地址，填写用户实际打开的 URL：局域网 IP、公网域名或反向代理地址均可。", "text"),
    ("auth.user_access_token", "普通访问码", "普通用户登录码。首次启动时自动生成随机值，可在此修改；登录后设备仍需管理员审批。", "password"),
    ("auth.admin_token", "管理访问码", "管理员登录码。管理员可审批设备并查看本页。首次启动时自动生成随机值。", "password"),
    ("auth.secret_key", "会话签名密钥", "用于签名登录 Cookie。首次启动时自动生成；修改后所有已登录会话失效。", "password"),
    ("limits.max_active_sessions", "最大同时窗口", "限制同时活跃对话窗口数量，防止公网入口被占满。", "number"),
    ("limits.session_ttl_minutes", "窗口空闲超时", "活跃窗口超过该分钟数未刷新会过期。", "number"),
    ("limits.max_upload_mb", "单文件上传上限 MB", "限制上传文件大小。", "number"),
    ("llm.default_provider", "默认 Provider", "没有命中路由时使用的 provider ID，对应下方 providers 中的键。", "text"),
    ("llm.api_timeout_seconds", "LLM 超时秒数", "Web 层等待 Hermes Agent API 返回的最长时间。", "number"),
    ("models.routing_enabled", "启用 Provider 路由", "启用后 Web 层按任务类型选择 chat/code/plan/long_context 对应 provider。", "checkbox"),
    ("llm.endpoint_model_mapping.router", "Router Provider", "意图分类/轻量路由用 provider ID，留空则使用默认 provider。", "text"),
    ("llm.endpoint_model_mapping.chat", "Chat Provider", "普通对话和简单需求用 provider ID。", "text"),
    ("llm.endpoint_model_mapping.code", "Code Provider", "代码、报错、脚本修改用 provider ID。", "text"),
    ("llm.endpoint_model_mapping.plan", "Plan Provider", "复杂规划、架构、分析用 provider ID。", "text"),
    ("llm.endpoint_model_mapping.long_context", "Long Context Provider", "长文本、多文件、大文件分析用 provider ID。", "text"),
    ("llm.endpoint_model_mapping.multimodal", "Multimodal Provider", "图片、网页图片和多模态识别用 provider ID。", "text"),
    ("llm.endpoint_model_mapping.vision", "Vision Provider", "兼容别名：图片识别用 provider ID。", "text"),
    ("search.enabled", "启用网络搜索", "允许 Hermes 使用已配置的搜索/网页工具。", "checkbox"),
    ("search.backend", "搜索后端", "web_search 使用的后端，如 tavily、duckduckgo、tavily+duckduckgo。", "text"),
    ("search.tavily_max_rounds", "Tavily 搜索轮数", "单次 Agent 回合最多允许的 Tavily web_search 调用次数，防止反复搜索。", "number"),
    ("search.tavily_api_key", "Tavily API Key", "tavily 搜索后端的凭据。留空则只能使用 duckduckgo 后端。", "password"),
    ("search.exa_api_key", "Exa API Key", "用于 web search。可选，但建议配置。", "password"),
    ("search.firecrawl_api_key", "Firecrawl API Key", "用于网页抓取/提取。可选。", "password"),
    ("search.require_sources", "搜索结果要求来源", "用户要求搜索/最新信息时，提示 Hermes 返回来源。", "checkbox"),
    ("agent.max_iteration_review_provider", "迭代上限裁判 Provider", "Agent 达到迭代上限后用于判断继续或停止的 provider ID。留空则复用默认 provider。", "text"),
    ("agent.max_iteration_review_model", "迭代上限裁判模型", "裁判使用的模型名。留空则使用该 provider 自己的默认模型。", "text"),
    ("agent.max_iteration_review_extra_iters", "单次续跑迭代数", "裁判允许继续时最多追加的迭代次数。", "number"),
    ("agent.max_iteration_review_max_rounds", "最多续跑次数", "单次用户请求中最多允许几次迭代上限裁判续跑。", "number"),
]


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _default_config() -> dict[str, Any]:
    return {
        "server": {"public_base_url": "http://localhost:5020", "bind_ip": "0.0.0.0", "port": 5020},
        # Empty by default. bootstrap_config() generates random values on first start
        # and writes them to the config file; nothing usable ships in the repo.
        "auth": {"user_access_token": "", "admin_token": "", "secret_key": ""},
        "limits": {
            "max_active_sessions": 2,
            "max_sessions_per_user": 1,
            "session_ttl_minutes": 30,
            "max_upload_mb": 100,
            "max_files_per_session": 20,
        },
        "llm": {
            "provider": "openai-compatible",
            "base_url": "",
            "api_key": "",
            "default_model": "hermes-agent",
            "default_provider": "default",
            "api_timeout_seconds": 1200,
            # One empty slot so the config page has something to edit on first run.
            # Fill base_url, api_key and default_model there, or add more provider
            # IDs and point the route mapping below at them.
            "providers": {
                "default": {
                    "name": "Default",
                    "type": "custom",
                    "base_url": "",
                    "api_key": "",
                    "default_model": "",
                    "api_mode": "chat_completions",
                },
            },
            "endpoint_model_mapping": {
                "router": "default",
                "chat": "default",
                "code": "default",
                "plan": "default",
                "long_context": "default",
                "multimodal": "default",
                "vision": "default",
            },
        },
        "models": {"routing_enabled": False, "default": "hermes-agent", "simple": "", "code": "", "strong": "", "long_context": "", "router": ""},
        "search": {
            "enabled": True,
            # duckduckgo needs no credential, so the default backend works out of the box.
            # Switch to "tavily" or "tavily+duckduckgo" once a Tavily key is set below.
            "backend": "duckduckgo",
            "tavily_max_rounds": 4,
            "tavily_api_key": "",
            "exa_api_key": "",
            "firecrawl_api_key": "",
            "require_sources": True,
        },
        "agent": {
            "max_iteration_review_provider": "",
            "max_iteration_review_model": "",
            "max_iteration_review_extra_iters": 4,
            "max_iteration_review_max_rounds": 2,
        },
        "tools": {
            "allowed_commands": ["python", "python3", "git", "rg", "cat", "ls", "pwd", "jq", "node", "npm"],
            "allowed_upload_extensions": [
                ".txt", ".md", ".rst", ".log", ".csv", ".tsv",
                ".json", ".jsonl", ".yaml", ".yml", ".toml",
                ".xml", ".html", ".htm",
                ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
                ".odt", ".ods", ".odp", ".rtf", ".pdf",
                ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".svg",
                ".zip", ".gz", ".tar", ".tgz", ".7z",
                ".py", ".js", ".ts", ".ipynb",
            ],
        },
    }


def load_unified_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or DEFAULT_CONFIG_PATH
    default = _default_config()
    if not cfg_path.exists():
        return default
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return default
    return _deep_merge(default, data)


def save_unified_config(config: dict[str, Any], path: Path | None = None) -> None:
    cfg_path = path or DEFAULT_CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")


def get_path(config: dict[str, Any], dotted: str, default: Any = "") -> Any:
    current: Any = config
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return default
        current = current.get(part, default)
    return current


def set_path(config: dict[str, Any], dotted: str, value: Any) -> None:
    current = config
    parts = dotted.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def masked(value: Any) -> str:
    text = "" if value is None else str(value)
    if not text:
        return ""
    if len(text) <= 8:
        return "********"
    return f"{text[:3]}...{text[-3:]}"


def bootstrap_config(path: Path | None = None) -> dict[str, str]:
    """Create the config file on first start and fill in any missing access tokens.

    Returns the fields that were generated, keyed by dotted name, so the caller can
    print them once to the container log. An operator who has already set a value on
    the config page keeps it: only empty fields are filled.
    """
    cfg_path = path or DEFAULT_CONFIG_PATH
    existing: dict[str, Any] = {}
    if cfg_path.exists():
        loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            existing = loaded

    generated: dict[str, str] = {}
    for dotted in BOOTSTRAP_TOKEN_FIELDS:
        if str(get_path(existing, dotted, "") or "").strip():
            continue
        token = secrets.token_urlsafe(24)
        set_path(existing, dotted, token)
        generated[dotted] = token

    if generated or not cfg_path.exists():
        merged = _deep_merge(_default_config(), existing)
        for dotted in BOOTSTRAP_TOKEN_FIELDS:
            set_path(merged, dotted, get_path(existing, dotted, ""))
        try:
            save_unified_config(merged, cfg_path)
        except OSError:
            # A read-only mount is a deployment choice, not an error: the generated
            # tokens still apply to this process via the returned dict.
            return generated
    return generated
