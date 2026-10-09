# 外部搜索后端选型（强化 web_search 能力）

> 目标：在保留 DuckDuckGo（`ddgs`）作为零成本兜底的前提下，引入一档 **质量 + 性能均衡** 的第三方 API 作为主用后端，并把 `agent/search.py` 的占位接入做成可生产代码。

---

## 0. 现状摘要

当前已实现：

```python
_FACTORIES = {
    "duckduckgo": DuckDuckGoBackend,
    "tavily": TavilyBackend,
    "tavily+duckduckgo": lambda: MultiBackend("tavily+duckduckgo", [TavilyBackend(), DuckDuckGoBackend()]),
    "tavily+ddg": lambda: MultiBackend("tavily+ddg", [TavilyBackend(), DuckDuckGoBackend()]),
}
```

`web_search` 当前默认通过 `HERMES_SEARCH_BACKEND=tavily+duckduckgo` 使用 Tavily 主用 + DuckDuckGo 兜底。

2026-05-15 稳定性更新：

- 多后端检索增加 per-backend timeout。
- 单个后端卡住会在子进程内 terminate/kill，不阻塞整个 agent。
- `web_search` 增加 `timeout_sec` 参数。
- 搜索失败不再空输出，会返回诊断信息并写 `outputs/web-search-diagnostic-<ts>.md`。
- compose 显式透传：
  - `HERMES_SEARCH_TOOL_TIMEOUT_SEC`
  - `HERMES_SEARCH_BACKEND_TIMEOUT_SEC`

---

## 1. 候选 API 横评

下表按"Agent 用途适配度"排序。价格为 2026-04 公开标价，可能微调；调用前请到对应官网核对。

| API | 定位 | 单次返回内容 | 免费额度 | 付费门槛 | Agent 视角优势 | 劣势 |
|---|---|---|---|---|---|---|
| **Tavily** | 为 LLM 设计的搜索 API | 标题 / URL / snippet **+ 可选 raw_content** | 1,000 次/月 | 约 $0.005 / 次 | 一次调用拿"用于 RAG 的清洗后文本"；`include_answer=True` 自带摘要；`search_depth=advanced` 时把网页正文抓回 | 国内访问偶发抖动；snippet 来源偏英语网络 |
| **Brave Search API** | 独立索引（自爬非 Bing/Google 二手） | 标题 / URL / 描述 / age / 类型 | 2,000 次/月（Free 套餐，1 QPS）| $3 / 每千次起（Pro）| 索引独立，结果差异化；隐私友好；返回 `news / videos / faq` 多种 vertical | 不返回正文，需要二次抓取；中文结果质量不如 SerpAPI/Tavily |
| **Serper.dev** | Google SERP 包装 | Google 风格 organic / KG / answer box / news | 2,500 次（一次性试用） | $50 / 每月 50K 次（$0.001/次，性价比最高的 Google 镜像） | 直接是 Google 结果，权威性最高；延迟稳定 ~1s | 不带正文；中国大陆访问需走代理 |
| **SerpAPI** | 多搜索引擎 SERP（Google/Bing/Baidu/百度...）| 极完整的 SERP JSON | 100 次/月 | $75 / 每月 5K | 唯一原生支持百度/Bing 中文搜索；结构化字段最齐 | 价格贵，Agent 量大不划算 |
| **Bing Web Search v7**（Azure）| 微软索引 | 标题 / URL / snippet / dateLastCrawled | F1 免费 1K/月 | S1 ~$3/千次 | 亚洲/中文召回比 Google API 强；可选 `freshness=Day/Week/Month` | 需要 Azure 账号；Microsoft 已宣布 2025 后将其转入 Azure AI Foundry，注册流程比第三方繁琐 |
| **Exa（前 Metaphor）** | 神经语义搜索 | URL / title / 自动摘要 / `text=true` 时返回正文 | $10 试用额度 | $5 / 1K（基础）至 $20+ / 1K（含 contents）| 语义召回适合"找类似文章"；`/contents` 端点返回干净 markdown | 关键词类查询召回不如传统 SERP |
| **You.com Search API** | LLM-friendly | snippet / 段落级文本 | 申请制 free tier | 商务定价 | 段落粒度，方便直接拼进 context | 公开文档少，企业销售流程 |
| **SearXNG（自建）** | 元搜索代理 | 聚合 Bing/DDG/Brave/Qwant 等 | 自建免费 | 自己服务器成本 | 零外部费用、无配额、可完全私有化 | 维护负担；被上游限流时无力恢复；冷启动质量需调优 engines.yml |

