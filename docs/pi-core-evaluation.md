# Hermes vs Pi Core 评估与改造建议

> 评估对象：当前 仓库根目录 Python Hermes 与 `pi-main/`（上游参考副本，未随仓库发布） TypeScript Pi monorepo。
> 结论日期：2026-05-15。

## 结论

不建议把 Hermes 底层一次性直接替换为 `pi-agent-core`。

更稳妥的路线是：保留 Hermes 现有的 Web 会话、鉴权、Docker sandbox、文件上传下载、报告渲染和 LLM Router，把 Pi 的成熟 agent 能力分阶段吸收进来。只有当这些能力在 Hermes 里验证稳定后，再考虑把 agent loop 独立成 TypeScript sidecar，最后才评估完整迁移到 Pi core。

原因很直接：

- Hermes 当前是 Python FastAPI + SQLite + Docker runner/sandbox 的服务型系统，Pi 是 TypeScript monorepo，核心目标是终端/SDK coding agent。
- Hermes 的安全边界依赖 runner 持有 `docker.sock`、agent 只通过 runner 执行工具；Pi 的默认工具是本地 cwd 工具，需要重新适配到 Hermes sandbox。
- Hermes 已经有生产相关的会话、设备审批、上传、输出文件注册、报告 PDF/DOCX 渲染、搜索、路由与配置热加载；直接替换会重写这些稳定面。
- Pi 的强项主要在 agent state machine、事件协议、上下文压缩、工具并行与模型/provider 注册，这些可以按模块借鉴。

## 当前 Hermes 的基础能力

Hermes 的核心路径在：

- `app/main.py`：FastAPI Web/SSE、登录、设备审批、会话、上传下载。
- `agent/loop.py`：主 agent loop、流式消息归一、工具调用、超时、max-iteration supervisor、技能注入、模型路由。
- `agent/tools/`：bash、文件读写、web search、报告渲染工具。
- `runner/app.py`：唯一持有 Docker socket 的执行入口。
- `llm_router/`：OpenAI-compatible 路由层，按 `chat/code/plan/long_context/multimodal` 语义模型路由。

这个架构的优势是部署边界清晰，尤其是 sandbox 隔离和 Web 产品面已经成型。

## Pi 可借鉴的能力

Pi 的核心能力分布在：

- `pi-main/packages/agent/src/agent.ts`
- `pi-main/packages/agent/src/agent-loop.ts`
- `pi-main/packages/coding-agent/src/core/compaction/`
- `pi-main/packages/coding-agent/src/core/tools/`
- `pi-main/packages/ai/src/providers/`

最值得吸收的能力：

1. **状态化 Agent**
   Pi 的 `Agent` 拥有明确的 `AgentState`、订阅式事件、steering queue、follow-up queue、abort signal、`continue()`。Hermes 现在每次 turn 基本从当前用户消息构造上下文，缺少长期状态和排队语义。

2. **更完整的事件协议**
   Pi 把事件拆成 `agent_start`、`turn_start`、`message_start/update/end`、`tool_execution_start/update/end`、`turn_end`、`agent_end`。Hermes 当前事件能支撑 SSE，但 message/tool 生命周期较粗。

3. **工具并行与工具钩子**
   Pi 支持 parallel/sequential tool execution、`beforeToolCall`、`afterToolCall`、工具级 sequential 标记、工具结果 `terminate`。Hermes 当前工具调用是顺序执行。

4. **上下文转换和压缩**
   Pi 的 `transformContext`、`convertToLlm`、compaction、文件操作摘要，适合 Hermes 长会话、多文件分析和报告类任务。

5. **Provider/model registry**
   Pi 的 `packages/ai` 覆盖更多供应商、OAuth、模型元数据、thinking 配置。Hermes 的 LLM Router 也有路由优势，但 provider 生态可以参考 Pi 的注册表设计。

6. **更细的文件工具**
   Pi 拆分了 `read/write/edit/grep/find/ls/bash`，而 Hermes 目前主要是 `read_file/write_file/bash`。对代码任务，显式 `grep/find/ls/edit` 会减少模型滥用 bash。

## 不建议直接替换的部分

1. **Web/会话层**
   Pi 的 Web UI/terminal UI 不等价于 Hermes 的设备审批、多人 Web 会话、上传下载、输出文件注册。

2. **执行隔离层**
   Pi 默认工具面向本地工作目录；Hermes 必须继续走 runner/sandbox，否则会弱化现有安全边界。

3. **报告渲染能力**
   Hermes 的 `render_report` 和 layout review 是垂直业务能力，不应被 Pi 默认 coding tools 覆盖。

4. **配置真源**
   Hermes 已经以 `config/hermes.yaml` 和环境变量作为部署真源。Pi 的 `~/.pi` 配置体系不应直接引入服务端。

## 推荐路线

### Phase 1：在 Python Hermes 内吸收 Pi 设计

优先级最高，风险最低。

- 把 Hermes event schema 调整得更接近 Pi：新增 `message_start/message_end/tool_execution_start/tool_execution_end/turn_end`，保留旧事件兼容前端。
- 给工具增加 `before_tool_call` / `after_tool_call` 钩子，用于权限、审计、搜索限流、报告渲染终止策略。
- 增加工具并行执行，但只对明确安全的只读工具开放，例如 `web_search`、`read_file`、未来的 `grep/find/ls`。`bash/write_file/render_report` 默认保持 sequential。
- 增加 `grep/find/ls/edit_file` 等显式工具，减少模型通过 bash 做简单文件任务。
- 增加上下文压缩：基于最近 usage 和消息估算，在接近模型上下文窗口时生成压缩摘要，并保留最近消息和文件操作摘要。

