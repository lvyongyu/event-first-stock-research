"""Legacy multi-agent review committee (selectable via --agent-impl legacy).

A self-contained, deterministic agent committee kept as an alternative to the
default agent_runtime implementation. It uses a richer 6-factor evidence model
and an optional final OpenAI overlay, but emits no per-task tool/plan trace, and
it relies on fundamentals + data_confidence already being attached to each
candidate (it does not fetch SEC data itself). Wired in through
event_bottom_fishing.apply_agent_reviews when --agent-impl legacy is selected.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

from efsr.prompts import (
    OPENAI_REVIEW_SYSTEM_PROMPT,
    build_llm_review_prompt,
    compact_text,
    estimate_tokens,
)
from efsr.models import AgentResult, AgentReview, Candidate, Evidence
from efsr.scoring import count_categories, top_category_labels

logger = logging.getLogger(__name__)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def evidence_quality(candidate: Candidate) -> tuple[float, list[str]]:
    specific_events = [
        event for event in candidate.events
        if any(category != "macro_sector" for category in event.categories)
    ]
    source_credibility = 0.45
    reasons = ["Yahoo RSS is the discovery source, so headline evidence starts at medium-low credibility."]
    if candidate.data_confidence.sec_filings:
        source_credibility = max(source_credibility, 0.85)
        reasons.append("Recent SEC filings provide primary-source evidence.")
    if candidate.fundamentals.source_status == "SEC company facts":
        source_credibility = max(source_credibility, 0.75)
        reasons.append("SEC company facts support the financial review.")

    primary_confirmation = 1.0 if candidate.data_confidence.sec_filings else 0.35
    if candidate.data_confidence.level == "High":
        primary_confirmation = max(primary_confirmation, 0.8)
    elif candidate.data_confidence.level == "Medium":
        primary_confirmation = max(primary_confirmation, 0.55)

    sentiments = [event.sentiment for event in specific_events]
    positive = sum(1 for sentiment in sentiments if sentiment > 0)
    negative = sum(1 for sentiment in sentiments if sentiment < 0)
    if positive and negative:
        source_consistency = 0.55
        reasons.append("Headlines are mixed, which lowers consistency but supports a real debate.")
    elif sentiments:
        source_consistency = 0.75
        reasons.append("Company-specific headlines mostly point in one direction.")
    else:
        source_consistency = 0.30
        reasons.append("The event trail is mostly macro/sector commentary.")

    source_independence = clamp(len({event.link for event in specific_events}) / 5)
    data_freshness = 0.80 if candidate.events else 0.20
    evidence_completeness = 0.25
    if candidate.fundamentals.source_status == "SEC company facts":
        evidence_completeness += 0.30
    if candidate.data_confidence.sec_filings:
        evidence_completeness += 0.25
    if specific_events:
        evidence_completeness += 0.20

    quality = (
        0.30 * source_credibility
        + 0.25 * primary_confirmation
        + 0.15 * source_consistency
        + 0.10 * source_independence
        + 0.10 * data_freshness
        + 0.10 * clamp(evidence_completeness)
    )
    return round(clamp(quality), 3), reasons


def agent_news(candidate: Candidate) -> AgentResult:
    category_counts = count_categories(candidate.events)
    catalyst = ", ".join(top_category_labels(category_counts, 4)) or "No clear event catalyst"
    specific_events = [
        event for event in candidate.events
        if any(category != "macro_sector" for category in event.categories)
    ]
    confidence = clamp(0.35 + min(len(specific_events), 5) * 0.08)
    stance = "mixed"
    if category_counts.get("terminal_risk") or category_counts.get("legal_regulatory"):
        stance = "negative"
    elif category_counts.get("analyst_positive") or category_counts.get("company_action_positive"):
        stance = "mixed_positive"
    elif category_counts.get("earnings_recoverable"):
        stance = "mixed"
    news_evidence = [
        Evidence(
            source_type="news",
            source=event.link,
            date=event.published.date().isoformat() if event.published else "",
            claim=event.title,
            credibility=0.45,
        )
        for event in candidate.events[:3]
    ]
    return AgentResult(
        agent="news",
        task="Identify the event catalyst and credibility of the market narrative.",
        conclusion=f"The visible event narrative is: {catalyst}.",
        stance=stance,
        confidence=round(confidence, 2),
        evidence=news_evidence,
        counterarguments=[
            "Yahoo RSS headlines may be syndicated commentary rather than independent reporting.",
            "The market narrative must be checked against primary filings or earnings material.",
        ],
        missing_evidence=["Company press release or earnings transcript"] if category_counts.get("earnings_recoverable") else [],
        risk_flags=["legal/regulatory headline present"] if category_counts.get("legal_regulatory") else [],
        next_steps=["Verify the main catalyst with a primary company source."],
    )


def agent_sec(candidate: Candidate) -> AgentResult:
    filings = candidate.data_confidence.sec_filings
    if filings:
        forms = ", ".join(sorted({filing.form for filing in filings}))
        conclusion = f"Recent SEC filing trail exists ({forms}), so the thesis can be checked against primary disclosures."
        confidence = 0.70
        stance = "neutral"
        evidence = [
            Evidence(
                source_type="sec_filing",
                source=filing.form,
                date=filing.filing_date,
                claim=filing.description or f"{filing.form} filed",
                credibility=1.0,
            )
            for filing in filings[:3]
        ]
    else:
        conclusion = "No recent 8-K/10-Q/10-K style filing was found in the lookback window, so primary-source confirmation is incomplete."
        confidence = 0.35
        stance = "unknown"
        evidence = []
    return AgentResult(
        agent="sec_filing",
        task="Check whether primary filings support or contradict the selloff thesis.",
        conclusion=conclusion,
        stance=stance,
        confidence=confidence,
        evidence=evidence,
        counterarguments=["Filing metadata is not the same as reading the filing text."],
        missing_evidence=["8-K/10-Q/10-K text extraction", "Risk factor diff", "Latest earnings call transcript"],
        risk_flags=[] if filings else ["primary filing confirmation missing"],
        next_steps=["Read the latest relevant filing sections before acting on the thesis."],
    )


def agent_financial(candidate: Candidate) -> AgentResult:
    fundamentals = candidate.fundamentals
    if fundamentals.business_quality_score >= 18 and fundamentals.structural_risk_penalty <= 12:
        stance = "positive"
        conclusion = "SEC-derived metrics suggest enough business quality to justify deeper research."
        confidence = 0.68
    elif fundamentals.structural_risk_penalty > 25:
        stance = "negative"
        conclusion = "Structural risk signals are too high for a normal bottom-fishing setup."
        confidence = 0.72
    else:
        stance = "mixed"
        conclusion = "Financial support is mixed; this needs manual review before promotion to Focus."
        confidence = 0.52
    evidence = [
        Evidence(
            source_type="financial_metric",
            source=fundamentals.source_status,
            claim=(
                f"Quality {fundamentals.business_quality_score:.1f}, "
                f"valuation {fundamentals.valuation_score:.1f}, "
                f"structural risk {fundamentals.structural_risk_penalty:.1f}."
            ),
            credibility=0.75 if fundamentals.source_status == "SEC company facts" else 0.30,
        )
    ]
    return AgentResult(
        agent="financial",
        task="Decide whether this is a quality business at a better valuation or a weak business getting weaker.",
        conclusion=conclusion,
        stance=stance,
        confidence=confidence,
        evidence=evidence,
        counterarguments=fundamentals.risks[:3],
        missing_evidence=["Peer comparison", "Latest quarterly trend", "Earnings transcript commentary"],
        risk_flags=fundamentals.risks[:2] if fundamentals.structural_risk_penalty else [],
        next_steps=["Compare growth, margins, and valuation against close peers."],
    )


def agent_technical(candidate: Candidate) -> AgentResult:
    price = candidate.price
    risk_flags = []
    if price.change_5d < -10:
        risk_flags.append("5-day price action is still sharply negative")
    if price.above_5d_low < 2:
        risk_flags.append("little evidence of stabilization above the 5-day low")
    if price.above_5d_low >= 2 and price.change_5d > -10:
        stance = "mixed_positive"
        conclusion = "The chart shows early stabilization, but it is not proof of business improvement."
        confidence = 0.60
    else:
        stance = "negative"
        conclusion = "The setup still has falling-knife risk."
        confidence = 0.58
    return AgentResult(
        agent="technical",
        task="Judge timing and stabilization without making a business-quality claim.",
        conclusion=conclusion,
        stance=stance,
        confidence=confidence,
        evidence=[
            Evidence(
                source_type="price",
                source="Yahoo chart",
                claim=(
                    f"60-day drawdown {price.drawdown_60d:.1f}%, "
                    f"5-day change {price.change_5d:.1f}%, "
                    f"{price.above_5d_low:.1f}% above 5-day low."
                ),
                credibility=0.60,
            )
        ],
        counterarguments=["Technical stabilization can fail quickly after event-driven selloffs."],
        missing_evidence=["Relative strength vs sector ETF", "Intraday support/volume profile"],
        risk_flags=risk_flags,
        next_steps=["Wait for stabilization if the stock is still below the event-day range."],
    )


def agent_sentiment(candidate: Candidate) -> AgentResult:
    return AgentResult(
        agent="sentiment",
        task="Check retail narrative and crowding risk.",
        conclusion="Sentiment ingestion is not enabled yet; treat crowd narrative as missing evidence.",
        stance="unknown",
        confidence=0.10,
        evidence=[],
        counterarguments=["No Reddit/social source has been ingested in this run."],
        missing_evidence=["Reddit mention velocity", "Narrative clustering", "One-sided sentiment check"],
        risk_flags=["sentiment unavailable"],
        next_steps=["Add Reddit/social ingestion before using sentiment as a signal."],
    )


def build_debate_result(candidate: Candidate, agent_results: list[AgentResult]) -> AgentResult:
    positive = [result for result in agent_results if result.stance in {"positive", "mixed_positive"}]
    negative = [result for result in agent_results if result.stance == "negative"]
    bull = candidate.deep_dive_reasons[0] if candidate.deep_dive_reasons else candidate.thesis
    bear = candidate.deep_dive_risks[0] if candidate.deep_dive_risks else candidate.risks[0]
    confidence = clamp((sum(result.confidence for result in agent_results) / max(len(agent_results), 1)) - 0.05 * len(negative))
    return AgentResult(
        agent="debate",
        task="Produce the strongest bull case and bear case from specialist agent outputs.",
        conclusion=(
            f"Bull case: {bull} Bear case: {bear} "
            f"Agent balance is {len(positive)} constructive vs {len(negative)} negative."
        ),
        stance="mixed_positive" if len(positive) > len(negative) else "mixed",
        confidence=round(confidence, 2),
        evidence=[
            Evidence(
                source_type="agent_committee",
                source="specialist agents",
                claim=f"{len(agent_results)} specialist results reviewed.",
                credibility=0.70,
            )
        ],
        counterarguments=[bear],
        missing_evidence=sorted({item for result in agent_results for item in result.missing_evidence})[:5],
        risk_flags=sorted({item for result in agent_results for item in result.risk_flags})[:5],
        next_steps=["Resolve the highest-impact missing evidence before treating a candidate as Focus."],
    )


def build_risk_result(candidate: Candidate, evidence_score: float, agent_results: list[AgentResult]) -> AgentResult:
    category_counts = count_categories(candidate.events)
    risk_flags = sorted({item for result in agent_results for item in result.risk_flags})
    hard_blocks = []
    if category_counts.get("terminal_risk"):
        hard_blocks.append("terminal-risk headline appeared")
    if candidate.fundamentals.structural_risk_penalty > 30:
        hard_blocks.append("structural risk penalty is high")
    if evidence_score < 0.40:
        hard_blocks.append("evidence quality is too low")
    if candidate.fundamentals.business_quality_score < 8 and candidate.deep_dive_score < 50:
        hard_blocks.append("business quality support is weak")

    if hard_blocks:
        rating = "Blocked"
        stance = "negative"
        conclusion = "Risk gate blocks this candidate from the Focus list: " + "; ".join(hard_blocks) + "."
        confidence = 0.78
    elif category_counts.get("legal_regulatory") or candidate.fundamentals.structural_risk_penalty > 20:
        rating = "High"
        stance = "negative"
        conclusion = "Risk is high enough to require manual primary-source review before any Focus classification."
        confidence = 0.66
    elif evidence_score < 0.55:
        rating = "Medium"
        stance = "mixed"
        conclusion = "Risk is manageable, but evidence quality is not strong enough for high conviction."
        confidence = 0.60
    else:
        rating = "Low"
        stance = "mixed_positive"
        conclusion = "No hard risk block was found in the available evidence."
        confidence = 0.62

    return AgentResult(
        agent="risk",
        task="Apply vetoes and downgrade candidates with unacceptable risk or weak evidence.",
        conclusion=conclusion,
        stance=stance,
        confidence=confidence,
        evidence=[
            Evidence(
                source_type="risk_gate",
                source="deterministic guardrails",
                claim=f"Risk rating: {rating}; evidence quality: {evidence_score:.2f}.",
                credibility=0.80,
            )
        ],
        counterarguments=hard_blocks or candidate.deep_dive_risks[:3],
        missing_evidence=sorted({item for result in agent_results for item in result.missing_evidence})[:5],
        risk_flags=(hard_blocks + risk_flags)[:6],
        next_steps=["Do not promote to Focus until risk blocks and missing evidence are resolved."],
    )


def decide_agent_action(candidate: Candidate, evidence_score: float, risk_result: AgentResult) -> tuple[str, str]:
    if risk_result.stance == "negative" and "blocks this candidate" in risk_result.conclusion:
        return "Blocked", "Risk Agent vetoed the setup."
    if evidence_score < 0.45:
        return "Pass", "Evidence quality is too low for serious research this cycle."
    if candidate.deep_dive_decision == "Focus" and evidence_score >= 0.55 and risk_result.stance != "negative":
        return "Focus", "Agent review supports serious manual research."
    if candidate.deep_dive_score >= 35 and risk_result.stance != "negative":
        return "Watch", "The setup is interesting but needs more evidence before Focus."
    return "Pass", "The agent review does not find enough support to prioritize it."


def build_agent_review(candidate: Candidate, token_budget: int, provider: str = "deterministic") -> AgentReview:
    evidence_score, quality_reasons = evidence_quality(candidate)
    specialist_results = [
        agent_news(candidate),
        agent_sec(candidate),
        agent_financial(candidate),
        agent_technical(candidate),
        agent_sentiment(candidate),
    ]
    debate_result = build_debate_result(candidate, specialist_results)
    risk_result = build_risk_result(candidate, evidence_score, specialist_results + [debate_result])
    decision, decision_reason = decide_agent_action(candidate, evidence_score, risk_result)
    review_score = (
        candidate.deep_dive_score
        + evidence_score * 20
        - (20 if decision == "Blocked" else 0)
        - (10 if risk_result.stance == "negative" else 0)
    )
    missing_evidence = sorted({
        item
        for result in specialist_results + [debate_result, risk_result]
        for item in result.missing_evidence
    })[:8]
    invalidation = []
    invalidation.extend(candidate.watchpoints[:2])
    if candidate.fundamentals.structural_risk_penalty > 0:
        invalidation.append("Structural risk rises or is confirmed by primary filings.")
    if evidence_score < 0.55:
        invalidation.append("Primary-source evidence remains unavailable.")

    prompt = build_llm_review_prompt(candidate, specialist_results + [debate_result, risk_result], token_budget)
    if decision == "Blocked":
        risk_rating = "Blocked"
    elif risk_result.stance == "negative":
        risk_rating = "High"
    elif "No hard risk block" in risk_result.conclusion:
        risk_rating = "Low"
    else:
        risk_rating = "Medium"

    review = AgentReview(
        decision=decision,
        review_score=round(review_score, 2),
        evidence_quality=evidence_score,
        risk_rating=risk_rating,
        reasoning=decision_reason,
        main_bull_case=candidate.deep_dive_reasons[0] if candidate.deep_dive_reasons else candidate.thesis,
        main_bear_case=candidate.deep_dive_risks[0] if candidate.deep_dive_risks else candidate.risks[0],
        missing_evidence=missing_evidence,
        invalidation_conditions=invalidation[:5],
        agent_results=specialist_results + [debate_result, risk_result],
        token_budget=token_budget,
        prompt_tokens_estimate=estimate_tokens(prompt),
        llm_provider=provider,
        llm_notes="; ".join(quality_reasons[:3]),
    )
    return review


def call_openai_review(prompt: str, model: str, max_output_tokens: int) -> dict | None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": OPENAI_REVIEW_SYSTEM_PROMPT,
            },
            {"role": "user", "content": prompt},
        ],
        "max_output_tokens": max_output_tokens,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    text_parts = []
    for item in result.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                text_parts.append(content.get("text", ""))
    text = "\n".join(text_parts).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw_text": text}


def apply_llm_overlay(
    candidates: list[Candidate],
    provider: str,
    model: str,
    review_count: int,
    token_budget: int,
    max_output_tokens: int,
) -> None:
    if provider != "openai" or not os.environ.get("OPENAI_API_KEY"):
        return
    reviewable = sorted(
        candidates,
        key=lambda item: item.agent_review.review_score,
        reverse=True,
    )[:review_count]
    for candidate in reviewable:
        prompt = build_llm_review_prompt(candidate, candidate.agent_review.agent_results, token_budget)
        try:
            result = call_openai_review(prompt, model, max_output_tokens)
        except Exception as exc:  # noqa: BLE001 - LLM should never break the report.
            candidate.agent_review.llm_notes = f"OpenAI review failed: {exc}"
            continue
        if not result:
            candidate.agent_review.llm_notes = "OpenAI review skipped or returned no content."
            continue
        candidate.agent_review.llm_provider = "openai"
        candidate.agent_review.llm_notes = compact_text(json.dumps(result, ensure_ascii=False), 800)
        if isinstance(result, dict):
            decision = str(result.get("decision") or result.get("action") or "").strip()
            if decision in {"Focus", "Watch", "Pass", "Blocked"}:
                candidate.agent_review.decision = decision
            if result.get("main_bull_case"):
                candidate.agent_review.main_bull_case = compact_text(str(result["main_bull_case"]), 500)
            if result.get("main_bear_case"):
                candidate.agent_review.main_bear_case = compact_text(str(result["main_bear_case"]), 500)
            if isinstance(result.get("missing_evidence"), list):
                candidate.agent_review.missing_evidence = [
                    compact_text(str(item), 180) for item in result["missing_evidence"][:8]
                ]


def apply_agent_reviews_legacy(
    candidates: list[Candidate],
    token_budget: int,
    mode: str = "deterministic",
    model: str = "gpt-4o-mini",
    max_output_tokens: int = 350,
    review_count: int = 1,
    verbose: bool = True,
) -> list[Candidate]:
    """Entry point for the legacy committee (build review + optional LLM overlay).

    Mirrors agent_runtime.apply_agent_reviews' signature so apply_agent_reviews
    can dispatch to either. Reviews the top `review_count` candidates by deep-dive
    score; `lean`/`full` modes apply the final OpenAI overlay when a key is set.
    """
    ranked = sorted(candidates, key=lambda item: item.deep_dive_score, reverse=True)
    reviewed = ranked[:review_count]
    use_llm = mode in {"lean", "full"} and bool(os.environ.get("OPENAI_API_KEY"))
    provider = "openai" if use_llm else "deterministic"
    for candidate in reviewed:
        candidate.agent_review = build_agent_review(candidate, token_budget, provider=provider)
    if use_llm:
        apply_llm_overlay(reviewed, "openai", model, review_count, token_budget, max_output_tokens)
    if verbose:
        logger.info(
            "[agent] legacy review batch candidates=%s mode=%s llm=%s review_count=%s model=%s",
            len(reviewed), mode, "on" if use_llm else "off", review_count, model,
        )
    return candidates
