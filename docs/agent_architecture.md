# AI Agent Research System Design

## Goal

Build an AI-agent-first research system for US stock bottom-fishing candidates.

The current screener can still provide the initial candidate pool, but the professional version should not be a rules engine with agent names attached. The core system should be a group of AI research agents that can investigate, reason, challenge each other, and produce an auditable watchlist.

The system should not predict stock prices directly. Its value should come from:

- Reading primary evidence before trusting commentary
- Comparing multiple data sources
- Explaining why a selloff may be temporary or structural
- Separating good businesses on sale from weak businesses getting weaker
- Producing bull and bear cases
- Blocking candidates with unacceptable risk
- Creating a research note that a human can inspect

The output remains a research watchlist, not investment advice and not an automatic trade instruction.

## Core Position

This project should be designed as an AI agent system from the start.

Rules, deterministic scores, and data loaders still matter, but they should be tools used by agents, not the main architecture.

```text
AI agents decide what question to answer.
Tools provide data and deterministic calculations.
Guardrails constrain unsafe or low-quality conclusions.
Reports expose the reasoning and evidence.
```

This avoids building a fixed scoring pipeline first and later trying to retrofit autonomy into it.

## High-Level Architecture

```text
Candidate Generator
        |
        v
Orchestrator Agent
        |
        +--> News Agent
        +--> SEC Filing Agent
        +--> Financial Agent
        +--> Technical Agent
        +--> Sentiment Agent
        |
        v
Debate Agent
        |
        v
Risk Agent
        |
        v
Trade Review Agent
        |
        v
Daily Research Report
```

The candidate generator can be the existing event-driven bottom-fishing screener. It narrows the market to a manageable list, such as 10 stocks per day.

The AI agents then perform deeper research on those candidates and reduce the list to 2-3 stocks worth serious manual review.

## What Makes These AI Agents

Each agent should have:

- A mission, not just a formula
- Access to a defined toolset
- A structured output schema
- Evidence requirements
- Confidence calibration
- Counterarguments
- A list of missing evidence

An agent should not merely produce a score. It should answer a research question.

Example:

```text
Bad:
SEC Agent returns recent filing count.

Good:
SEC Agent determines whether the selloff is supported by new primary disclosures,
whether the issue looks temporary or structural, and what evidence is missing.
```

## Orchestrator Agent

Purpose: plan and coordinate the research process for each candidate.

Main question:

```text
What needs to be investigated before this stock can be classified as Focus, Watch, Pass, or Blocked?
```

Responsibilities:

- Read the initial screener output
- Identify the event thesis for each candidate
- Assign subtasks to specialist agents
- Decide which tools are needed
- Track missing evidence
- Ask for follow-up analysis when an agent result is weak or contradictory
- Produce a complete research packet for Debate Agent

Example task:

```json
{
  "ticker": "ADBE",
  "research_goal": "Determine whether the drawdown is an earnings reset or a structural growth concern.",
  "required_agents": ["news", "sec_filing", "financial", "technical"],
  "optional_agents": ["sentiment"],
  "priority_questions": [
    "Was the selloff caused by guidance, margins, AI disruption, or macro pressure?",
    "Do filings or earnings materials confirm a permanent deterioration?",
    "Is valuation now reasonable relative to growth quality?"
  ]
}
```

## News Agent

Purpose: understand the market narrative and event catalyst.

Main question:

```text
What event caused or explains the selloff, and how credible is the narrative?
```

Tools:

- News RSS/search tool
- Company press release tool
- Source deduplication tool
- Event classifier

Outputs:

- Main catalyst
- Narrative summary
- Source list
- Source credibility
- Whether coverage is independent or syndicated
- Bullish interpretation
- Bearish interpretation
- Missing primary evidence

Important rule:

News can explain market reaction, but it should not prove business quality. If news claims a major structural issue, the SEC Filing Agent and Financial Agent must verify it.

## SEC Filing Agent

Purpose: read primary disclosures and determine whether the market narrative is supported by company filings.

