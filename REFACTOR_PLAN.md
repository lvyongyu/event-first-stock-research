# Refactor Plan — event-first-stock-research

> Goal: finish a half-done modularization. The clean modules
> (`models / data_sources / scoring / agent_runtime / reporting / paper_portfolio`)
> already existed and were running, but `src/event_bottom_fishing.py` still
> carried the entire pre-refactor implementation in parallel. This work slims the
> main file down to a pipeline/CLI orchestrator, removes the duplication and the
> silent-divergence traps, and keeps the reserved agent committee as a real,
> switchable implementation.
>
> Status: **implemented (Steps 1–6 complete)**. Net −865 lines of code;
> `py_compile` + `pyflakes` clean across `src/`; `score_candidate` relocation is
> field-for-field equivalent; default behavior (`--agent-impl runtime`) is
> unchanged, so CI and the daily report are unaffected.
>
> Progress:
> - ✅ Step 1 — dataclass dedup → `models`
> - ✅ Step 2 — data/scoring function dedup → `data_sources`/`scoring`; relocate `score_candidate` into `scoring`
> - ✅ Step 3 = **3-C**: `--agent-impl runtime|legacy` switch + `apply_agent_reviews_legacy`
>   orchestrator, wiring the reserved committee into a real switchable implementation (default runtime)
> - ✅ Step 4 — converge `pct`/`multiple` into leaf module `formatting.py`;
>   rename `reporting.event_label` → `event_display_label`; clean unused imports + dead local in `agent_runtime.py`
> - ✅ Step 5 — offline smoke test `tests/test_smoke_offline.py` (single source of truth /
>   pinned scores / both agent impls + serialization / routing)
> - ✅ Step 6 — packaging to convention: `src/efsr/` package (layered, with an `agents/`
>   subpackage), `pyproject.toml` (PEP 621 metadata + `efsr`/`efsr-email` console scripts),
>   `python -m efsr` entry point, absolute `efsr.*` imports, CI/README updated, tests run via `pytest`.
>   Module renames: `data_sources`→`sources`, `llm_prompts`→`prompts`, `paper_portfolio`→`portfolio`,
>   `event_bottom_fishing`→`cli`, `email_daily_report`→`email_report`, `agent_runtime`→`agents.runtime`,
>   `agent_review_legacy`→`agents.legacy`.
> - ➕ Extra: fixed the `email_daily_report.py` SMTP entry point (extract
>   `build_arg_parser`/`parse_args` to share one arg source; its hand-built
>   `SimpleNamespace` had drifted — missing `scan_workers`/`skip_agent_review`/`agent_*` —
>   plus a call to a non-existent `event_bottom_fishing.write_outputs`)

See [docs/module_structure.md](docs/module_structure.md) for the resulting layering.

---

## 0. Starting situation (verified)

- `python3 -m py_compile src/*.py` passed; the program ran; CI produced output.
- 34 of the main file's 54 functions were exact duplicates of `data_sources.py` / `scoring.py`.
- All 9 dataclasses in the main file duplicated `models.py`.
- `EVENT_KEYWORDS / NEGATIVE_WORDS / POSITIVE_WORDS` were duplicated in the main file and `data_sources.py`.
- `pct` / `multiple` existed in 3 places (`llm_prompts.py` / `scoring.py` / a main-file import).
- `event_label` existed in both `scoring.py` and `reporting.py` with **different return values**.
- ~470 lines of an agent pipeline in the main file (`build_agent_review`, `agent_news` … `apply_llm_overlay`)
  were unreachable from the entry point: `apply_agent_reviews` delegated to `agent_runtime.run_agent_reviews`.
  This was confirmed to be a **reserved** feature and was handled per Step 3 (not deleted).

### Source of truth (what actually ran, before)

| Capability | Implementation that ran | Duplicate / old copy in the main file |
|---|---|---|
| Data fetch (news/price/SEC) | main-file local copies (live) + `data_sources.py` (used by agent_runtime) | both present |
| Scoring (fundamentals/deep_dive) | **main-file local copies (live)** | `scoring.py` same-named functions were unreachable |
| Agent review | **`agent_runtime.py` (live)** | main-file ~470 lines (reserved) |
| Reporting / paper portfolio | `reporting.py` / `paper_portfolio.py` (live) | — |

> Counter-intuitive trap: `scoring.py`'s large functions (`score_fundamentals`,
> `apply_deep_dive`, `score_deep_dive`, `build_reasons`…) had **no callers** — the
> running code used the main-file local copies. Editing `scoring.py`'s scoring
> logic had **no effect**. This refactor unifies the source of truth in `scoring.py`.