---

## 2. 推荐组合

按"开箱即用 → 量大成本 → 极致私有化"三档给出方案，**默认推 A**。

### A. **Tavily 主用 + DuckDuckGo 兜底**（已上线）
- **理由**：Tavily 是市面上唯一明确把"为 Agent/RAG 优化"作为产品定位的 API，单次调用就能拿到 snippet + 正文 + 自动答案，能直接砍掉 60% 的"再 curl 一次抓正文"的 bash 调用。1K/月的免费额度足够内部 dogfooding；超出按 $5/千次走也不痛。
- **对 Agent 的实际收益**：
  - `include_answer=True` → 简单事实查询（"DeepSeek V4 发布日期"）一轮就给最终答案，省掉 LLM 二次推理。
  - `search_depth="advanced"` + `include_raw_content=True` → 复杂调研类查询直接拿到清洗后的网页 markdown，免去 sandbox 内跑 trafilatura/readability 的负担。
  - 内置 `topic="news"` / `days=7` / `include_domains=[…]` 过滤参数，可以 1:1 暴露给 Agent。
- **降级策略**：Tavily 5xx、限流或超时 → 自动落回 `DuckDuckGoBackend`；DDG 也失败 → 返回明确诊断并写入 `outputs/web-search-diagnostic-*.md`，让 Agent 可以继续缩窄 query 或解释限制。

### B. **Brave + Serper 双主（A/B）+ Tavily 仅做正文抓取**
- 适合 query 量上来后（>5K/月）想压成本但又要稳定的场景。Brave/Serper 单价 $0.001-$0.003，比 Tavily 便宜。
- 把 Tavily 退化为"专用 fetcher"——只在 Agent 显式要"读这篇全文"时才调它的 `extract` 端点。

### C. **SearXNG 自建 + Tavily 备用**
- 完全私有化首选。在 sandbox 同 compose 网络起一个 `searxng/searxng:latest`，配置 `engines.yml` 启用 Brave/Bing/Mojeek/Qwant。
- 优点：零 API key 泄露面、零配额。
- 缺点：上游引擎自己限流时 SearXNG 会大面积失败 → 必须配 Tavily 当 last resort。

---

## 3. 当前接入状态（方案 A）

- `TavilyBackend` 已实现。
- `MultiBackend` 已实现。
- `web_search.py` 已支持：
  - `time_range`
  - `topic`
  - `include_domains`
  - `exclude_domains`
  - `include_raw_content`
  - `timeout_sec`
- `docker-compose.yml` 已配置：
  - `HERMES_SEARCH_BACKEND=tavily+duckduckgo`
  - `HERMES_SEARCH_TOOL_TIMEOUT_SEC`
  - `HERMES_SEARCH_BACKEND_TIMEOUT_SEC`
- 已验证慢后端超时后 fallback 到快后端。
- 已验证失败时有 stdout 诊断和 outputs 诊断文件。

---

## 4. 当前关键实现

```python
def _search_with_timeout(backend, query, *, max_results, timeout, **kwargs):
    # Runs backend.search in a child process. If it hangs, terminate/kill it.
    ...

class MultiBackend:
    def search(self, query: str, max_results: int = 5, **kwargs) -> list[dict]:
        # Try each backend with HERMES_SEARCH_BACKEND_TIMEOUT_SEC budget.
        ...
```

`agent/tools/web_search.py` 负责整体工具预算和诊断文件：

```text
outputs/web-search-diagnostic-<ts>.md
```

---

## 5. 决策建议（一句话）

当前建议保持 **Tavily 主用 + DDG 兜底（方案 A）**。下一步优先做：

1. 搜索结果缓存，避免同 query 重复调用外部 API。
2. 独立网页正文 fetch/readability 工具。
3. 小型离线评测集，对比 Tavily、DDG、Brave/Serper 后再扩后端。