Main question:

```text
Does primary company disclosure show temporary pressure, structural deterioration, legal risk, accounting risk, or no clear confirmation?
```

Tools:

- SEC submissions API
- SEC company facts API
- 8-K parser
- 10-Q / 10-K section extractor
- Risk factor diff tool
- Filing quote extractor

Outputs:

- Relevant filings
- Filing-based thesis
- New risks
- Changed risk language
- Accounting or liquidity concerns
- Evidence quotes or evidence IDs
- Confidence
- Missing filings or documents

This agent should carry high authority. SEC evidence should outweigh headlines, Reddit, and broad market commentary.

## Financial Agent

Purpose: decide whether the company is fundamentally worth researching as a bottom-fishing candidate.

Main question:

```text
Is this a good or improving business whose valuation has become more interesting, or a weak business that merely got cheaper?
```

Tools:

- SEC company facts tool
- Financial metrics calculator
- Valuation calculator
- Peer comparison tool
- Historical growth and margin tool

Outputs:

- Business quality conclusion
- Valuation conclusion
- Growth and margin summary
- Balance sheet concerns
- Structural risk view
- Peer-relative context
- Confidence
- Missing financial data

This agent should prevent names like low-quality cyclicals, structurally impaired businesses, or balance-sheet-stressed companies from ranking too high just because they dropped.

## Technical Agent

Purpose: judge timing and stabilization, not business quality.

Main question:

```text
Is the stock showing signs of stabilization, or is it still a falling knife?
```

Tools:

- Daily price tool
- Volume tool
- Relative strength tool
- Drawdown calculator
- Moving average and stabilization calculator

Outputs:

- Drawdown context
- Short-term stabilization
- Volume confirmation
- Relative strength vs SPY / QQQ / sector ETF
- Falling-knife warning
- Timing confidence

Technical evidence should affect urgency and timing. It should not turn a low-quality business into a Focus candidate.

## Sentiment Agent

Purpose: measure crowd narrative and retail positioning risk.

Main question:

```text
Is public sentiment creating opportunity, crowding risk, or noise?
```

Tools:

- Reddit ingestion tool
- Social mention velocity tool
- Sentiment classifier
- Narrative clustering tool

Outputs:

- Mention velocity
- Dominant retail narratives
- Sentiment direction
- One-sidedness
- Meme or crowding risk
- Contrarian signal, if any

Important rule:

Sentiment has low source credibility by default. It can flag crowd behavior, but it should not override SEC filings, financials, or clear risk evidence.

## Debate Agent

Purpose: force the research to confront both sides.

Main question:

```text
What is the strongest bull case, what is the strongest bear case, and what evidence would change the conclusion?
```

Inputs:

- All specialist agent results
- Evidence quality scores
- Source credibility
- Missing evidence

Outputs:

- Bull thesis
- Bear thesis
- Points of agreement
- Points of disagreement
- Key evidence
- Open questions
- Debate conclusion

The Debate Agent should not simply average agent scores. It should identify the central disagreement.

Example:

```text
Bull case:
- The selloff is tied to guidance reset rather than solvency or demand collapse.
- Business quality remains high.
- Valuation has improved enough to justify manual research.

Bear case:
- Management commentary suggests growth durability may be weaker.
- Technical stabilization is not yet clear.
- Evidence quality is medium because primary documents are incomplete.
```

## Risk Agent

Purpose: protect the final shortlist from attractive-looking but dangerous setups.

Main question:

```text
Should this candidate be blocked, downgraded, or allowed into the final research shortlist?
```

Tools:

- Structural risk classifier
- Legal/regulatory risk detector
- Balance-sheet stress checker
- Data quality checker
- Falling-knife checker

Outputs:

- Risk rating: Low / Medium / High / Blocked
- Blocking reasons
- Structural risk summary
- Data quality concerns
- Required human checks
- Final risk gate decision

Hard blocks:

