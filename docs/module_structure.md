# Module Structure

重构后的代码按**职责分层**,依赖关系是一张无环图(DAG):上层只依赖下层,
每个概念只有一个"家"。本文件是结构的真相源(与 `src/` 实际 import 关系一致)。

## 分层总览

```
                 ┌──────────────────────────────────────────┐
  入口 / 编排      │ event_bottom_fishing.py   email_daily_report.py
                 └──────────────────────────────────────────┘
                          │ 组装流水线、CLI、收发
                          ▼
  推理 / 渲染      agent_runtime.py        reporting.py        paper_portfolio.py
                 (多 agent 评审)          (Markdown/JSON)      (纸面验证账本)
                          │
                          ▼
  评分 / 提示      scoring.py              llm_prompts.py
                 (确定性打分)             (提示词模板/token)
                          │
                          ▼
  检索            data_sources.py
                 (universe/news/price/SEC + 缓存)
                          │
                          ▼
  原语 (叶子)      models.py               formatting.py
                 (数据类)                 (pct / multiple)
```

## 依赖图(实际 import 边)

```
formatting        ← scoring, llm_prompts, reporting
models            ← data_sources, scoring, reporting, paper_portfolio, agent_runtime
data_sources      → models                              ← scoring, paper_portfolio, agent_runtime
scoring           → data_sources, formatting, models    ← agent_runtime, event_bottom_fishing
llm_prompts       → formatting                          ← agent_runtime, event_bottom_fishing
reporting         → formatting, models                  ← event_bottom_fishing, email_daily_report
paper_portfolio   → data_sources, models                ← event_bottom_fishing
agent_runtime     → data_sources, llm_prompts, models, scoring
event_bottom_fishing → (上面全部)
email_daily_report   → event_bottom_fishing, reporting
```

`formatting` 和 `models` 是叶子(不依赖任何业务模块),所以任何层都能引用它们而不会形成环。

## 模块职责与公开接口

| 模块 | 行数 | 职责 | 关键公开接口 |
|---|---:|---|---|
| `formatting.py` | 15 | 纯数值格式化(叶子,无依赖) | `pct`, `multiple` |
| `models.py` | 151 | 全部共享数据类 | `Candidate`, `NewsItem`, `PriceStats`, `FilingItem`, `DataConfidence`, `FundamentalScore`, `Evidence`, `AgentResult`, `ToolResult`, `AgentTask`, `AgentPlan`, `AgentReview` |
| `data_sources.py` | 613 | 所有外部 I/O + 本地缓存 | `load_universe`, `load_aliases`, `load_sec_ticker_map`, `fetch_news`, `fetch_price_stats`, `fetch_stooq_price_stats`, `fetch_recent_sec_filings`, `fetch_sec_company_facts`, `EVENT_KEYWORDS` |
| `scoring.py` | 569 | **唯一打分家**:事件初筛、深挖、基本面、数据置信 | `score_candidate`, `score_fundamentals`, `score_deep_dive`, `build_data_confidence`, `apply_fundamental_scores`, `apply_deep_dive`, `apply_data_confidence`, `count_categories`, `event_label`(叙述短语) |
| `llm_prompts.py` | 120 | 提示词模板 + token 估算 | `build_llm_review_prompt`, `build_agent_task_prompt`, `compact_text`, `estimate_tokens`, `*_SYSTEM_PROMPT` |
| `agent_runtime.py` | 650 | **在用的**多 agent 评审(带 plan/tool trace,可逐 agent 调 LLM) | `apply_agent_reviews`, `build_agent_review`, `build_agent_plan` |
| `reporting.py` | 335 | 渲染 Markdown + JSON | `write_outputs`, `candidate_to_dict`, `event_display_label`(展示标签) |
| `paper_portfolio.py` | 507 | 纸面验证账本(SQLite) | `apply_paper_buy`, `update_portfolio_performance`, `archive_report`, `append_*_to_outputs` |
| `event_bottom_fishing.py` | 834 | 流水线/CLI 编排 + **legacy agent 实现(可切换)** | `main`, `parse_args`, `build_arg_parser`, `scan`, `build_candidate`, `prepare_selected_candidates`, `apply_agent_reviews`(派发器), `apply_agent_reviews_legacy` |
| `email_daily_report.py` | 109 | SMTP 发信入口(复用 `parse_args`) | `generate_report`, `send_email`, `main` |

> `event_bottom_fishing.py` 偏大,因为底部保留了一整套 **legacy agent 委员会**(用 6 因子证据模型,
> 通过 `--agent-impl legacy` 切换;默认走 `agent_runtime`)。把它抽到独立模块是后续可选项。

## 数据流(每日流水线)

```
parse_args
  → scan                       data_sources.load_universe / load_aliases，并发抓 news+price
      → build_candidate        每只股票:fetch_news + fetch_price_stats + scoring.score_candidate
      → prepare_selected_candidates
            scoring.apply_fundamental_scores   (SEC company facts)
            scoring.apply_deep_dive            (二阶深挖排序 + Focus 资格)
            scoring.apply_data_confidence      (SEC filings + Stooq 第二价源)
            apply_agent_reviews → [runtime|legacy] 多 agent 评审
  → reporting.write_outputs    Markdown + JSON
  → paper_portfolio            买入(可跳过)/ 盯市 / 归档
```

## "我要加 X,改哪里?"

| 需求 | 改哪 |
|---|---|
| 换股票池(自定义 watchlist 等) | `data_sources.load_universe` |
| 新增/调整事件关键词分类 | `data_sources.EVENT_KEYWORDS` |
| 调整打分权重 / 增加打分维度 | `scoring.py`(**唯一打分家**;改这里就生效) |
| 接新数据源(财报转录、8-K 正文…) | `data_sources.py` 出 fetch 函数,在 `scoring`/`agent_runtime` 里消费 |
| 改 agent 推理 / 加专家 | `agent_runtime.py`(在用);legacy 版在 `event_bottom_fishing.py` 底部 |
| 改提示词 / token 预算 | `llm_prompts.py` |
| 改报告版式 | `reporting.py`(不影响研究结论) |
| 加共享数据字段 | `models.py`(所有层共用) |

## 入口

```bash
python3 src/event_bottom_fishing.py --top 10                    # 生成报告(默认 runtime agent)
python3 src/event_bottom_fishing.py --top 10 --agent-impl legacy # 用 legacy 委员会
python3 src/email_daily_report.py --to you@example.com           # 生成并 SMTP 发信
python3 tests/smoke_offline.py                                   # 离线冒烟(无网络/无 key/不碰 DB)
```

## 设计不变量(重构守护的)

- **单一真相源**:每个职责一个家;`score_candidate` 只在 `scoring`,`pct/multiple` 只在 `formatting`。
  (`tests/smoke_offline.py` 用 `is` 断言守护,防止再次出现"改了却不生效"的静默分叉。)
- **无环依赖**:上层依赖下层,叶子是 `models` / `formatting`。
- **行为可解释**:确定性打分是护栏,agent 推理是叠加层,报告只解释不改结论。
