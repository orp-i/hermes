# Hermes 框架实现状态（v0.3）

> 初版截至 2026-04-29；本版更新至 2026-05-17。基于 仓库根目录 目录下源码梳理，覆盖运行时拓扑、Agent 主循环、上下文压缩、工具执行、报告生成、Skill 自动优化、LLM Router、Sandbox/Runner 隔离与外部搜索能力。2026-05-17 补丁见第 9 节。

---

## 1. 运行时拓扑（Docker Compose 4 服务）

`docker-compose.yml` 定义四个长驻服务，组成 **A+B+D 隔离架构**：

| 服务 | 镜像/构建 | 端口 | 角色 |
|---|---|---|---|
| `hermes` | 主仓库 `Dockerfile` | 宿主 `0.0.0.0:5020 → 8000`（2026-05-17 起改为无代理模式，原 `<mesh-ip>:5020:8000`） | FastAPI Web/SSE 前端 + Agent 调度 |
| `hermes-runner` | `docker/runner.Dockerfile` | 仅 compose 网络 `:8650` | 持有 `/var/run/docker.sock`，对外暴露 `/exec`、`/ensure-session-dirs` |
| `hermes-sandbox` | `docker/sandbox.Dockerfile` | 无端口 | 长驻沙箱，`sleep infinity`，被 runner 通过 `docker exec` 注入命令 |
| `hermes-llm-router` | 主仓库镜像（共用） | compose 网络 `:8642` | OpenAI 兼容代理，统一上游模型配置 |

共享卷：`hermes-workspace`（runner + sandbox + hermes 三方共享 `/workspace`），`hermes-data`（SQLite），`hermes-home`，`./config:/config`。

### 网络模式：无代理（2026-05-17 起）

入流量不再绑死到 netbird (`wt0` / `<mesh-ip>`)。`docker-compose.yml` 把 hermes 端口发布到 `0.0.0.0:5020:8000`，LAN/公网/netbird 任意接口都可直连。`config/hermes.yaml` `server.bind_ip` = `0.0.0.0`，`public_base_url` 默认 LAN `http://<lan-ip>:5020`（按部署环境改）。

出流量明确禁用 HTTP 代理。所有四个服务的 `environment:` 都注入：

```yaml
HTTP_PROXY: ""
HTTPS_PROXY: ""
http_proxy: ""
https_proxy: ""
NO_PROXY: "*"
no_proxy: "*"
```

确保 LLM、search、文件传输都不会借道 `<mesh-proxy-ip>:8080`（netbird 内的 HTTP 代理）。默认路由 `<lan-gateway> dev wlo1` 把 egress 走 LAN/WiFi。

### 关键安全边界
- 只有 `runner` 持有 `docker.sock`；hermes/sandbox 都不接触。
- sandbox 没有任何对外端口，唯一入口是 runner 的 `docker exec`。
- runner 用 `HERMES_RUNNER_TOKEN`（`Authorization: Bearer …`）做调用方鉴权（`runner/app.py:48-53`，`hmac.compare_digest`）。
- runner 强制 workdir 必须在 `/workspace` 下（`runner/app.py:56-60`），session_id 必须匹配 `^[a-f0-9]{32}$`。
- Sandbox 内置 bioinformatics 工具链（samtools、bcftools、bwa、bowtie2、minimap2、bedtools、seqkit、biopython、pysam、pyfaidx 等）。

---

## 2. Agent 主循环（`agent/loop.py`）

### 2.0 2026-05-15 更新摘要
- Agent 事件协议补齐 Pi 风格生命周期事件，同时保留旧 SSE 事件兼容前端。
- 会话历史已进入模型上下文，并通过 `agent/context_compaction.py` 做预算压缩。
- Gateway route 只看非 system 消息，避免系统提示污染路由；同一 turn 内 route 会缓存。
- 工具解析、钩子和并行判定拆到 `agent/tool_executor.py`。
- route、context compaction 和 tool execution 元数据写入 SQLite 审计表。
- 报告生成转为 CRO 咨询客户交付导向，并接入 layout review 自动优化循环。

### 2.1 系统提示构造
`stream_run()` 拼接顺序：
1. `DEFAULT_SYSTEM_PROMPT`（角色 + 工作区布局 + 规则）
2. `_current_time_block()` —— 每轮新计算的 `Asia/Shanghai (UTC+8)` 与 UTC 时间，附带 weekday 和"对相对时间求值"的说明（`loop.py:44-54`）
3. `extra_system`（调用方注入，可选）
4. `render_system_prompt()` 把 skill catalog + 激活 skill 的完整正文追加进去

