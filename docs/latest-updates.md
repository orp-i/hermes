# Hermes 最新更新汇总

> 更新时间：2026-05-17。覆盖 Pi core 借鉴、上下文、路由、工具、报告生成、搜索稳定性、自动优化循环，以及 2026-05-17 的稳定性 / 可观察性补丁（skill 路由缓存、provider 工厂动态化、SSE 断线重连）。

## 1. Agent Loop 与 Pi 风格能力

- 保留 Python Hermes + FastAPI + runner/sandbox + LLM Router 架构，没有切换到底层 `pi-agent-core`。
- Agent 流式事件补齐 Pi 风格生命周期：
  - `agent_start`
  - `turn_start`
  - `message_start`
  - `message_update`
  - `message_end`
  - `tool_execution_start`
  - `tool_execution_end`
  - `turn_end`
  - `agent_end`
- 旧前端事件仍保留：
  - `assistant_chunk`
  - `tool_call`
  - `tool_result`
  - `final`
  - `done`
- 工具执行逻辑拆到 `agent/tool_executor.py`，`agent/loop.py` 保留主循环和流式编排。

## 2. 上下文压缩与会话历史

- Web 会话历史现在会进入模型上下文。
- `hermes` 消息映射为模型侧 `assistant`。
- 当前用户消息会从历史中排除，避免重复注入。
- 新增 `agent/context_compaction.py`：
  - 估算消息 token。
  - 超预算时保留 system prompt 和最近消息。
  - 旧消息合并为 continuation summary。
  - 优先使用 router 模型总结，失败时退回确定性摘要。
- 压缩结果持久化到 `agent_context_compactions`：
  - `tokens_before`
  - `tokens_after`
  - `usable_tokens`
  - `old_messages`
  - `kept_recent_messages`
  - `summary`

相关环境变量：

```bash
HERMES_AGENT_HISTORY_MESSAGES=80
HERMES_CONTEXT_COMPACTION_ENABLED=1
HERMES_CONTEXT_MAX_TOKENS=48000
HERMES_CONTEXT_RESERVE_TOKENS=12000
HERMES_CONTEXT_KEEP_RECENT_TOKENS=16000
HERMES_CONTEXT_SUMMARY_MAX_CHARS=12000
HERMES_CONTEXT_LLM_SUMMARY=1
HERMES_CONTEXT_SUMMARY_TIMEOUT=30
```

## 3. Router 优化

- Gateway route 只看非 system 消息，避免 system prompt 中的 Python/Docker/tool 词汇污染路由。
- 明显请求先走确定性分类：
  - `multimodal`
  - `long_context`
  - `code`
  - `plan`
- 语义 router 模型仍作为兜底。
- 同一 agent turn 内缓存 gateway route，工具迭代后复用。
- 路由决策写入 `agent_routes`：
  - `route`
  - `iteration`
  - `cached`
  - `tokens_estimate`
  - `metadata`

## 4. 工具系统

新增只读工具：

- `ls`
- `grep`
- `find`

增强文件工具：

- `read_file` 支持 `start_line` / `end_line`。
- 新增 `edit_file`：
  - exact `old_text` 替换 `new_text`
  - 默认要求唯一匹配
  - 拒绝写 `/code`

工具执行元数据：

- `execution_mode`
- `readonly`
- `blocked_by_policy`
- `policy_reason`

只读且 `parallel_safe` 的同批工具会并行执行。`bash`、`write_file`、`edit_file`、`render_report`、`review_report_layout` 仍保持顺序执行。

工具审计扩展到 `agent_executions`：

- `tool_call_id`
- `route`
- `iteration`
- `execution_mode`
- `readonly`
- `blocked_by_policy`
- `policy_reason`
- `timed_out`

## 5. 搜索稳定性

`web_search` 已从“同步长调用”改为“有预算、可降级、有部分诊断输出”。

当前行为：

- `tavily+duckduckgo` 多后端检索支持 per-backend timeout。
- 单个后端卡住会被子进程终止。
- Tavily 超时后可降级 DuckDuckGo。
- 单后端也受总超时保护。
- 失败时不再空 stdout：
  - 返回可见诊断文本。
  - 写入 `outputs/web-search-diagnostic-<ts>.md`。
  - agent 可继续缩窄 query、减少结果数、指定 `include_domains` 或稍后重试。

相关环境变量：

```bash
HERMES_SEARCH_BACKEND=tavily+duckduckgo
HERMES_SEARCH_TOOL_TIMEOUT_SEC=18
HERMES_SEARCH_BACKEND_TIMEOUT_SEC=8
TAVILY_TIMEOUT=20
```

`web_search` 工具参数新增：

- `timeout_sec`

