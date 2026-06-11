from __future__ import annotations

import dataclasses
import datetime as dt


@dataclasses.dataclass
class NewsItem:
    title: str
    link: str
    published: dt.datetime | None
    categories: list[str]
    sentiment: int


@dataclasses.dataclass
class PriceStats:
    last_close: float
    change_5d: float
    change_20d: float
    drawdown_60d: float
    above_5d_low: float
    volume_ratio_5d_20d: float


@dataclasses.dataclass
class FilingItem:
    form: str
    filing_date: str
    report_date: str
    accession_number: str
    description: str


@dataclasses.dataclass
class DataConfidence:
    level: str = "Low"
    reasons: list[str] = dataclasses.field(default_factory=list)
    sec_filings: list[FilingItem] = dataclasses.field(default_factory=list)
    secondary_price: PriceStats | None = None
    price_source_status: str = "not checked"


@dataclasses.dataclass
class FundamentalScore:
    business_quality_score: float = 0.0
    valuation_score: float = 0.0
    structural_risk_penalty: float = 0.0
    reasons: list[str] = dataclasses.field(default_factory=list)
    risks: list[str] = dataclasses.field(default_factory=list)
    metrics: dict[str, float | None] = dataclasses.field(default_factory=dict)
    source_status: str = "not checked"


@dataclasses.dataclass
class Evidence:
    source_type: str
    source: str
    claim: str
    credibility: float
    date: str = ""


@dataclasses.dataclass
class AgentResult:
    agent: str
    task: str
    conclusion: str
    stance: str
    confidence: float
    evidence: list[Evidence] = dataclasses.field(default_factory=list)
    counterarguments: list[str] = dataclasses.field(default_factory=list)
    missing_evidence: list[str] = dataclasses.field(default_factory=list)
    risk_flags: list[str] = dataclasses.field(default_factory=list)
    next_steps: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class AgentReview:
    decision: str = "Watch"
    review_score: float = 0.0
    evidence_quality: float = 0.0
    risk_rating: str = "Medium"
    reasoning: str = ""
    main_bull_case: str = ""
    main_bear_case: str = ""
    missing_evidence: list[str] = dataclasses.field(default_factory=list)
    invalidation_conditions: list[str] = dataclasses.field(default_factory=list)
    agent_results: list[AgentResult] = dataclasses.field(default_factory=list)
    token_budget: int = 0
    prompt_tokens_estimate: int = 0
    llm_provider: str = "deterministic"
    llm_notes: str = ""


@dataclasses.dataclass
class Candidate:
    ticker: str
    score: float
    bucket: str
    thesis: str
    reasons: list[str]
    risks: list[str]
    watchpoints: list[str]
    score_breakdown: dict[str, float]
    events: list[NewsItem]
    price: PriceStats
    deep_dive_score: float = 0.0
    deep_dive_decision: str = "Review"
    deep_dive_reasons: list[str] = dataclasses.field(default_factory=list)
    deep_dive_risks: list[str] = dataclasses.field(default_factory=list)
    data_confidence: DataConfidence = dataclasses.field(default_factory=DataConfidence)
    fundamentals: FundamentalScore = dataclasses.field(default_factory=FundamentalScore)
    agent_review: AgentReview = dataclasses.field(default_factory=AgentReview)
