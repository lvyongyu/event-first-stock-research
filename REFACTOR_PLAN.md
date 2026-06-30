# Refactor Plan — event-first-stock-research

> 目的:收尾一次"做了一半"的模块化重构。`models / data_sources / scoring /
> agent_runtime / reporting / paper_portfolio` 这套干净模块已经存在并在运行,
> 但 `src/event_bottom_fishing.py`(1773 行)里仍保留着重构前的整套旧实现。
> 本计划把主文件瘦身成纯编排层,消除重复与静默分叉,**同时保留"预留功能"**。
>
> 状态:**已实施(Step 1–4 全部完成)**。净 −865 行;全模块 `py_compile` 通过、pyflakes 全绿;
> `score_candidate` 搬家逐字段等价;默认行为(`--agent-impl runtime`)不变,CI/邮件不受影响。
>
> 进度:
> - ✅ Step 1 数据类去重 → `models`
> - ✅ Step 2 数据/评分函数去重 → `data_sources`/`scoring`;`score_candidate` 归位到 `scoring`
> - ✅ Step 3 = **3-C**:`--agent-impl runtime|legacy` 开关 + `apply_agent_reviews_legacy` 编排器,
>   把预留委员会接成真·可切换实现(默认 runtime)
> - ✅ Step 4 收尾:`pct`/`multiple` 收敛到叶子模块 `formatting.py`;`reporting.event_label` →
>   `event_display_label` 消歧;清理 `agent_runtime.py` 未用 import + 死变量
> - ✅ Step 5 离线冒烟测试 `tests/smoke_offline.py`(单一真相源 / 评分钉值 / 双 agent 实现+序列化 / 路由)
> - ➕ 额外:修通 `email_daily_report.py` 的 SMTP 备用入口(抽 `build_arg_parser`/`parse_args` 共用,
>   修掉手搓 `SimpleNamespace` 漂移的 4 处缺字段 + `write_outputs` 误用)

---

## 0. 现状结论(已验证)

- `python3 -m py_compile src/*.py` 全部通过,程序可运行,CI 正常产出。
- 主文件 54 个函数中 **34 个**与 `data_sources.py` / `scoring.py` 完全重名重复。
- 主文件 **9 个 dataclass** 全部与 `models.py` 重复。
- 主文件的 `EVENT_KEYWORDS / NEGATIVE_WORDS / POSITIVE_WORDS` 与 `data_sources.py` 重复。
- `pct` / `multiple` 共 **3 份**(`llm_prompts.py` / `scoring.py` / 主文件 import)。
- `event_label` 在 `scoring.py` 与 `reporting.py` 各一份,**返回值不同**(详见 Step 4)。
- 主文件 ~470 行 Agent 流水线(`build_agent_review`、`agent_news`…`apply_llm_overlay`)
  从入口**不可达**:`apply_agent_reviews` 实际转发给 `agent_runtime.run_agent_reviews`。
  → 经确认这是**预留功能**(reserved),按 Step 3 处理,**不删除**。

### 真相源(谁在跑)

| 能力 | 实际运行的实现 | 主文件里的重复/旧版 |
|---|---|---|
| 数据抓取(news/price/SEC) | 主文件本地副本(live) + `data_sources.py`(被 agent_runtime 用) | 两份并存 |
| 评分(fundamentals/deep_dive) | **主文件本地副本(live)** | `scoring.py` 同名函数其实**不可达** |
| Agent 评审 | **`agent_runtime.py`(live)** | 主文件 ~470 行(预留) |
| 报告 / 纸面组合 | `reporting.py` / `paper_portfolio.py`(live) | — |

> ⚠️ 注意一个反直觉点:`scoring.py` 的大函数(`score_fundamentals`、`apply_deep_dive`、
> `score_deep_dive`、`build_reasons`…)目前**没有任何调用方**——真正在跑的是主文件里的本地副本。
> 也就是说改 `scoring.py` 的评分逻辑当前**不会生效**。本计划要把真相源统一到 `scoring.py`。

---

## Step 1 — 数据类去重(主文件 → 全部用 `models`)

**动机**:主文件造的是 `event_bottom_fishing.Candidate`,却传给标注 `models.Candidate`
的函数,目前靠鸭子类型硬撑;给 `models` 加字段随时可能 `AttributeError`。

**删除** `src/event_bottom_fishing.py` 第 **109–219 行**的全部 9 个 dataclass:
`NewsItem, PriceStats, FilingItem, DataConfidence, FundamentalScore, Evidence,
AgentResult, AgentReview, Candidate`。

