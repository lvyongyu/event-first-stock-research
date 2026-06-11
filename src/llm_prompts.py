from __future__ import annotations

import re


OPENAI_REVIEW_SYSTEM_PROMPT = (
    "You are a cautious equity research assistant. "
    "Return compact JSON only. Do not provide investment advice."
)


def compact_text(value: str, max_chars: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3].rstrip() + "..."


def estimate_tokens(value: str) -> int:
    return max(1, len(value) // 4)


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def multiple(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}x"


def build_llm_review_prompt(candidate, agent_results: list, token_budget: int) -> str:
    """Build a compact prompt for optional LLM review.

    The prompt intentionally uses summaries, top evidence, and capped lists.
    Raw article text, full SEC filings, and long metric payloads should stay
    out of the prompt unless a future tool explicitly retrieves a targeted
    excerpt.
    """
    max_chars = max(1200, token_budget * 4)
    metrics = candidate.fundamentals.metrics
    event_lines = []
    for event in candidate.events[:3]:
        date = event.published.date().isoformat() if event.published else "unknown"
        event_lines.append(f"- {date}: {compact_text(event.title, 160)}")
    filing_lines = [
        f"- {filing.filing_date}: {filing.form} {compact_text(filing.description, 120)}"
        for filing in candidate.data_confidence.sec_filings[:3]
    ]
    agent_lines = [
        f"- {result.agent}: {compact_text(result.conclusion, 220)}"
        for result in agent_results
    ]
    prompt = f"""
You are reviewing one stock candidate for a research watchlist, not giving investment advice.
Return JSON only with decision, main_bull_case, main_bear_case, missing_evidence, and risk_notes.

Ticker: {candidate.ticker}
Initial setup: {compact_text(candidate.thesis, 240)}
Deep dive score: {candidate.deep_dive_score:.2f}
Deep dive decision: {candidate.deep_dive_decision}
Data confidence: {candidate.data_confidence.level}
Quality score: {candidate.fundamentals.business_quality_score:.1f}
Valuation score: {candidate.fundamentals.valuation_score:.1f}
Structural risk penalty: {candidate.fundamentals.structural_risk_penalty:.1f}
Metrics: revenue_growth={pct(metrics.get('revenue_growth'))}, net_margin={pct(metrics.get('net_margin'))}, fcf_margin={pct(metrics.get('fcf_margin'))}, liabilities/assets={pct(metrics.get('liabilities_to_assets'))}, P/S={multiple(metrics.get('price_to_sales'))}, P/E={multiple(metrics.get('price_to_earnings'))}, FCF_yield={pct(metrics.get('fcf_yield'))}

Recent events:
{chr(10).join(event_lines) if event_lines else "- none"}

Recent SEC filings:
{chr(10).join(filing_lines) if filing_lines else "- none found in lookback"}

Agent summaries:
{chr(10).join(agent_lines)}
""".strip()
    return compact_text(prompt, max_chars)