### Phase 2：把 Pi core 作为 sidecar 试运行

在不破坏 Hermes Python 服务面的前提下，新建一个 Node/TypeScript agent sidecar：

- 输入：Hermes session id、system prompt、messages、tools schema。
- 输出：Pi 风格 JSONL/SSE events。
- 工具执行：sidecar 不直接碰文件系统，所有工具仍通过 Hermes runner HTTP API 执行。
- Provider：短期继续走 Hermes `llm_router`，让 Pi core 只负责 loop/state/tool orchestration。

这个阶段的目标是验证 Pi loop 是否能在 Hermes 的 sandbox 和 Web 事件下稳定工作。

### Phase 3：可选迁移核心 loop

只有 Phase 2 稳定后，才考虑把 `agent/loop.py` 替换为 Node sidecar 或 TypeScript service。此时需要完成：

- Hermes DB message 格式与 Pi `AgentMessage` 的双向转换。
- Hermes SSE 事件与 Pi `AgentEvent` 的兼容映射。
- Hermes tools 到 Pi `AgentTool` 的 adapter。
- LLM Router 到 Pi `Model`/`streamFn` 的 adapter。
- 超时、取消、会话恢复、输出文件注册、审计日志全部补齐。

## 最小可行改造清单

建议先做这些，不需要引入 TypeScript 运行时：

1. 给 Hermes 增加 `ls`、`grep`、`find` 三个只读工具。
2. 将 `read_file/write_file` 升级为更精确的 offset/limit 和 edit patch 工具。
3. 在 `agent/loop.py` 引入工具级 execution mode：`parallel_safe` / `sequential`。
4. 增加上下文压缩模块 `agent/context_compaction.py`。
5. 在 SQLite 中记录更完整 usage、route、tool duration、tool error details，方便后续 agent 质量分析。
6. 将 max-iteration supervisor 泛化为 `prepare_next_turn` 钩子，向 Pi 的 `prepareNextTurn` 靠拢。

## 迁移判断标准

满足以下条件前，不建议完整切到 `pi-agent-core`：

- Node sidecar 能连续跑完 Hermes 现有典型任务：代码审查、文件上传分析、报告生成、联网搜索、多轮会话。
- sidecar 的工具调用全部经过 runner/sandbox，且不会绕过 Hermes 权限边界。
- SSE 前端能兼容新事件，旧会话不丢失。
- 上下文压缩不会破坏报告生成、文件引用和下载链接。
- 失败模式明确：provider 失败、工具超时、sidecar crash、用户取消都能恢复并写 recovery output。

## 建议

短期目标应是“Pi 化 Hermes 的 agent loop 设计”，而不是“把 Hermes 换成 Pi”。这样能获得 Pi 的成熟能力，同时保住 Hermes 当前最有价值的服务端能力和隔离模型。

推荐下一步先实现 Phase 1 的前三项：只读工具拆分、工具钩子、只读工具并行。这三项收益明显，风险可控，并且会为后续 sidecar 适配打好边界。

## Phase 1 后续优化：上下文与路由

已按 Pi agent 的 `transformContext()` 思路补齐 Hermes 的会话上下文链路：

- Web 会话层把最近历史消息传入 agent，`hermes` 角色转换为模型侧 `assistant`。
- Agent 侧先构造 `system + history + current user`，再执行上下文压缩。
- 压缩策略保留 system prompt 和最近消息，把旧消息合并为一个 continuation summary。
- 默认预算由环境变量控制：`HERMES_CONTEXT_MAX_TOKENS`、`HERMES_CONTEXT_RESERVE_TOKENS`、`HERMES_CONTEXT_KEEP_RECENT_TOKENS`、`HERMES_CONTEXT_SUMMARY_MAX_CHARS`。
- 若 `HERMES_CONTEXT_LLM_SUMMARY=1`，会优先用 router 模型生成摘要；失败时退回确定性摘要。

路由层也做了 Pi 风格优化：

- 路由只看非 system 消息，避免 system prompt 中的 Python/Docker/tool 词汇把普通任务误判为 code。
- 对明显的 `multimodal`、`long_context`、`code`、`plan` 先做确定性分类。
- 语义 router 模型仍作为兜底，用于普通 chat/code/plan 边界不清的请求。
- 同一 agent turn 内缓存 gateway route，工具迭代后复用，减少重复 router 调用。

进一步按“不改变架构”的优化顺序落地：

- 上下文压缩结果写入 `agent_context_compactions`，完整 summary 持久化，SSE 只发送轻量 metadata。
- 路由决策写入 `agent_routes`，记录 route、iteration、cached、tokens_estimate 和 provider metadata。
- 工具执行审计扩展为 route-aware/tool-aware，记录 tool_call_id、route、iteration、execution_mode、readonly、blocked_by_policy、policy_reason、timed_out。
- `read_file` 支持 `start_line` / `end_line`，用于精确读取大文件片段。
- 新增 `edit_file`，用 exact `old_text` 替换 `new_text`，默认要求唯一匹配并拒绝写 `/code`。
- 工具执行纯逻辑拆到 `agent/tool_executor.py`，`agent/loop.py` 保留流式事件和主循环编排。
- 前端会显示 context compaction 和 route_selected 事件，便于观察当前 turn 的上下文和模型路由。