## 6. 报告生成：CRO 咨询交付方向

系统报告标准已从“最终分析报告”改为“面向 CRO 咨询客户的交付文档”。

新增 skill：

- `skills/cro-consulting-report.md`

更新 skill：

- `skills/bio-analysis.md`

报告必须更偏向：

- 客户决策问题
- 执行摘要
- 数据/材料范围
- 实际分析方法
- 关键发现和证据
- 客户含义
- 建议动作
- 风险与局限
- 下一步工作计划
- 输出文件引用

避免：

- 理论化长背景
- 技术笔记式堆砌
- 未解释客户影响的技术发现
- 星级评分或 `$ / $$ / $$$` 这类不专业成本表达
- 单行超长蛋白/核酸序列

## 7. 报告渲染与质量检查

`render_report` 增强：

- 返回 CRO 交付质量警告。
- PDF 表格单元格自动换行。
- 大表可跨页并重复表头。
- 长序列代码块按 80 字符换行。
- 水平分隔线不再当普通段落渲染。
- PDF 增加页脚和页码。

质量检查会提示：

- 缺少执行摘要。
- 缺少建议。
- 缺少下一步。
- 缺少风险/局限。
- 段落过密。
- 长序列未换行。
- 星级/符号化成本表达。
- 乱码迹象。
- 缺少客户含义或建议动作映射。

`review_report_layout` prompt 已改为检查 CRO 咨询客户交付就绪度，而不仅是版式。

## 8. 历史报告审计

新增：

- `docs/report-generation-history-audit.md`

历史审计发现：

- 旧报告技术内容充分，但像技术分析记录。
- 缺少执行摘要、下一步、风险/局限。
- 建议常埋在技术章节中。
- 长段落、表格列宽、长序列、链接/乱码是 layout review 的高频问题。

这些问题已固化进：

- `cro-consulting-report` skill
- `bio-analysis` skill
- `render_report` 质量检查
- layout review prompt

## 9. Layout Review 自动优化循环

新增：

- `agent/report_skill_optimizer.py`

机制：

- 每次 `review_report_layout` 成功后检查累计 review 次数。
- 默认每 10 次触发一次自动优化。
- 读取最近 `*layout-review*.md`。
- 抽取高频问题。
- 生成 `/opt/data/generated_skills/report-generation-feedback.md`。
- `skills_loader` 会自动加载 `/opt/data/generated_skills`。
- 自动生成的 feedback skill 设置为 `always: true`。
- 不修改人工维护的 `skills/*.md`。

相关环境变量：

```bash
HERMES_REPORT_SKILL_AUTO_OPTIMIZE=1
HERMES_REPORT_SKILL_OPTIMIZE_EVERY=10
HERMES_GENERATED_SKILLS_DIR=/opt/data/generated_skills
```

当前线上历史累计 `review_report_layout` 为 4 次，会在第 10 次成功 review 后首次触发。

## 10. 前端可观察性

聊天页现在会显示：

- 上下文压缩状态。
- 当前模型路由。
- route cache 命中状态。
- 工具并行/完成状态仍沿用原有工具 note。

## 11. 2026-05-17 稳定性与可观察性补丁

### 11.1 Skill 路由缓存

`agent/skill_router.py` 现在带一个内存 TTL LRU：

- key = `sha256(user_message + 排序后的 skill 目录签名)`
- 命中时直接返回，跳过 router LLM 调用，`latency_ms=0`。
- skill_router 事件新增 `cached`、`latency_ms` 字段；前端会把"（缓存）"标在技能路由 note 上。
- 失败兜底（keyword 回退）不进缓存。

相关环境变量：

```bash
HERMES_SKILL_ROUTER_CACHE_ENABLED=1
HERMES_SKILL_ROUTER_CACHE_TTL_SEC=600
HERMES_SKILL_ROUTER_CACHE_SIZE=256
```

模块层还暴露了 `cache_stats()` / `cache_clear()` 便于诊断。

### 11.2 Provider 工厂动态化

`agent/router.py` 不再写死 qwen/glm/ollama 占位注释。`get_provider(name)`：

1. 先查内建工厂（`gateway` / `deepseek-chat` / `deepseek-reasoner` / `xiaomi-mimo-pro` / `fallback` / `tiered`）。
2. 再查 `hermes.yaml` `llm.providers` 下的所有条目，按 mtime 自动重载。
3. yaml 条目通过 `OpenAICompatibleProvider` 包装：`base_url` / `api_key` 支持 `${ENV:-fallback}` 展开，`default_model` 作为模型名，`timeout` 缺省取 `llm.api_timeout_seconds`。
4. 缺 `base_url` 或 `api_key` 的条目跳过并日志告警，避免首次调用才崩。

