# Event-First US Stock Bottom-Fishing Agent

This project is a daily research engine for US stocks that sold off after real events.
It is not an auto-trader and not investment advice.
Its job is narrower and more useful: surface the 10 names most worth human attention, explain why they made the list, and narrow that list again into a smaller deep-dive shortlist.

## Manifesto

This codebase is built around a simple belief:

- the best trading system starts as a research system
- event context matters more than generic factor soup
- explanations matter as much as rankings
- every output should show its work
- the cheapest LLM call is the one you do not need to make

The workflow is intentionally opinionated:

1. Find event-driven names with a real catalyst.
2. Reject obvious terminal-risk cases.
3. Score the remaining names with light structure, not a giant black box.
4. Run a second-stage deep dive to narrow the list.
5. Optionally add a multi-agent LLM layer for richer reasoning.
6. Write a human-readable report and a JSON artifact for automation.

## What It Looks At

The screener focuses on:

- Recent Yahoo Finance news events
- Recent price drawdown and early stabilization
- Whether the event looks recoverable or structurally broken
- Whether the setup is noisy macro flow or company-specific
- Recent SEC filings as a primary-source cross-check
- Stooq daily prices when available as a second price source

The model intentionally avoids a broad multi-factor ranking system. It is built for event-first bottom fishing.

## Output

Every run writes Markdown and JSON files into `outputs/`.

The report includes:

- Top 10 event-driven candidates
- A deep-dive shortlist that narrows the list to 2-3 names
- Business quality, valuation, and structural risk scores
- A `Data Confidence` rating
- A transparent score breakdown
- A rationale section for each candidate
- AI agent review notes, committee summaries, and tool traces when enabled

## Install

The project is a standard-library-only Python package. Install it (editable) to
get the `efsr` package and the `efsr` / `efsr-email` console scripts on your path:

```bash
pip install -e .
```

## Run

```bash
python -m efsr          # equivalent to the `efsr` console script
```

## Agent Modes

The agent layer has three modes:

- `deterministic`: no LLM calls
- `lean`: one compact final synthesis per reviewed candidate
- `full`: LLM support for each agent step plus the final synthesis

By default:

- if `OPENAI_API_KEY` is set, the script runs in `lean`
- otherwise it runs in `deterministic`

The live S&P 500 universe and SEC company-name lookup are cached locally in `.cache/event-first/` and refreshed once a week. GitHub Actions restores the same cache directory between runs.

After each report run, the project also updates a SQLite paper portfolio at `state/paper_portfolio.sqlite`. It simulates buying `$100` of the highest-ranked new report candidate and never buys the same ticker twice. The DB archives each daily report and stores daily mark-to-market snapshots, so users can see open positions, unrealized P/L, and one-month or two-month holding performance as history accumulates. GitHub Actions commits the evolving research history back to the repository. This is a validation ledger for judging the research engine over time, not real trading.

To run the full multi-agent version:

```bash
OPENAI_API_KEY=... python -m efsr \
  --top 10 \
  --agent-mode full \
  --agent-review-count 3 \
  --agent-token-budget 900 \
  --agent-max-output-tokens 350
```

Token controls:

- `--agent-mode` selects `deterministic`, `lean`, or `full`
- `--agent-review-count` limits how many top candidates receive agent review
- `--agent-token-budget` caps the compact prompt budget per candidate
- `--agent-max-output-tokens` caps response length

The prompts do not send raw article text or full filings. They use compressed event, SEC, financial, technical, debate, and risk summaries.

## Testing Doctrine

This project does not try to prove itself with a wall of unit tests.
The important failure modes here are not tiny pure functions. They are:

- broken source data
- stale prompt assumptions
- bad report assembly
- fragile external integrations
- changed runtime behavior in the live pipeline

So the testing style is intentionally practical:

- run the full workflow end to end
- verify the live data sources still respond
- inspect the generated Markdown and JSON
- catch prompt or integration regressions early

That is the right tradeoff for a system whose output is a research report, not a library API.

## Daily Use

Run it once per trading day before the US market open:

```bash
python -m efsr --top 10
```

To generate the report and email it from your own machine or any SMTP-enabled environment:

```bash
python -m efsr.email_report --to lvyongyu@gmail.com   # or: efsr-email --to ...
```

Email settings are loaded from environment variables or a local `.env` file. Start from the example:

```bash
cp config/email.env.example .env
```

For Gmail, set `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, and use a Gmail app password as `SMTP_PASSWORD`. The `.env` file is ignored by Git.

## GitHub Actions Schedule

The repository includes `.github/workflows/daily-stock-report.yml`.

It runs Monday through Friday at 13:00 UTC, which is before the US market open in both daylight-saving and standard-time periods. It does not run on weekends. You can also run it manually from the GitHub Actions tab with `workflow_dispatch`.

By default, the GitHub workflow does not need an SMTP server. It generates the report and creates a GitHub Issue with the full watchlist. If your GitHub notifications are enabled, GitHub will email you when the issue is created.

To receive it by email, make sure you are watching the repository:

```text
Repository -> Watch -> All Activity
```

Also confirm GitHub email notifications are enabled:

```text
GitHub -> Settings -> Notifications -> Email
```

## Ranking Idea

The first-stage score favors stocks that:

- have meaningful negative or mixed events recently
- dropped enough to be interesting
- show early stabilization rather than continued free-fall
- do not have obvious terminal-risk language

After the first-stage top 10 is selected, a second-stage deep dive narrows the list to 2-3 focus candidates. It favors stocks where:

- the event is tied to something verifiable, such as earnings, guidance, revenue, margin, or analyst revisions
- the drawdown is meaningful but not so extreme that it likely signals structural damage
- the stock shows at least early stabilization from its 5-day low
- the risk is bounded enough to investigate, instead of being dominated by legal, delisting, fraud, or bankruptcy language
- there are multiple company-specific headlines rather than only broad macro noise
- business quality is not weak, based on revenue growth, margin, free cash flow, and balance-sheet leverage
- valuation has some support, based on SEC-derived P/S, P/E, and FCF yield approximations
- structural risk is not high enough to block a focus designation

The AI agent layer then overlays:

- News Agent: explains the event narrative and headline credibility
- SEC Filing Agent: checks whether primary filings are present and what is still missing
- Financial Agent: judges business quality, valuation, and structural risk
- Technical Agent: checks stabilization and falling-knife risk
- Sentiment Agent: currently marks social sentiment as unavailable until a real source is added
- Debate Agent: summarizes bull and bear cases
- Risk Agent: can downgrade or block a candidate

The AI agent review is still a research workflow, not an investment recommendation.

The output classes are:

- `A`: High-priority research candidate
- `B`: Watchlist
- `C`: Weak or noisy event
- `D`: Avoid or review only because the event risk is too severe

Always read the actual event context before doing anything with real money.

## Configuration

By default the screener uses live S&P 500 membership and auto-generated company aliases.
If live membership cannot be fetched, it falls back to `config/universe_sp100.txt`.
If a company name needs a manual patch, use `config/company_aliases.json` as the override layer.