**新增 import**(主文件顶部):
```python
from models import Candidate, NewsItem, PriceStats
```
> 仅这 3 个被主文件保留代码(`score_candidate` / `build_candidate` 的类型标注 + 构造 `Candidate`)
> 直接引用。其余 dataclass 由被调模块各自从 `models` 引入。

**验证**:`models.AgentReview` 比主文件版多了 `agent_plan: list[AgentTask]`、
`tool_trace: list[ToolResult]` 的精确类型——是超集,字段名一致,可安全替换。

---

## Step 2 — 数据/评分函数去重(主文件 → 用 `data_sources` + `scoring`)

把真相源统一:主文件**删除**本地副本,改为从已有模块 import。

### 2a. 删除主文件中这些"数据抓取"重复函数(与 `data_sources.py` 完全一致)

第 222–567 行区间内:
`fetch_url, fetch_sec_url, parse_rss_date, categorize, headline_sentiment,
is_relevant_title, fetch_news, fetch_price_stats, parse_csv_rows, stooq_symbol,
fetch_stooq_price_stats, fetch_recent_sec_filings, fetch_sec_company_facts,
fact_units, latest_annual_value, latest_two_annual_values, latest_value`

以及顶部常量 `EVENT_KEYWORDS / NEGATIVE_WORDS / POSITIVE_WORDS`(62–106 行)
和 `SEC_TICKERS_URL / SEC_SUBMISSIONS_URL / SEC_COMPANY_FACTS_URL / SEC_USER_AGENT`(57–60 行)。

### 2b. 删除主文件中这些"评分"重复函数(与 `scoring.py` 完全一致)

`score_fundamentals, add_score, event_label, top_category_labels, count_categories,
build_reasons, build_watchpoints, score_deep_dive, price_sources_match,
build_data_confidence, apply_data_confidence, load_sec_ticker_map_safely,
apply_deep_dive, apply_fundamental_scores`

### 2c. 主文件保留并新增 import

`src/event_bottom_fishing.py` 改为:
```python
from data_sources import (
    fetch_news, fetch_price_stats,
    load_aliases as load_aliases_source,
    load_sec_ticker_map as load_sec_ticker_map_source,
    load_universe as load_universe_source,
)
from scoring import (
    add_score, build_reasons, build_watchpoints, count_categories, top_category_labels,
    apply_fundamental_scores, apply_deep_dive, apply_data_confidence,
    load_sec_ticker_map_safely,
)
```

保留在主文件的(编排层,这才是这个文件应有的内容):
- 薄包装 `load_universe(path)` / `load_aliases(path, universe)`(绑定 `DEFAULT_*` 路径常量)
- `prepare_selected_candidates`、`select_investable_candidates`
- `score_candidate`(一阶打分,只此一份,不在 `scoring.py`)
- `build_candidate`、`scan`、`main` 以及全部 `argparse` 定义
- 常量 `ROOT / DEFAULT_UNIVERSE / DEFAULT_ALIASES / DEFAULT_UNIVERSE_FALLBACK /
  DEFAULT_ALIASES_OVERRIDE / OUTPUT_DIR / DEFAULT_PAPER_PORTFOLIO_DB`

### 2d. 清理主文件 stdlib import

删除变为未使用的:`dataclasses, email.utils, html, json, re, time,
urllib.parse, urllib.request, xml.etree.ElementTree`。
保留:`argparse, concurrent.futures, datetime as dt, os, sys, typing.Iterable`。

### 2e. 等价性核对(逐函数已比对,均为逐字一致)

- `scoring.apply_fundamental_scores` ≡ 主文件版(都调 `data_sources.fetch_sec_company_facts` + `scoring.score_fundamentals`)。
- `scoring.apply_deep_dive` / `apply_data_confidence` ≡ 主文件版。
- 唯一差异:`scoring.apply_*` 用 `import time`(函数内),主文件用顶部 `time` — 行为一致。

> 完成 Step 1+2 后,`src/event_bottom_fishing.py` 预计 **1773 行 → ~300 行**,且不含 Step 3 的预留块。

---

## Step 3 — 预留 Agent 流水线(reserved,**不删除**,待你决策)

涉及主文件第 **969–1437 行**:
`clamp, evidence_quality, agent_news, agent_sec, agent_financial, agent_technical,
agent_sentiment, build_debate_result, build_risk_result, decide_agent_action,
build_agent_review, call_openai_review, apply_llm_overlay`。