### 2.2 流式事件协议
`_accumulate_stream()` 把 OpenAI 流式 chunk 归一为以下事件：
- `assistant_chunk` — 文本 delta
- `tool_args_chunk` — tool call arguments delta（按 `tool_index` 分槽累积）
- `assistant_message` — 单轮终态，含 `content`、`reasoning_content`、`tool_calls`、`usage`
- `iteration_start` / `tool_call` / `tool_result` / `final` / `timeout` / `max_iters` / `provider_error` / `done`

最终事件保证为 `done`，`run()` 包装器消费这些事件得到 `final_text`。

2026-05-15 后额外发出以下 Pi 风格事件：
- `agent_start` / `agent_end`
- `turn_start` / `turn_end`
- `message_start` / `message_update` / `message_end`
- `tool_execution_start` / `tool_execution_end`
- `context_compaction`
- `route_selected`

前端仍兼容旧事件；新增事件用于可观察性和后续 sidecar 适配。

### 2.3 四层超时体系
| 层级 | 控制点 | 默认值 | 来源 |
|---|---|---|---|
| 整体 wallclock | `AGENT_TIMEOUT_SEC` | 300s | `loop.py:57` |
| 单工具调用 | bash `timeout` 参数 | 120s（被 clamp 到剩余 wallclock，`loop.py:336-339`）| `tools/bash.py` |
| Runner HTTP | `HERMES_RUNNER_HTTP_TIMEOUT_SEC` | 620s | `agent/sandbox.py:14` |
| Runner exec | `HERMES_RUNNER_MAX_TOOL_TIMEOUT_SEC` | 600s（容器内 `timeout --signal=TERM`）| `runner/app.py:31, 96-99` |

`MAX_ITERATIONS=16`，每轮开始 + 每个 tool_call 前都检查 deadline，触发时发出带最近一段 assistant 输出的 `[hermes-timeout]` 哨兵。

### 2.4 工具集（`agent/tools/__init__.py`）
工具全部走 sandbox/runner，不直接在 hermes 容器执行用户命令。

当前注册工具：
- `bash` — `docker_exec(workdir=session_workdir(session_id))`
- `read_file` — 支持 `max_bytes`、`start_line`、`end_line`
- `write_file` — 写 session workspace，拒绝 `/code`
- `edit_file` — exact `old_text` 替换 `new_text`，默认要求唯一匹配，拒绝 `/code`
- `ls` / `grep` / `find` — 只读文件发现与检索工具，优先替代简单 bash
- `render_report` / `review_report_layout` — Markdown → PDF/DOCX + 多模态布局审阅
- `web_search` — Tavily/DDG 可插拔后端，带 per-backend timeout 和诊断输出

`ToolSpec` 记录 `execution_mode` 和 `readonly`。只读且 `parallel_safe` 的同批工具可并行执行；写入类和报告类工具保持顺序执行。

### 2.5 上下文压缩
`agent/context_compaction.py` 实现 Pi 风格 `transformContext`：
- Web 会话层传入最近历史消息。
- `hermes` 历史角色映射为模型侧 `assistant`。
- 构造 `system + history + current user` 后估算 token。
- 超预算时保留 system prompt 和最近消息，把旧消息压缩为 continuation summary。
- 优先用 router 模型总结，失败时退回确定性摘要。
- 压缩结果写入 `agent_context_compactions`。

### 2.6 审计表扩展
新增或扩展的 agent 侧审计：
- `agent_context_compactions`
- `agent_routes`
- `agent_executions` 新增 `tool_call_id`、`route`、`iteration`、`execution_mode`、`readonly`、`blocked_by_policy`、`policy_reason`、`timed_out`

---

## 3. Provider 路由（`agent/router.py` + `fallback.py`）

### 3.1 抽象
`Provider` 协议要求 `complete()` 与 `stream()` 都说 OpenAI tool-use 协议。`OpenAICompatibleProvider` 是通用实现（DeepSeek、Qwen、GLM、Moonshot、Ollama、vLLM 都能复用）。

### 3.2 内置工厂
| Factory | 用途 |
|---|---|
| `gateway()` | 走 `llm-router` 服务（`http://llm-router:8642/v1`），**默认值** |
| `deepseek_chat()` / `deepseek_reasoner()` | 直连 deepseek API |
| `fallback_default()` | 顺序回退：deepseek-chat → deepseek-reasoner |
| `tiered_default()` | 双层：`chat` tier 与 `plan` tier 各自有 fallback 链 |

### 3.3 FallbackProvider 关键设计
- `RETRYABLE` 集合：`RateLimitError`、`APITimeoutError`、`APIConnectionError`、`NotFoundError`、`APIStatusError`（5xx）。
- `NON_RETRYABLE`：`AuthenticationError`、`PermissionDeniedError` —— 切换 provider 救不了 bad credential，直接抛。
- **流式回退用首块校验**（`fallback.py:91-115`）：先 `next(iterator)` 拉到第一个 chunk 才认为这条上游"活"了；首块失败转下一个 provider，已开始 yield 后失败不可恢复。
- `last_used` 记录最近成功的 provider，便于诊断。