---

## Step 1 — dataclass dedup (main file → use `models`)

Motivation: the main file built `event_bottom_fishing.Candidate` but passed it to
functions annotated `models.Candidate`, working only by duck typing; adding a field
to `models` could raise `AttributeError` at any time.

Deleted the 9 dataclasses from `event_bottom_fishing.py`
(`NewsItem, PriceStats, FilingItem, DataConfidence, FundamentalScore, Evidence,
AgentResult, AgentReview, Candidate`) and imported them from `models`.

---

## Step 2 — data/scoring function dedup (main file → `data_sources` + `scoring`)

Unified the source of truth: the main file deleted its local copies and imports
from the existing modules instead.

- **2a.** Removed the duplicated fetch helpers (`fetch_url`, `fetch_news`,
  `fetch_price_stats`, `fetch_recent_sec_filings`, `fetch_sec_company_facts`,
  `fact_units`, `latest_*`, …) and the `EVENT_KEYWORDS`/etc. constants.
- **2b.** Removed the duplicated scoring helpers (`score_fundamentals`, `add_score`,
  `event_label`, `top_category_labels`, `count_categories`, `build_reasons`,
  `build_watchpoints`, `score_deep_dive`, `price_sources_match`,
  `build_data_confidence`, `apply_data_confidence`, `load_sec_ticker_map_safely`,
  `apply_deep_dive`, `apply_fundamental_scores`).
- **Relocate:** moved the first-pass `score_candidate` into `scoring.py` so scoring
  has a single home. (Verified field-for-field equivalent to the original.)
- The main file kept only the orchestration layer: thin `load_universe`/`load_aliases`
  wrappers, `prepare_selected_candidates`, `select_investable_candidates`,
  `build_candidate`, `scan`, `main`, and the CLI.

Result: `event_bottom_fishing.py` went from 1773 lines to a thin orchestrator plus
the fenced legacy block.

---

## Step 3 — reserved agent pipeline → switchable (decision: 3-C)

The ~470-line reserved committee is a second, self-contained implementation of the
same agents as `agent_runtime.py`. The two are **not** equivalent:

| | reserved (legacy) | live (`agent_runtime`) |
|---|---|---|
| evidence_quality | **6-factor weighted** | `0.45 + 0.2/0.1/0.1/0.1` additive |
| tool_trace / agent_plan | none | **present** (auditable trace) |
| LLM involvement | final overlay only | optional **per-agent** (full mode) |

**3-C implemented:** added a `--agent-impl runtime|legacy` flag (also `AGENT_IMPL`),
default `runtime`. `apply_agent_reviews` became a dispatcher; a new
`apply_agent_reviews_legacy` orchestrator runs the reserved committee (build review
+ optional final OpenAI overlay). The reserved block now imports its dataclasses from
`models` and is fenced under a clear banner at the bottom of the main file.

---

## Step 4 — converge helpers and disambiguate names

- **`pct` / `multiple`**: moved into a new dependency-free leaf module `formatting.py`;
  `scoring`, `llm_prompts`, and `reporting` all import from it. (`reporting` no longer
  depends on `scoring` just for these.)
- **`event_label` name collision**: `scoring.event_label` returns narrative phrases
  ("earnings disappointment", used in reasons/thesis prose); `reporting.event_label`
  returned display labels ("Earnings miss"). These are different by intent, so the
  reporting one was renamed to `event_display_label` rather than merged.
- **`agent_runtime.py`**: removed unused imports (`compact_text`, `scoring.pct`,
  `scoring.multiple`) and a dead local (`task_tool_names`).

---

## Step 5 — regression guard

`tests/smoke_offline.py` (offline, no API key, does not touch the paper portfolio DB):
- asserts the single source of truth (`event_bottom_fishing.score_candidate is
  scoring.score_candidate`; `pct`/`multiple` share `formatting.py`) — directly catching
  re-duplication / silent divergence;
- pins the deterministic event score (catches accidental weight changes);
- runs the legacy committee and serializes it through `reporting`;
- checks `--agent-impl` routing.

Run: `python3 tests/smoke_offline.py`

---

## Verification performed

- `py_compile` + `pyflakes` clean across `src/`.
- `score_candidate` relocation verified field-for-field equivalent against the original.
- Live end-to-end run (20-ticker temp universe, temp DB): produced a valid report;
  both `--agent-impl runtime` and `legacy` ran live (legacy: 6-factor evidence, no tool
  trace; runtime: tool trace). The paper-portfolio DB was not touched.
- Default runtime behavior and the GitHub-Issue daily report path are unchanged.