- Bankruptcy or going-concern risk
- Fraud or accounting irregularity
- Delisting or severe liquidity issue
- Major unresolved legal/regulatory risk
- High structural risk with weak business quality
- Evidence quality too low for the claimed thesis

Risk Agent should have veto power. A candidate can have an attractive event setup and still be blocked.

## Trade Review Agent

Purpose: convert the research packet into a human-readable action.

This agent should not place trades.

Actions:

- `Focus`: worth serious manual research now
- `Watch`: interesting, but not ready
- `Pass`: not worth time this cycle
- `Blocked`: risk or evidence quality invalidates the setup

Outputs:

- Final action
- Research rationale
- Main bull case
- Main bear case
- Evidence quality
- Risk rating
- Invalidation conditions
- Next verification event

Example:

```text
Action: Focus
Reason:
- Selloff appears tied to a checkable earnings/guidance reset.
- Business quality remains high.
- Valuation is more reasonable than before the drawdown.
- No hard risk block found.

Main risk:
- Technical stabilization is early and the next earnings call must confirm margin durability.
```

## Agent Output Schema

Every agent should return structured output.

```json
{
  "agent": "sec_filing",
  "ticker": "ADBE",
  "task": "Determine whether recent filings support a structural risk thesis.",
  "conclusion": "No hard structural risk found in recent filings, but growth durability needs transcript confirmation.",
  "stance": "mixed_positive",
  "confidence": 0.68,
  "evidence": [
    {
      "source_type": "sec_filing",
      "source": "10-Q",
      "date": "2026-05-08",
      "claim": "Risk language does not indicate a new liquidity or solvency issue.",
      "credibility": 1.0
    }
  ],
  "counterarguments": [
    "Risk language around demand uncertainty expanded compared with prior filing."
  ],
  "missing_evidence": [
    "Latest earnings call transcript",
    "Management commentary on AI-related competitive pressure"
  ],
  "risk_flags": [],
  "recommended_next_steps": [
    "Run transcript analysis before promoting to Focus."
  ]
}
```

## Tool Layer

Tools should be deterministic, testable functions. Agents call tools; tools should not make investment judgments.

Initial tools:

- `price_history_tool`
- `news_search_tool`
- `sec_submissions_tool`
- `sec_company_facts_tool`
- `filing_text_tool`
- `financial_metrics_tool`
- `valuation_tool`
- `technical_indicators_tool`
- `report_writer_tool`

Later tools:

- `earnings_transcript_tool`
- `risk_factor_diff_tool`
- `peer_comparison_tool`
- `reddit_sentiment_tool`
- `source_hit_rate_tool`
- `memory_store_tool`

Tool output should include:

- Raw data reference
- Timestamp
- Source URL or source ID
- Data freshness
- Parsing confidence
- Any failure or partial-data warning

## Evidence Quality

Evidence quality should be dynamic. It should affect confidence and final action.

Inputs:

- Source credibility
- Primary-source confirmation
- Number of independent sources
- Consistency between sources
- Data freshness
- Completeness of required evidence
- Historical source hit rate

Initial source credibility:

```text
SEC filings:          1.00
Company reports:      0.90
Earnings transcripts: 0.85
Reuters/Bloomberg:    0.80
Major financial news: 0.65
Yahoo RSS headlines:  0.45
Reddit/social:        0.20
```

Suggested score:

```text
evidence_quality =
  0.30 * source_credibility
+ 0.25 * primary_source_confirmation
+ 0.15 * source_consistency
+ 0.10 * source_independence
+ 0.10 * data_freshness
+ 0.10 * evidence_completeness
```

Historical hit rate can be added after enough daily outputs are stored.

Low evidence quality should not just reduce a numeric score. It should change the action from `Focus` to `Watch` or `Pass`.

## Scoring and Guardrails

The AI agents should produce reasoning first. Scores should summarize the reasoning, not replace it.

The system can still maintain deterministic scoring components:

- Event opportunity score
- Business quality score
- Valuation score
- Technical stabilization score
- Structural risk penalty
- Legal/regulatory risk penalty
- Evidence quality score