### 3.4 TieredProvider
基于"用户消息"做粗分流：长度 ≥ `HERMES_TIERED_PLAN_LEN`（默认 2000）或命中 `PLAN_KEYWORDS`（中英双语：design/architecture/复杂/比较/权衡/...）→ `plan` tier，否则 `chat` tier。每 tier 内部再走自己的 FallbackProvider。

### 3.5 loop 内的 gateway 路由
当 `provider.name == "gateway"` 时，`stream_run` 把 `model` 字段重写为 `chat / code / plan / long_context / multimodal` 之一。

当前路由策略：
- 有图片输入 → `multimodal`
- 非 system 消息长度 ≥ `HERMES_LONG_CONTEXT_ROUTE_LEN` → `long_context`
- 明显代码/调试/仓库任务 → `code`
- 明显规划/架构/对比/优化任务 → `plan`
- 其余请求交给 router 模型做语义分类

同一 agent turn 内会缓存非 `multimodal` / `long_context` route，工具迭代后复用，减少重复 router 调用。每次路由写入 `agent_routes`。

---

## 4. LLM Router 服务（`llm_router/app.py` + `config.py`）

### 4.1 接口
- `GET /healthz` — liveness
- `GET /v1/models` — 把 yaml 里的 providers 列成 OpenAI models list
- `POST /v1/chat/completions` — OpenAI 兼容代理，按请求 `model` 字段路由
- `POST /admin/reload` — 强制重读 yaml

### 4.2 配置驱动（`config/hermes.yaml`）
当前 yaml 的 `llm.providers` 注册了 6 家上游：`default`（deepseek-chat）、`deepseek_reasoner`、`deepseek_flash`（默认）、`deepseek_pro`、`xiaomi_flash`、`xiaomi_pro`。
`endpoint_model_mapping` 把语义路由名映射到具体 provider：
```
router        → deepseek_flash
chat          → deepseek_flash
code          → deepseek_pro
plan          → deepseek_pro
long_context  → deepseek_pro
```

`ConfigCache` 通过 mtime 自动热重载。`${ENV_VAR:-default}` 模式占位符在 `_expand_env()` 里展开。

### 4.3 鉴权
若设置了 `HERMES_LLM_ROUTER_TOKEN` 则要求 `Authorization: Bearer <token>`，hermes 容器透传 `HERMES_GATEWAY_API_KEY = HERMES_LLM_ROUTER_TOKEN`。

---

## 5. Skill 路由（`agent/skill_router.py` + `skills_loader.py`）

- `skills/` 下每个 `.md` 用 frontmatter（`name / description / triggers / always`）+ 正文。
- `llm_select_skills()` 让 router 模型（`endpoint_model_mapping.router`）输出严格 JSON：`{"activate":[…],"reasoning":"<= 15 words"}`，先尝试 `response_format={"type":"json_object"}`，失败兜底纯 prompt。
- 失败时 `_keyword_fallback()` 退回 trigger 关键字匹配 + `always: true` 的 skill 强制激活。
- 选中的 skill 完整 body 被注入 system prompt（`render_system_prompt`），未选中只在目录里出现名字。

当前人工 skills：
- `bio-analysis.md`
- `code-review.md`
- `cro-consulting-report.md`
- `hermes-access.md`

此外，`skills_loader` 会加载 `HERMES_GENERATED_SKILLS_DIR`，默认 `/opt/data/generated_skills`。`agent/report_skill_optimizer.py` 会基于 layout review 每 10 次生成一次 `report-generation-feedback.md`，作为 `always: true` 的自动反馈 skill。

---

## 6. 前端 / 会话层（`app/main.py` + `store.py`）

- FastAPI + Jinja2 模板 + 静态文件 + SSE。
- `multiprocessing.Process` 隔离每个会话的 agent 执行（`_session_processes`）。
- 配置文件 mtime 自动热重载（`refresh_config_middleware`）。
- 会话上限：`limits.max_active_sessions=20`、`max_sessions_per_user=20`、`session_ttl_minutes=10080`（7 天）。
- 文件下载：`/files/{file_id}/download` + `/files.json` 动态列表，`outputs/` 内文件自动注册为可下载（4/29 凌晨已部署，验证 OK）。
- TTL/上传限制：`max_upload_mb=100`、`max_files_per_session=20`，扩展白名单见 yaml `tools.allowed_upload_extensions`。

---

## 7. 外部搜索能力（`agent/search.py` + `agent/tools/web_search.py`）

