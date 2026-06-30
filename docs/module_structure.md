# Module Structure

After the refactor the code is organized **by responsibility**, and the
dependencies form an acyclic graph (DAG): upper layers depend only on lower
ones, and every concept has exactly one home. This file is the source of truth
for the structure (it matches the actual imports under `src/`).

## Layer overview

```
                 ┌──────────────────────────────────────────┐
  entry / orch.  │ event_bottom_fishing.py   email_daily_report.py
                 └──────────────────────────────────────────┘
                          │ assemble the pipeline, CLI, send
                          ▼
  reason / render  agent_runtime.py   agent_review_legacy.py   reporting.py      paper_portfolio.py
                   (live review)      (legacy review, switch)  (Markdown/JSON)   (paper ledger)
                          │
                          ▼
  score / prompt   scoring.py              llm_prompts.py
                   (deterministic scoring)  (prompt templates / tokens)
                          │
                          ▼
  retrieval        data_sources.py
                   (universe/news/price/SEC + cache)
                          │
                          ▼
  primitives (leaf) models.py              formatting.py
                   (dataclasses)            (pct / multiple)
```

## Dependency graph (actual import edges)

```
formatting          ← scoring, llm_prompts, reporting
models              ← data_sources, scoring, reporting, paper_portfolio, agent_runtime, agent_review_legacy
data_sources        → models                            ← scoring, paper_portfolio, agent_runtime
scoring             → data_sources, formatting, models  ← agent_runtime, agent_review_legacy, event_bottom_fishing
llm_prompts         → formatting                        ← agent_runtime, agent_review_legacy
reporting           → formatting, models                ← event_bottom_fishing, email_daily_report
paper_portfolio     → data_sources, models              ← event_bottom_fishing
agent_runtime       → data_sources, llm_prompts, models, scoring
agent_review_legacy → llm_prompts, models, scoring      ← event_bottom_fishing
event_bottom_fishing → agent_review_legacy, agent_runtime, data_sources, models, paper_portfolio, reporting, scoring
email_daily_report   → event_bottom_fishing, reporting
```

`formatting` and `models` are leaves (no business-module dependencies), so any
layer can import them without creating a cycle.

## Module responsibilities and public surface

| Module | Lines | Responsibility | Key public API |
|---|---:|---|---|
| `formatting.py` | 15 | Pure numeric formatting (leaf, no deps) | `pct`, `multiple` |
| `models.py` | 151 | All shared dataclasses | `Candidate`, `NewsItem`, `PriceStats`, `FilingItem`, `DataConfidence`, `FundamentalScore`, `Evidence`, `AgentResult`, `ToolResult`, `AgentTask`, `AgentPlan`, `AgentReview` |
| `data_sources.py` | 613 | All external I/O + local cache | `load_universe`, `load_aliases`, `load_sec_ticker_map`, `fetch_news`, `fetch_price_stats`, `fetch_stooq_price_stats`, `fetch_recent_sec_filings`, `fetch_sec_company_facts`, `EVENT_KEYWORDS` |
| `scoring.py` | 569 | **The single scoring home**: event screen, deep dive, fundamentals, data confidence | `score_candidate`, `score_fundamentals`, `score_deep_dive`, `build_data_confidence`, `apply_fundamental_scores`, `apply_deep_dive`, `apply_data_confidence`, `count_categories`, `event_label` (narrative phrase) |
| `llm_prompts.py` | 120 | Prompt templates + token estimation | `build_llm_review_prompt`, `build_agent_task_prompt`, `compact_text`, `estimate_tokens`, `*_SYSTEM_PROMPT` |
| `agent_runtime.py` | 650 | **The live** multi-agent review (with plan/tool trace, optional per-agent LLM) | `apply_agent_reviews`, `build_agent_review`, `build_agent_plan` |
| `agent_review_legacy.py` | 525 | Legacy agent committee (6-factor evidence, optional final OpenAI overlay); selected via `--agent-impl legacy` | `apply_agent_reviews_legacy`, `build_agent_review` |
| `reporting.py` | 335 | Render Markdown + JSON | `write_outputs`, `candidate_to_dict`, `event_display_label` (display label) |
| `paper_portfolio.py` | 507 | Paper validation ledger (SQLite) | `apply_paper_buy`, `update_portfolio_performance`, `archive_report`, `append_*_to_outputs` |
| `event_bottom_fishing.py` | 308 | Pipeline/CLI orchestration; dispatches to the runtime or legacy agent impl | `main`, `parse_args`, `build_arg_parser`, `scan`, `build_candidate`, `prepare_selected_candidates`, `apply_agent_reviews` (dispatcher) |
| `email_daily_report.py` | 109 | SMTP send entry point (reuses `parse_args`) | `generate_report`, `send_email`, `main` |

> The legacy committee lives in its own `agent_review_legacy.py` (selected via
> `--agent-impl legacy`; the default is `agent_runtime`), so
> `event_bottom_fishing.py` stays a thin orchestrator.

## Data flow (daily pipeline)

```
parse_args
  → scan                       data_sources.load_universe / load_aliases, fetch news+price concurrently
      → build_candidate        per ticker: fetch_news + fetch_price_stats + scoring.score_candidate
      → prepare_selected_candidates
            scoring.apply_fundamental_scores   (SEC company facts)
            scoring.apply_deep_dive            (second-stage ranking + Focus eligibility)
            scoring.apply_data_confidence      (SEC filings + Stooq secondary price source)
            apply_agent_reviews → [runtime|legacy] multi-agent review
  → reporting.write_outputs    Markdown + JSON
  → paper_portfolio            buy (optional) / mark-to-market / archive
```

## "I want to add X — where does it go?"

| Need | Where |
|---|---|
| Change the universe (custom watchlist, etc.) | `data_sources.load_universe` |
| Add/adjust event keyword categories | `data_sources.EVENT_KEYWORDS` |
| Tune scoring weights / add a scoring dimension | `scoring.py` (**the single scoring home**; edits here take effect) |
| Add a new data source (transcripts, 8-K body…) | add a fetch function in `data_sources.py`, consume it in `scoring`/`agent_runtime` |
| Change agent reasoning / add a specialist | `agent_runtime.py` (live); the legacy committee is `agent_review_legacy.py` |
| Change prompts / token budget | `llm_prompts.py` |
| Change report layout | `reporting.py` (does not affect the research conclusion) |
| Add a shared data field | `models.py` (used by every layer) |

## Entry points

```bash
python3 src/event_bottom_fishing.py --top 10                     # generate the report (default: runtime agent)
python3 src/event_bottom_fishing.py --top 10 --agent-impl legacy # use the legacy committee
python3 src/email_daily_report.py --to you@example.com           # generate and send via SMTP
python3 tests/smoke_offline.py                                   # offline smoke test (no network/key/DB)
```

## Design invariants (guarded by the refactor)

- **Single source of truth**: one home per responsibility; `score_candidate`
  lives only in `scoring`, `pct`/`multiple` only in `formatting`.
  (`tests/smoke_offline.py` asserts this with `is` checks to prevent the
  "edited but had no effect" silent-divergence trap from coming back.)
- **Acyclic dependencies**: upper layers depend on lower ones; the leaves are
  `models` / `formatting`.
- **Explainable behavior**: deterministic scoring is the guardrail, agent
  reasoning is an overlay, and reporting only explains — it never changes the
  conclusion.