新增 `list_providers()` 工具函数返回当前可用的全部 provider 名字。新增 provider 只需改 yaml，不动代码。

### 11.3 SSE 断线重连

`/chat/{session_id}/stream` 已重构：

- agent 跑在后台线程，事件写入 per-session 环形 buffer（带单调递增 `event_id`）。
- 每条 SSE 都带 `id: <n>`。客户端断线后浏览器自动重连，会带上 `Last-Event-ID`，服务端从该 ID 之后重放缓冲事件，再继续 tail。
- 客户端断开不会杀掉 agent；agent 完成后写入最终 assistant 消息，再宽限 `STREAM_RUN_GRACE_SEC`（默认 60s）允许迟到的重连。
- 服务端定期 `: keepalive` 注释防止代理超时断流。
- 同一 session 在 agent 跑完之前提交新内容会返回 409；带 `Last-Event-ID` 或同 content 的请求被识别为重连并加入现有 run。

前端 `chat.html`：

- `onerror` 区分 `readyState`，`CONNECTING (0)` 显示"连接中断，正在重连..."的临时 note，`CLOSED (2)` 才认定为致命。
- `open` 事件清理 reconnect note。
- 只有显式的 `done` 事件才结束流。

相关环境变量：

```bash
HERMES_STREAM_BUFFER_LIMIT=1024
HERMES_STREAM_RUN_GRACE_SEC=60
HERMES_STREAM_TAIL_KEEPALIVE_SEC=15
HERMES_STREAM_RETRY_MS=2000
```

注意：流式入口现在使用线程而非子进程；非流式 `_start_session_process` 仍用 `multiprocessing.Process`，两条路径互不冲突。

---

## 11bis. 2026-05-17 无代理网络模式

Hermes 的入流量和出流量都不再绑定到 netbird mesh（`<mesh-ip>` / `wt0`）。

### 入流量
- `docker-compose.yml` 端口发布从 `<mesh-ip>:5020:8000` 改为 `0.0.0.0:5020:8000`，意味着 LAN/公网/netbird 任意接口都能进。
- `config/hermes.yaml`：`bind_ip` 改为 `0.0.0.0`，`public_base_url` 默认改为 LAN 地址 `http://<lan-ip>:5020`。`unified_config.py` 的默认值同步更新；浏览器实际访问的 URL 仍可通过 config 页面或 `HERMES_PUBLIC_BASE_URL` 覆盖。
- `public_base_url` 决定 Cookie 的 `secure` 属性（`https://` 时 secure），不影响绑定本身，请按部署环境填正确协议。

### 出流量
- 宿主机当前 shell 设置了 `http_proxy/https_proxy=http://<mesh-proxy-ip>:8080`（netbird 代理）。`docker-compose.yml` 现在为 `hermes`、`hermes-runner`、`hermes-sandbox`、`hermes-llm-router` 四个服务显式注入：

```yaml
HTTP_PROXY: ""
HTTPS_PROXY: ""
http_proxy: ""
https_proxy: ""
NO_PROXY: "*"
no_proxy: "*"
```

  确保即使将来 host 或 docker daemon 引入 proxy 变量，容器也强制不走代理；OpenAI/Tavily/DeepSeek/Xiaomi HTTP 客户端（`httpx`/`requests`）会因为 `NO_PROXY=*` 直接绕开任何代理。

### 验证
- `docker compose config` 解析正常，端口为 `0.0.0.0:5020 → 8000`，四个服务 env 都含 `HTTP_PROXY=""` + `NO_PROXY=*`。
- 默认路由仍是 `<lan-gateway> dev wlo1`，确认 LLM/搜索 egress 走 WiFi/LAN 而非 `wt0`。如需 DNS 也避开 netbird，检查 `/etc/resolv.conf` 不指向 <mesh-ip>。
- 重新部署：

```bash
docker compose down
docker compose up -d --build
ss -tln | grep 5020   # 应出现 0.0.0.0:5020
docker exec hermes env | grep -iE "http_proxy|no_proxy"  # 应见空 + NO_PROXY=*
```

---

## 11quater. 2026-05-17 参考文献校验工具 + 清华镜像源默认值

### `validate_references` 工具

新增 agent 工具 `agent/tools/validate_references.py`：

