# Module Structure

The code is an installable Python package, `efsr`, under a `src/` layout. Modules
are organized **by responsibility**, and the dependencies form an acyclic graph
(DAG): upper layers depend only on lower ones, and every concept has exactly one
home. This file is the source of truth for the structure (it matches the actual
imports under `src/efsr/`).

## Package layout

```
src/efsr/
  __init__.py            package metadata (__version__)
  __main__.py            enables `python -m efsr`
  cli.py                 pipeline/CLI orchestrator (entry point)
  email_report.py        SMTP send entry point
  models.py              shared dataclasses
  formatting.py          pct / multiple
  sources.py             universe, aliases, news, price, SEC retrieval + cache
  scoring.py             deterministic scoring (event screen, deep dive, fundamentals)
  prompts.py             prompt templates + token estimation
  reporting.py           Markdown / JSON rendering
  portfolio.py           paper validation ledger (SQLite)
  agents/
    __init__.py
    runtime.py           the live multi-agent committee
    legacy.py            alternative committee (--agent-impl legacy)
pyproject.toml           PEP 621 metadata + console scripts (efsr, efsr-email)
tests/test_smoke_offline.py
```

## Layer overview

```
  entry / orchestration   cli.py · __main__.py        email_report.py
                                  │
                                  ▼
  reason / render   agents/runtime.py · agents/legacy.py   reporting.py   portfolio.py
                                  │
                                  ▼
  score / prompt          scoring.py        prompts.py
                                  │
                                  ▼
  retrieval               sources.py  (I/O + cache)
                                  │
                                  ▼
  primitives (leaf)       models.py         formatting.py
```

## Dependency graph (actual import edges, within `efsr`)

```
formatting        ← scoring, prompts, reporting
models            ← sources, scoring, reporting, portfolio, agents.runtime, agents.legacy
sources           → models                            ← scoring, portfolio, agents.runtime
scoring           → sources, formatting, models       ← agents.runtime, agents.legacy, cli
prompts           → formatting                         ← agents.runtime, agents.legacy
reporting         → formatting, models                 ← cli, email_report
portfolio         → sources, models                    ← cli
agents.runtime    → sources, prompts, models, scoring
agents.legacy     → prompts, models, scoring           ← cli
cli               → agents.runtime, agents.legacy, sources, models, portfolio, reporting, scoring
email_report      → cli, reporting
```

`formatting` and `models` are leaves (no business-module dependencies), so any
layer can import them without creating a cycle.

## Module responsibilities and public surface

| Module | Lines | Responsibility | Key public API |
|---|---:|---|---|
| `formatting.py` | 15 | Pure numeric formatting (leaf, no deps) | `pct`, `multiple` |
| `models.py` | 151 | All shared dataclasses | `Candidate`, `NewsItem`, `PriceStats`, `FilingItem`, `DataConfidence`, `FundamentalScore`, `Evidence`, `AgentResult`, `ToolResult`, `AgentTask`, `AgentPlan`, `AgentReview` |
| `sources.py` | 660 | All external I/O + local cache + bounded fetch retries | `load_universe`, `load_aliases`, `fetch_news`, `fetch_price_stats`, `fetch_close_history`, `fetch_recent_sec_filings`, `fetch_sec_company_facts`, `EVENT_KEYWORDS` |
| `scoring.py` | 569 | **The single scoring home**: event screen, deep dive, fundamentals, data confidence | `score_candidate`, `score_fundamentals`, `score_deep_dive`, `build_data_confidence`, `apply_fundamental_scores`, `apply_deep_dive`, `apply_data_confidence`, `count_categories`, `event_label` (narrative phrase) |
| `prompts.py` | 120 | Prompt templates + token estimation | `build_llm_review_prompt`, `build_agent_task_prompt`, `compact_text`, `estimate_tokens`, `*_SYSTEM_PROMPT` |
| `agents/runtime.py` | 650 | **The live** multi-agent review (plan/tool trace, optional per-agent LLM) | `apply_agent_reviews`, `build_agent_review`, `build_agent_plan` |
| `agents/legacy.py` | 525 | Legacy committee (6-factor evidence, optional final overlay); `--agent-impl legacy` | `apply_agent_reviews_legacy`, `build_agent_review` |
| `reporting.py` | 335 | Render Markdown + JSON | `write_outputs`, `candidate_to_dict`, `event_display_label` (display label) |
| `portfolio.py` | 565 | Paper validation ledger (SQLite) + SPY benchmark / win rate | `apply_paper_buy`, `update_portfolio_performance`, `compute_benchmark`, `archive_report`, `append_*_to_outputs` |
| `cli.py` | 308 | Pipeline/CLI orchestration; dispatches to the runtime or legacy agent impl | `main`, `parse_args`, `build_arg_parser`, `scan`, `build_candidate`, `prepare_selected_candidates`, `apply_agent_reviews` (dispatcher) |
| `email_report.py` | 109 | SMTP send entry point (reuses `cli.parse_args`) | `generate_report`, `send_email`, `main` |

