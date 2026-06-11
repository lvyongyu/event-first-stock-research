# Multi-Agent Research Pipeline Design

## Goal

Upgrade the current event-driven bottom-fishing screener into a research pipeline that behaves more like a small investment research desk.

The system should not try to predict stock prices directly. Its value should come from:

- Collecting evidence from multiple sources
- Separating source credibility from market excitement
- Reading primary documents before trusting secondary commentary
- Debating bull and bear cases
- Penalizing structural risk before producing a focus list
- Producing an auditable research note, not an opaque signal

The output remains a research watchlist, not investment advice or an auto-trading instruction.

## Target Architecture

```text
News Agent
SEC Filing Agent
Financial Agent
Reddit Agent
Technical Agent
      |
      v
Debate Agent
      |
      v
Risk Agent
      |
      v
Trade Agent
```

## Agent Responsibilities

### News Agent

Purpose: detect fresh market-moving events.

Inputs:

- Yahoo Finance RSS in the current version
- Future: Reuters, Benzinga, MarketWatch, company press releases

Outputs:

- Event category
- Event freshness
- Headline sentiment
- Company-specific vs macro/sector relevance
- News consistency score
- Duplicate/news-noise warning

Example output:

```json
{
  "agent": "news",
  "stance": "mixed_positive",
  "score": 18,
  "confidence": 0.55,
  "evidence": [
    "Analyst downgrade created selloff catalyst",
    "Positive analyst follow-up suggests debate is not one-sided"
  ],
  "concerns": [
    "Some headlines are commentary rather than primary evidence"
  ]
}
```

### SEC Filing Agent

Purpose: verify whether the event has primary-source support.

Inputs:

- SEC company submissions
- SEC company facts
- Future: 8-K item extraction, 10-Q/10-K risk factor diffing

Outputs:

- Recent relevant filing list
- Primary-source confirmation
- Red flags from filings
- Whether the issue is temporary, cyclical, or structural

Priority:

SEC evidence should outrank all secondary news. If Yahoo says a company has a major issue but SEC filings do not support it, confidence should be capped.

### Financial Agent

Purpose: judge whether the company is worth bottom-fishing.

Inputs:

- SEC company facts
- Existing price data
- Future: quarterly statements, analyst estimates, peer comparisons

Outputs:

- Business Quality Score
- Valuation Score
- Structural Risk Penalty
- Key metrics:
  - revenue growth
  - net margin
  - free-cash-flow margin
  - liabilities/assets
  - P/S
  - P/E
  - FCF yield

This agent prevents low-quality companies from entering the focus list just because they dropped.

### Reddit Agent

Purpose: measure retail sentiment and narrative intensity.

Inputs:

- Future: Reddit API or Pushshift-style source if available
- Subreddits such as stocks, investing, wallstreetbets, security-specific communities

Outputs:

- Mention velocity
- Positive/negative sentiment
- Narrative crowding
- Meme-risk flag
- Contrarian signal if sentiment is extremely one-sided

Important rule:

Reddit should have low source credibility by default. It can improve awareness of crowd behavior, but it should not override SEC or financial evidence.

### Technical Agent

Purpose: judge whether the setup is stabilizing or still falling.

Inputs:

- Daily prices
- Volume
- Future: intraday VWAP, relative strength, sector ETF comparison

Outputs:

- Drawdown
- 5-day stabilization
- 20-day selloff
- Volume expansion
- Falling-knife warning
- Relative strength vs SPY/QQQ/sector ETF

Technical evidence should influence timing, not business quality.

## Debate Agent

Purpose: combine agent outputs into bull and bear cases.

Inputs:

- All agent signals
- Source credibility weights
- Evidence quality score

Outputs:

- Bull case
- Bear case
- Disagreement summary
- Debate score
- Questions to verify next

Example:

```text
Bull case:
- Event appears tied to guidance rather than permanent demand destruction.
- Business quality remains high.
- Valuation has improved after the selloff.

Bear case:
- Price has not stabilized.
- Analyst downgrade may reflect slower structural growth.
- Data confidence is only Medium.
```

## Risk Agent

Purpose: block weak or dangerous setups from becoming focus candidates.

Inputs:

- Debate Agent output
- Structural Risk Penalty
- Legal/terminal-risk events
- Data Confidence
- Technical falling-knife status

Outputs:

- Risk rating: Low / Medium / High / Blocked
- Position-sizing suggestion for research notes only
- Reasons a candidate cannot be Focus

Hard blocks:

- bankruptcy
- fraud/accounting irregularity
- delisting
- severe liquidity issue
- high structural risk with weak business quality
- data confidence too low for the claimed thesis

## Trade Agent

Purpose: produce a final research action.

This should not place trades. It should classify the candidate:

- `Focus`: worth serious manual research now
- `Watch`: monitor, but not top priority
- `Pass`: not worth time this cycle
- `Blocked`: risk/data quality makes the thesis unreliable

Outputs:

- Final action
- Trade score
- Research rationale
- Invalidation level or condition
- Next verification event

Example:

```text
Action: Watch
Reason:
- Event is real and valuation improved.
- Business quality is acceptable.
- But technical stabilization is weak and data confidence is Low.
```

## Dynamic Quality Multiplier

The quality multiplier should not be a fixed constant.

It should be calculated from:

```text
Source credibility
News quantity
News consistency
Primary-source confirmation
Data freshness
Historical source hit rate
```

### Source Credibility

Initial weights:

```text
SEC filings:          1.00
Company reports:      0.90
Earnings transcripts: 0.85
Reuters/Bloomberg:    0.80
Major financial news: 0.65
Yahoo RSS headlines:  0.45
Reddit/social:        0.20
```

### News Quantity

More articles are not always better. The score should increase with independent corroboration, but penalize duplicate or syndicated headlines.

Suggested formula:

```text
quantity_score = min(unique_company_specific_events, 5) / 5
```

### News Consistency

Measure whether sources agree on direction:

```text
positive_count
negative_count
mixed_count
```

High consistency:

- Most credible sources point in the same direction

Low consistency:

- Headlines are contradictory
- Commentary conflicts with filings

### Historical Hit Rate

Later phase.

Track whether signals from each source historically produced useful focus candidates.

Example:

```text
source_hit_rate = successful_focus_outcomes / total_focus_outcomes
```

This can eventually tune credibility weights automatically.

### Proposed Evidence Quality Score

```text
evidence_quality =
  0.40 * source_credibility
+ 0.20 * primary_source_confirmation
+ 0.15 * news_consistency
+ 0.10 * unique_event_quantity
+ 0.10 * data_freshness
+ 0.05 * historical_hit_rate
```

Until historical data exists, set `historical_hit_rate = 0.5`.

## Final Scoring Model

The current model should evolve from:

```text
event score + deep dive score
```

to:

```text
trade_score =
  event_opportunity_score
+ technical_stabilization_score
+ business_quality_score
+ valuation_score
+ debate_score
- structural_risk_penalty
- legal_terminal_risk_penalty
- falling_knife_penalty

trade_score = trade_score * evidence_quality_multiplier
```

Where:

```text
evidence_quality_multiplier = 0.70 + 0.60 * evidence_quality
```

This creates a multiplier range of `0.70` to `1.30`.

Low-quality evidence dampens a candidate. High-quality corroborated evidence can lift it.

## Output Design

The report should start with:

```text
Daily Deep Dive Shortlist

Rank | Ticker | Action | Trade Score | Evidence Quality | Risk | Main Bull Case | Main Bear Case
```

Each candidate should include:

```text
Agent Committee Summary
- News Agent
- SEC Filing Agent
- Financial Agent
- Reddit Agent
- Technical Agent

Debate
- Bull case
- Bear case
- Open questions

Risk Review
- Main risks
- Hard blocks
- Invalidation conditions

Trade Agent
- Action
- Trade score
- Why Focus/Watch/Pass
```

## Implementation Plan

### Phase 1: Internal Agent Abstraction

No new external APIs.

- Add `AgentSignal`
- Add `AgentCommittee`
- Convert current logic into agent-style outputs:
  - News Agent from current RSS categories
  - SEC Agent from recent filings and company facts
  - Financial Agent from current fundamental scoring
  - Technical Agent from current price stats
  - Reddit Agent as unavailable/neutral placeholder
- Add Debate/Risk/Trade aggregation
- Report agent table in Markdown and JSON

### Phase 2: Better Primary Documents

- Download and parse recent 8-K text
- Add 10-Q/10-K risk factor diffing
- Add earnings release detection
- Add transcript ingestion if a reliable source is available

### Phase 3: Reddit Agent

- Add Reddit ingestion
- Score mention velocity and sentiment
- Add meme/crowding risk
- Keep Reddit low credibility unless backtested

### Phase 4: Backtesting and Historical Hit Rate

- Store daily outputs
- Track forward returns and drawdowns
- Learn source hit rates
- Tune evidence quality weights

### Phase 5: Trade Workflow

Still no auto-trading by default.

- Add paper-trade mode
- Add watchlist state
- Add invalidation alerts
- Add position sizing suggestions for manual review

## Design Principles

- Primary evidence beats commentary.
- Quality and structural risk gate Focus decisions.
- Reddit can inform sentiment but must not dominate.
- Every final score must be explainable.
- Every Focus candidate must include a bear case.
- The system should prefer saying `Watch` over forcing a trade idea.