### 7.1 当前实现
- 后端可插拔（`SearchBackend` Protocol + `_FACTORIES` 注册表 + `register_backend()`）。
- 当前上线后端：`DuckDuckGoBackend`、`TavilyBackend`、`MultiBackend(tavily+duckduckgo)`。
- `web_search` 工具默认 5 条、最多 10 条，支持 `time_range`、`topic`、`include_domains`、`exclude_domains`、`include_raw_content`、`timeout_sec`。
- `HERMES_SEARCH_TOOL_TIMEOUT_SEC` 控制整体检索预算，默认 18s。
- `HERMES_SEARCH_BACKEND_TIMEOUT_SEC` 控制多后端中单个后端预算，默认 8s。
- 单后端或多后端都通过子进程隔离执行，卡住时会 terminate/kill，不拖死 agent。
- 搜索失败会返回可见诊断并写 `outputs/web-search-diagnostic-<ts>.md`，避免“工具中止且无输出”。

### 7.2 已知短板
1. 还没有跨后端缓存层，同一 query 多次会重复调用外部 API。
2. 还没有专门的网页正文 fetch/readability 工具；Tavily `include_raw_content` 可覆盖一部分需求。
3. Brave、Serper、SearXNG 仍未接入。
4. 搜索结果质量还未做离线评测集。

---

## 10. 报告生成与自动优化

Hermes 报告生成已转向 CRO 咨询客户交付：
- 系统提示要求报告面向客户决策，而不是理论综述。
- `cro-consulting-report` skill 定义执行摘要、客户问题、证据、建议、风险、下一步等结构。
- `bio-analysis` skill 增加 CRO/蛋白表达报告交付要求。
- `render_report` 输出 PDF/DOCX，同时返回 CRO 质量警告。
- `review_report_layout` prompt 检查客户交付就绪度。
- 历史报告审计记录见 `docs/report-generation-history-audit.md`。

自动优化循环：
- 每次 `review_report_layout` 成功后统计累计次数。
- 默认每 10 次触发一次优化。
- 从最近 layout review 中抽取高频问题。
- 写入 `/opt/data/generated_skills/report-generation-feedback.md`。
- 自动生成 skill 不覆盖人工 skills。

---

---

## 8. 配置与启动

- `.env` 里管 `HERMES_RUNNER_TOKEN`、`HERMES_LLM_ROUTER_TOKEN`、`DEEPSEEK_API_KEY`、`XIAOMI_API_KEY` 等。
- `config/hermes.yaml` 是统一真源（server / auth / limits / llm / models / search / tools）。
- 镜像构建：`docker compose build` → `docker compose up -d`。
- 日志：每条用户消息和工具调用都写 SQLite（`agent/db.py`：`log_message` / `log_execution`）。

---

## 9. 已知 Tech Debt 与后续优先级

1. **搜索后端单点**（本次提案重点，见 `docs/search-backends.md`）。
2. ~~Provider 工厂里 `qwen_max / glm_4_plus / ollama` 仍是注释，应该按 yaml 动态注册而不是写死在 `_FACTORIES`。~~ **已修复（2026-05-17）**：`agent/router.py` 的 `get_provider` 在内建工厂之外会自动加载 `hermes.yaml` `llm.providers` 并按 mtime 热重载，新增上游不再需要改代码；占位注释已删除。
3. ~~`llm_select_skills` 没有缓存 —— 同一 user_message 每次进 agent 都会再问一次 router 模型。~~ **已修复（2026-05-17）**：`agent/skill_router.py` 加了 TTL LRU 内存缓存（`HERMES_SKILL_ROUTER_CACHE_TTL_SEC`，默认 600s），命中时直接返回，跳过 router 调用；skill_router 事件携带 `cached` 与 `latency_ms`。
4. ~~SSE 客户端没有断线重连（`app/static/` 仅基础 JS）。~~ **已修复（2026-05-17）**：服务端为每个 stream 维护 per-session 环形缓冲 + 单调递增 `event_id`，支持浏览器 `Last-Event-ID` 自动重放；agent 改在后台线程运行，客户端断开不会杀掉它，宽限期内重连可继续流式输出。前端 `EventSource.onerror` 区分 CONNECTING/CLOSED 状态，CONNECTING 时只显示"重连中"提示而不中止。
5. `web_search` 工具没有 `time_range` / `site` 参数；注释提示了"include year"，但模型并不总是照做。
6. **流式入口现在用 thread**：`_run_agent_in_background` 跑在 FastAPI 进程的线程里，没有 `_start_session_process` 那种进程隔离。OOM/segfault 会拖垮整个 web 进程；后续若高并发，可考虑统一到子进程 + IPC 队列。