## Data flow (daily pipeline)

```
cli.parse_args
  → cli.scan                   sources.load_universe / load_aliases, fetch news+price concurrently
      → cli.build_candidate    per ticker: sources.fetch_news + fetch_price_stats + scoring.score_candidate
      → cli.prepare_selected_candidates
            scoring.apply_fundamental_scores   (SEC company facts)
            scoring.apply_deep_dive            (second-stage ranking + Focus eligibility)
            scoring.apply_data_confidence      (SEC filings + Stooq secondary price source)
            cli.apply_agent_reviews → agents.{runtime|legacy} multi-agent review
  → reporting.write_outputs    Markdown + JSON
  → portfolio                  buy (optional) / mark-to-market / archive
```

## "I want to add X — where does it go?"

| Need | Where |
|---|---|
| Change the universe (custom watchlist, etc.) | `sources.load_universe` |
| Add/adjust event keyword categories | `sources.EVENT_KEYWORDS` |
| Tune scoring weights / add a scoring dimension | `scoring.py` (**the single scoring home**; edits here take effect) |
| Add a new data source (transcripts, 8-K body…) | add a fetch function in `sources.py`, consume it in `scoring`/`agents` |
| Change agent reasoning / add a specialist | `agents/runtime.py` (live); the legacy committee is `agents/legacy.py` |
| Change prompts / token budget | `prompts.py` |
| Change report layout | `reporting.py` (does not affect the research conclusion) |
| Add a shared data field | `models.py` (used by every layer) |

## Entry points

```bash
pip install -e .                                    # editable install (stdlib-only)
python -m efsr --top 10                             # or the `efsr` console script
python -m efsr --top 10 --agent-impl legacy         # use the legacy committee
python -m efsr.email_report --to you@example.com    # or the `efsr-email` console script
pytest                                              # or: python3 tests/test_smoke_offline.py
```

## Quality gates

`.github/workflows/ci.yml` runs on every push/PR and enforces:

```bash
pyflakes src/efsr tests          # lint
mypy src/efsr --ignore-missing-imports   # type check (clean)
pytest                           # tests/test_smoke_offline.py + test_scoring.py + test_portfolio.py
```

The daily report workflow (`daily-stock-report.yml`) is separate and only
generates the watchlist. Diagnostics go through `logging` (level via `--verbose`);
network fetches retry with backoff and log a warning before falling back to cache.

## Design invariants (guarded by the refactor)

- **Single source of truth**: one home per responsibility; `score_candidate`
  lives only in `scoring`, `pct`/`multiple` only in `formatting`.
  (`tests/test_smoke_offline.py` asserts this with `is` checks to prevent the
  "edited but had no effect" silent-divergence trap from coming back.)
- **Acyclic dependencies**: upper layers depend on lower ones; the leaves are
  `models` / `formatting`.
- **Explainable behavior**: deterministic scoring is the guardrail, agent
  reasoning is an overlay, and reporting only explains — it never changes the
  conclusion.