**事实**:这是 `agent_runtime.py` 之外的**第二套** Agent 实现,当前不可达。两者并非等价:
- 主文件这版 `evidence_quality` 是 **6 因子加权**(credibility / primary_confirmation /
  consistency / independence / freshness / completeness),逻辑比在跑的 `agent_runtime`
  那版(`0.45 + 0.2/0.1/0.1/0.1` 简单加法)更细。
- 主文件版**没有** `tool_trace` / `agent_plan` 结构;`agent_runtime` 版有。

**问题**:作为"预留功能",它现在的形态有两个隐患——(1) 混在编排文件里,看不出是预留还是 live;
(2) 依赖主文件本地 dataclass,Step 1 删掉后它会编译失败。因此即使保留,也需要处理。

> 👉 **本节为待决策项**,候选方案见下;选定前不动这 470 行。

| 方案 | 做法 | 取舍 |
|---|---|---|
| **3-A 隔离保留(推荐)** | 整块移到新文件 `src/agent_review_deterministic.py`,加模块 docstring 标注"reserved / 备选确定性实现",并改为 `from models import ...`。主文件不再引用它。 | 预留意图清晰、主文件干净、可随时 wire-up;改动小 |
| **3-B 救活后合并** | 把更细的 6 因子 `evidence_quality` 等优点并入 `agent_runtime.py`,其余删除。 | 行为会变(evidence 数值变化),需重测;一次性收敛成一套 |
| **3-C 接成真·开关** | 保留并加 `--agent-impl legacy|runtime` 让它真正可达。 | 它才成为名副其实的"预留功能";工作量最大、要补测 |
| **3-D 原样不动** | 仅加注释标注,留在主文件。 | 主文件仍臃肿、仍与 live 路径易混;Step 1 需对它就地改 import |

无论选哪个,**Step 3 都必须让这块改用 `from models import AgentResult, AgentReview, Evidence, Candidate`**(否则与 Step 1 冲突)。

---

## Step 4 — 收敛剩余助手与命名分叉

- **`pct` / `multiple`**:统一一个真相源。推荐让 `llm_prompts.py` 改为 `from scoring import pct, multiple`
  (无循环依赖:`scoring → data_sources → models`,`llm_prompts` 不被它们 import)。
  Step 1+2 后主文件不再需要这两个,删掉其 `from llm_prompts import` 里的 `pct, multiple`。
  *(可选升级:抽一个 `src/formatting.py` 放 `pct/multiple/compact_text/estimate_tokens/markdown_escape`。)*
- **`event_label` 命名冲突**:`scoring.event_label` 返回小写叙述短语("earnings disappointment",
  用于 reasons/thesis 散文);`reporting.event_label` 返回标题式展示标签("Earnings miss",
  用于 MD 事件列表)。**用途不同,不应合并**。建议**改名消歧**(如 `reporting.event_display_label`),
  消除"同名=同义"的误解。这是改善可读性,非修 bug。

---

## Step 5 —(可选)防回归

README 的 "Testing Doctrine" 明确不追求单测。鉴于本仓库**无测试**且刚经历过静默分叉,
建议至少加一项端到端冒烟:
- 给数据源加 `--offline` / 注入式 mock,跑 `scan → write_outputs` 产出固定 fixture,
  断言 JSON 结构与关键字段存在。
- 目的:未来再出现"改了 `scoring.py` 却不生效"这类分叉时能被立即发现。

此步看你意愿,可不做。

---

## 执行顺序与验证

1. Step 1 → `py_compile` + 跑一次 `python3 src/event_bottom_fishing.py --top 3 --skip-agent-review`
   (确认数据类替换后产出不变)。
2. Step 2 → 同上;并 `python3 src/email_daily_report.py --help` 确认 `scan/write_outputs/DEFAULT_*` 仍可用
   (已核实 email 脚本只依赖这 5 个符号)。
3. Step 3 → 按所选方案执行;`full`/`lean` 路径各跑一次确认 agent 行为符合预期。
4. Step 4 → `py_compile` + 全量 import 自检。
5. 全程对比 `outputs/*.json` 的关键字段,确保非 Step 3 改动**零行为变化**。

**回归基线**:改动前先存一份当前 `outputs/daily_*.json` 作 diff 基准(用 `--skip-agent-review`
+ 固定 universe 以获得确定性输出)。

## 落地方式(待你确认)

- 改动落到哪:当前是 clone 副本 `/Users/Eric/event-first-stock-research`,非你的原仓库。
- 是否开分支 + 提 PR,还是直接在本地副本改给你看 diff。
- Step 3 选哪个方案(3-A 推荐)。