- 接受一组 references，每条至少给 `title`（或 `doi`/`pmid`）。可选 `cite_text`、`authors`、`year`。
- 用 `agent.search`（Tavily + DuckDuckGo）按 title + author + year 搜索，逐条做 title 相似度 + 学术域名匹配。
- 已知学术域名清单：PubMed/PMC/DOI/Nature/Science/Cell/NEJM/Lancet/BMJ/PNAS/PLOS/Frontiers/biorxiv/medrxiv/arxiv/Springer/Wiley/Elsevier/ScienceDirect/MDPI/Cambridge/JSTOR/Crossref/Semantic Scholar/OpenAlex/EuropePMC 等。
- 状态：
  - `verified` —— 学术域名 + 标题匹配（相似度 ≥ 0.6）
  - `verified_web` —— 标题匹配但非学术域名，作为较弱证据
  - `not_found` —— 搜索结果中无标题匹配，**几乎可以认定是幻觉引用**
  - `mismatch` —— 标题匹配但年份/作者明显对不上
  - `unverifiable` —— 搜索失败或缺最少元数据
- 工具是 `parallel_safe + readonly`，可与其他只读工具并行。
- 审计 JSON 写到 `outputs/reference-validation-<ts>.json`。
- 没有 NCBI/代理依赖；和 `web_search` 同一条 egress 路径（无代理直连或按 backend 自身需求）。

环境变量：

```bash
HERMES_REF_VALIDATE_TIMEOUT_SEC=12        # 单次搜索超时
HERMES_REF_VALIDATE_MAX_RESULTS=6         # 每条 ref 看几条结果
HERMES_REF_VALIDATE_TITLE_THRESHOLD=0.6   # 标题相似度阈值
```

`skills/cro-consulting-report.md` 和 `skills/bio-analysis.md` 已写入"在最终 render_report 之前必须调用 validate_references"的硬要求和 5 种状态的处置规则。

### Docker 构建默认走清华 TUNA 镜像

`Dockerfile`、`docker/runner.Dockerfile`、`docker/sandbox.Dockerfile` 都加了三个 build ARG（默认开启）：

```dockerfile
ARG USE_TUNA_MIRROR=1
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
```

机制：

- `PIP_INDEX_URL` 通过 `ENV` 注入到镜像，所有 `pip install` 自动走 tuna。
- `USE_TUNA_MIRROR=1` 时，构建阶段 sed 改写 `/etc/apt/sources.list.d/debian.sources`，把 `deb.debian.org` 和 `security.debian.org` 都指向 `mirrors.tuna.tsinghua.edu.cn`（兼容老 `sources.list` 格式）。

`docker-compose.yml` 四个服务的 `build:` 都注入这三个 args，可通过 `.env` 或环境变量覆盖：

```bash
USE_TUNA_MIRROR=0 docker compose build           # 走默认源
PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple docker compose build hermes
APT_MIRROR=mirrors.ustc.edu.cn docker compose build sandbox
```

实测验证：`docker build -f docker/runner.Dockerfile` 拉包全部来自 `mirrors.tuna.tsinghua.edu.cn/debian` 和 `pypi.tuna.tsinghua.edu.cn/packages`。

---

## 11ter. 2026-05-17 上传白名单与文档解析

`config/hermes.yaml`、`app/unified_config.py`、`app/config.py` 三处的 `allowed_upload_extensions` 同步扩展（42 个扩展名），覆盖：

- 文本/标记：`.txt .md .rst .log .csv .tsv .json .jsonl .yaml .yml .toml .xml .html .htm`
- Office / 文档：`.doc .docx .ppt .pptx .xls .xlsx .odt .ods .odp .rtf .pdf`
- 图片：`.png .jpg .jpeg .webp .gif .bmp .tiff .svg`
- 压缩：`.zip .gz .tar .tgz .7z`
- 代码/数据：`.py .js .ts .ipynb`

`docker/sandbox.Dockerfile` 新增 Python 解析库，确保 Agent 拿到文件后能直接读取：

```text
python-docx   # .docx
python-pptx   # .pptx
odfpy         # .odt/.ods/.odp
striprtf      # .rtf
xlrd          # .xls（老版二进制 Excel）
pypdf         # .pdf 文本抽取
chardet       # 编码探测（中文 .txt GBK 等）
markdown      # .md
```

老二进制 `.doc`、`.ppt` 没有纯 Python 解析器，Agent 可在 bash 里用 `python -c` 或 fallback 到 `pdftotext`/`antiword`（非默认安装）；建议先用户侧另存为 `.docx`/`.pptx`。

部署：

```bash
docker compose build sandbox     # 只重建 sandbox 镜像
docker compose up -d sandbox     # 替换长驻 sandbox 容器
docker compose up -d hermes      # 重启 hermes 让新 config 生效
```

---

## 12. 部署状态

最近更新均已通过：

```bash
python -m compileall -q agent app llm_router
docker compose up -d --build hermes
curl http://<mesh-ip>:5020/healthz
```

当前服务：

- `hermes`
- `hermes-runner`
- `hermes-sandbox`
- `hermes-llm-router`

入口：

```text
http://<mesh-ip>:5020/
```