But final classification should be governed by:

```text
agent conclusions
+ evidence quality
+ risk gate
+ deterministic scores
+ human-readable rationale
```

Guardrails:

- No Focus without a bear case
- No Focus with high structural risk
- No Focus with low evidence quality
- No Focus if primary documents contradict the thesis
- No automatic trading
- Every report must expose evidence and uncertainty

## Daily Workflow

```text
1. Candidate Generator selects top 10 event-driven bottom-fishing candidates.
2. Orchestrator Agent creates a research plan for each candidate.
3. Specialist agents investigate each candidate with tools.
4. Debate Agent writes bull and bear cases.
5. Risk Agent applies vetoes and downgrades.
6. Trade Review Agent classifies Focus / Watch / Pass / Blocked.
7. Report writer produces the daily email and artifacts.
```

Expected final report:

```text
Daily AI Research Shortlist

Rank | Ticker | Action | Evidence Quality | Risk | Main Bull Case | Main Bear Case | Next Check
```

Each candidate should include:

- Agent summary
- Key evidence
- Debate
- Risk gate decision
- Final action
- Missing evidence
- Human follow-up checklist

## Memory and Feedback

The system should store past research packets.

Useful memory:

- Daily candidate list
- Agent conclusions
- Evidence sources
- Final action
- Forward returns
- Maximum drawdown after selection
- Whether the thesis was invalidated
- Which sources were useful

This enables:

- Source hit-rate estimation
- Agent calibration
- Better evidence weighting
- Detection of repeated mistakes

Memory should not blindly reinforce prior conclusions. It should be used for calibration and auditability.

## Implementation Phases

### Phase 1: Agent Runtime and Schemas

No new paid data sources required.

- Define `AgentTask`
- Define `AgentContext`
- Define `AgentResult`
- Define `Evidence`
- Define `ToolResult`
- Add JSON schema validation
- Add deterministic mock mode for tests
- Add report structure for agent outputs

Goal: create the real AI-agent interface before adding complex behavior.

### Phase 2: Tool Layer

- Wrap current price/news/SEC/fundamental functions as tools
- Make tool outputs structured and timestamped
- Add source IDs and data freshness
- Add failure handling
- Keep tools deterministic and testable

Goal: give agents reliable tools without mixing tool code with reasoning.

### Phase 3: First AI-Agent Workflow

- Use the existing screener to select 10 candidates
- Run Orchestrator Agent
- Run News, SEC Filing, Financial, Technical agents
- Run Debate Agent
- Run Risk Agent
- Run Trade Review Agent
- Produce 2-3 Focus/Watch candidates

Goal: make the daily report agent-driven while keeping scope controlled.

### Phase 4: Primary Document Depth

- Add 8-K text extraction
- Add 10-Q/10-K section extraction
- Add risk factor diffing
- Add earnings transcript ingestion if a reliable source is available

Goal: make SEC and transcript analysis the highest-value part of the system.

### Phase 5: Sentiment and Reddit

- Add Reddit ingestion
- Add mention velocity
- Add narrative clustering
- Add one-sided sentiment risk
- Keep low source credibility until backtested

Goal: understand crowd behavior without letting it dominate the thesis.

### Phase 6: Memory and Calibration

- Store daily research packets
- Track forward outcomes
- Estimate source hit rates
- Calibrate confidence
- Identify repeated false-positive patterns

Goal: make the system improve through audit and feedback.

## Design Principles

- AI agents own research questions; tools own data retrieval and deterministic calculations.
- Primary evidence beats commentary.
- SEC filings and financials outrank social sentiment.
- Scores summarize reasoning; they do not replace reasoning.
- Risk Agent has veto power.
- Every Focus candidate must include a bear case.
- Every conclusion must expose evidence, confidence, and missing data.
- The system should prefer `Watch` over forcing a weak `Focus`.
- No automatic trading without a separate explicit design and approval.
