from __future__ import annotations

import json
import os
import urllib.request

from efsr.sources import fetch_recent_sec_filings, fetch_sec_company_facts
from efsr.prompts import (
    AGENT_TASK_SYSTEM_PROMPT,
    build_agent_task_prompt,
    build_llm_review_prompt,
    estimate_tokens,
)
from efsr.models import AgentPlan, AgentResult, AgentReview, AgentTask, Candidate, Evidence, ToolResult
from efsr.scoring import count_categories, top_category_labels


def _log(message: str) -> None:
    print(f"[agent] {message}", flush=True)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _load_sec_ticker_map_safely() -> dict[str, str]:
    try:
        from efsr.sources import load_sec_ticker_map

        return load_sec_ticker_map()
    except Exception:
        return {}


def build_agent_plan(candidate: Candidate) -> AgentPlan:
    category_counts = count_categories(candidate.events)
    tasks = [
        AgentTask(
            agent="news",
            question="Identify the event catalyst and assess whether the narrative is coherent.",
            required_tools=["news_summary"],
            max_rounds=1,
            required_fields=["decision", "conclusion", "missing_evidence"],
        ),
        AgentTask(
            agent="sec_filing",
            question="Check whether the selloff is supported by primary SEC disclosures.",
            required_tools=["sec_filings", "sec_company_facts"],
            max_rounds=2,
            required_fields=["decision", "conclusion", "missing_evidence"],
        ),
        AgentTask(
            agent="financial",
            question="Judge whether the business is worth bottom-fishing or merely cheaper for a reason.",
            required_tools=["financial_metrics"],
            max_rounds=1,
            required_fields=["decision", "conclusion", "missing_evidence"],
        ),
        AgentTask(
            agent="technical",
            question="Decide whether the setup is stabilizing or still a falling knife.",
            required_tools=["price_history"],
            max_rounds=1,
            required_fields=["decision", "conclusion", "missing_evidence"],
        ),
        AgentTask(
            agent="sentiment",
            question="Measure retail crowding risk and note any missing sentiment source.",
            required_tools=["sentiment"],
            max_rounds=1,
            required_fields=["decision", "conclusion", "missing_evidence"],
        ),
        AgentTask(
            agent="debate",
            question="Synthesize bull and bear cases from the specialist agents.",
            required_tools=["agent_committee"],
            max_rounds=1,
            required_fields=["decision", "conclusion", "missing_evidence"],
        ),
        AgentTask(
            agent="risk",
            question="Apply the risk gate and decide whether the candidate should be blocked.",
            required_tools=["agent_committee", "risk_rules"],
            max_rounds=1,
            required_fields=["decision", "conclusion", "missing_evidence"],
        ),
    ]
    if not category_counts.get("legal_regulatory"):
        tasks[-1].required_tools.append("secondary_price_check")
    return AgentPlan(tasks=tasks, stop_reason="Planned research path built from event screen and quality filter.")


def _mode_uses_llm(mode: str) -> bool:
    return mode in {"lean", "full"} and bool(os.environ.get("OPENAI_API_KEY"))


def _mode_uses_task_llm(mode: str, task_agent: str) -> bool:
    return mode == "full" and task_agent in {"news", "sec_filing", "financial", "technical", "sentiment", "debate", "risk"} and bool(os.environ.get("OPENAI_API_KEY"))


def _mode_uses_final_llm(mode: str) -> bool:
    return mode in {"lean", "full"} and bool(os.environ.get("OPENAI_API_KEY"))


def _llm_mode_label(mode: str) -> str:
    if not os.environ.get("OPENAI_API_KEY"):
        return "off"
    if mode == "full":
        return "on(per-task+final)"
    if mode == "lean":
        return "on(final-only)"
    return "off"


def _tool_news(candidate: Candidate) -> ToolResult:
    headlines = candidate.events[:3]
    summary = ", ".join(top_category_labels(count_categories(candidate.events), 4)) or "No clear event catalyst"
    evidence = [
        Evidence(
            source_type="news",
            source=event.link,
            date=event.published.date().isoformat() if event.published else "",
            claim=event.title,
            credibility=0.45,
        )
        for event in headlines
    ]
    return ToolResult(
        tool="news_summary",
        status="ok" if headlines else "empty",
        summary=summary,
        evidence=evidence,
        metadata={"headline_count": str(len(headlines))},
    )


def _tool_sec(candidate: Candidate) -> ToolResult:
    filings = candidate.data_confidence.sec_filings
    summary = (
        ", ".join(sorted({filing.form for filing in filings}))
        if filings
        else "No recent SEC filing in the current lookback window."
    )
    evidence = [
        Evidence(
            source_type="sec_filing",
            source=filing.form,
            date=filing.filing_date,
            claim=filing.description or filing.form,
            credibility=1.0,
        )
        for filing in filings[:3]
    ]
    return ToolResult(
        tool="sec_filings",
        status="ok" if filings else "missing",
        summary=summary,
        evidence=evidence,
        metadata={"filing_count": str(len(filings))},
    )


def _tool_facts(candidate: Candidate, facts: dict | None) -> ToolResult:
    fs = candidate.fundamentals
    summary = (
        f"quality={fs.business_quality_score:.1f}, valuation={fs.valuation_score:.1f}, "
        f"structural_risk={fs.structural_risk_penalty:.1f}"
    )
    evidence = [
        Evidence(
            source_type="financial_metric",
            source=fs.source_status,
            claim=summary,
            credibility=0.75 if fs.source_status == "SEC company facts" else 0.30,
        )
    ]
    metadata = {}
    if facts:
        metadata["companyfacts"] = "available"
    return ToolResult(tool="sec_company_facts", status="ok" if facts else "missing", summary=summary, evidence=evidence, metadata=metadata)


def _tool_price(candidate: Candidate) -> ToolResult:
    price = candidate.price
    summary = (
        f"60d_drawdown={price.drawdown_60d:.1f}%, 20d_change={price.change_20d:.1f}%, "
        f"5d_change={price.change_5d:.1f}%, above_5d_low={price.above_5d_low:.1f}%"
    )
    evidence = [
        Evidence(
            source_type="price",
            source="Yahoo chart",
            claim=summary,
            credibility=0.60,
        )
    ]
    return ToolResult(tool="price_history", status="ok", summary=summary, evidence=evidence)


def _tool_sentiment(candidate: Candidate) -> ToolResult:
    return ToolResult(
        tool="sentiment",
        status="missing",
        summary="Sentiment ingestion is not enabled yet.",
        evidence=[],
        metadata={"reason": "no source configured"},
    )


def _tool_committee(agent_results: list[AgentResult]) -> ToolResult:
    summary = "; ".join(
        f"{result.agent}:{result.stance}:{result.confidence:.2f}"
        for result in agent_results
    )
    return ToolResult(
        tool="agent_committee",
        status="ok" if agent_results else "empty",
        summary=summary or "No committee results yet.",
        evidence=[],
        metadata={"result_count": str(len(agent_results))},
    )


def _tool_bundle(candidate: Candidate, task: AgentTask, facts: dict | None, agent_results: list[AgentResult]) -> list[ToolResult]:
    bundle = []
    for tool_name in task.required_tools:
        if tool_name == "news_summary":
            bundle.append(_tool_news(candidate))
        elif tool_name == "sec_filings":
            bundle.append(_tool_sec(candidate))
        elif tool_name == "sec_company_facts":
            bundle.append(_tool_facts(candidate, facts))
        elif tool_name == "financial_metrics":
            bundle.append(_tool_facts(candidate, facts))
        elif tool_name == "price_history":
            bundle.append(_tool_price(candidate))
        elif tool_name == "sentiment":
            bundle.append(_tool_sentiment(candidate))
        elif tool_name == "agent_committee":
            bundle.append(_tool_committee(agent_results))
        elif tool_name == "secondary_price_check":
            price = candidate.data_confidence.secondary_price
            summary = "available" if price else "unavailable"
            bundle.append(
                ToolResult(
                    tool="secondary_price_check",
                    status="ok" if price else "missing",
                    summary=summary,
                    evidence=[],
                    metadata={"status": summary},
                )
            )
        elif tool_name == "risk_rules":
            bundle.append(
                ToolResult(
                    tool="risk_rules",
                    status="ok",
                    summary="terminal-risk, legal, structural-risk, and evidence-quality guardrails",
                    evidence=[],
                    metadata={},
                )
            )
    return bundle


def _call_openai_json(prompt: str, model: str, max_output_tokens: int) -> tuple[dict | None, dict[str, int] | None]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None, None
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": AGENT_TASK_SYSTEM_PROMPT},
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
    usage = result.get("usage")
    text_parts = []
    for item in result.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                text_parts.append(content.get("text", ""))
    text = "\n".join(text_parts).strip()
    if not text:
        return None, _normalize_usage(usage)
    try:
        return json.loads(text), _normalize_usage(usage)
    except json.JSONDecodeError:
        return {"raw_text": text}, _normalize_usage(usage)


def _normalize_usage(payload: object) -> dict[str, int] | None:
    if not isinstance(payload, dict):
        return None
    result: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = payload.get(key)
        if isinstance(value, int):
            result[key] = value
    if not result:
        return None
    return result


def _parse_task_result(payload: dict | None, fallback: AgentResult) -> AgentResult:
    if not payload:
        return fallback
    decision = str(payload.get("decision") or payload.get("action") or fallback.stance).strip()
    conclusion = str(payload.get("conclusion") or payload.get("reasoning") or fallback.conclusion).strip()
    stance = str(payload.get("stance") or fallback.stance).strip() or fallback.stance
    confidence = payload.get("confidence")
    if isinstance(confidence, (int, float)):
        fallback.confidence = float(confidence)
    fallback.conclusion = conclusion
    fallback.stance = stance
    if decision in {"Focus", "Watch", "Pass", "Blocked"}:
        fallback.next_steps = [f"decision:{decision}"] + fallback.next_steps
    if isinstance(payload.get("missing_evidence"), list):
        fallback.missing_evidence = [str(item) for item in payload["missing_evidence"][:8]]
    if isinstance(payload.get("follow_up_tools"), list):
        fallback.next_steps.extend([str(item) for item in payload["follow_up_tools"][:5]])
    if isinstance(payload.get("risk_flags"), list):
        fallback.risk_flags.extend([str(item) for item in payload["risk_flags"][:5]])
    if isinstance(payload.get("next_steps"), list):
        fallback.next_steps = [str(item) for item in payload["next_steps"][:8]]
    return fallback


def _make_result_from_tools(candidate: Candidate, task: AgentTask, tool_results: list[ToolResult], agent_results: list[AgentResult]) -> AgentResult:
    category_counts = count_categories(candidate.events)
    if task.agent == "news":
        stance = "negative" if category_counts.get("terminal_risk") or category_counts.get("legal_regulatory") else ("mixed_positive" if category_counts.get("analyst_positive") or category_counts.get("company_action_positive") else "mixed")
        conclusion = f"The visible event narrative is: {', '.join(top_category_labels(category_counts, 4)) or 'No clear event catalyst'}."
        confidence = clamp(0.35 + min(len(candidate.events), 5) * 0.08)
        counterarguments = ["Headline evidence can be syndicated commentary.", "Primary sources still need verification."]
        missing = ["Company press release or earnings transcript"] if category_counts.get("earnings_recoverable") else []
        risk_flags = ["legal/regulatory headline present"] if category_counts.get("legal_regulatory") else []
        evidence = [e for tool in tool_results for e in tool.evidence]
    elif task.agent == "sec_filing":
        filings = candidate.data_confidence.sec_filings
        if filings:
            forms = ", ".join(sorted({f.form for f in filings}))
            conclusion = f"Recent SEC filing trail exists ({forms}), so the thesis can be checked against primary disclosures."
            confidence = 0.70
            stance = "neutral"
            evidence = [e for tool in tool_results for e in tool.evidence]
            missing = ["8-K/10-Q/10-K text extraction", "Risk factor diff", "Latest earnings call transcript"]
            risk_flags = []
        else:
            conclusion = "No recent 8-K/10-Q/10-K style filing was found in the lookback window, so primary-source confirmation is incomplete."
            confidence = 0.35
            stance = "unknown"
            evidence = []
            missing = ["8-K/10-Q/10-K text extraction", "Risk factor diff", "Latest earnings call transcript"]
            risk_flags = ["primary filing confirmation missing"]
        counterarguments = ["Filing metadata is not the same as reading the filing text."]
    elif task.agent == "financial":
        fs = candidate.fundamentals
        if fs.business_quality_score >= 18 and fs.structural_risk_penalty <= 12:
            stance = "positive"
            conclusion = "SEC-derived metrics suggest enough business quality to justify deeper research."
            confidence = 0.68
        elif fs.structural_risk_penalty > 25:
            stance = "negative"
            conclusion = "Structural risk signals are too high for a normal bottom-fishing setup."
            confidence = 0.72
        else:
            stance = "mixed"
            conclusion = "Financial support is mixed; this needs manual review before promotion to Focus."
            confidence = 0.52
        evidence = [e for tool in tool_results for e in tool.evidence]
        counterarguments = fs.risks[:3]
        missing = ["Peer comparison", "Latest quarterly trend", "Earnings transcript commentary"]
        risk_flags = fs.risks[:2] if fs.structural_risk_penalty else []
    elif task.agent == "technical":
        price = candidate.price
        if price.above_5d_low >= 2 and price.change_5d > -10:
            stance = "mixed_positive"
            conclusion = "The chart shows early stabilization, but it is not proof of business improvement."
            confidence = 0.60
        else:
            stance = "negative"
            conclusion = "The setup still has falling-knife risk."
            confidence = 0.58
        evidence = [e for tool in tool_results for e in tool.evidence]
        counterarguments = ["Technical stabilization can fail quickly after event-driven selloffs."]
        missing = ["Relative strength vs sector ETF", "Intraday support/volume profile"]
        risk_flags = ["5-day price action is still sharply negative"] if price.change_5d < -10 else []
    elif task.agent == "sentiment":
        stance = "unknown"
        conclusion = "Sentiment ingestion is not enabled yet; treat crowd narrative as missing evidence."
        confidence = 0.10
        evidence = []
        counterarguments = ["No Reddit/social source has been ingested in this run."]
        missing = ["Reddit mention velocity", "Narrative clustering", "One-sided sentiment check"]
        risk_flags = ["sentiment unavailable"]
    elif task.agent == "debate":
        positive = [result for result in tool_results if result.metadata.get("stance") in {"positive", "mixed_positive"}]
        negative = [result for result in tool_results if result.metadata.get("stance") == "negative"]
        stance = "mixed_positive" if len(positive) > len(negative) else "mixed"
        bull = candidate.deep_dive_reasons[0] if candidate.deep_dive_reasons else candidate.thesis
        bear = candidate.deep_dive_risks[0] if candidate.deep_dive_risks else candidate.risks[0]
        conclusion = (
            f"Bull case: {bull} Bear case: {bear} "
            f"Agent balance is {len(positive)} constructive vs {len(negative)} negative."
        )
        confidence = 0.50
        evidence = []
        counterarguments = [bear]
        missing = sorted({item for result in agent_results for item in result.missing_evidence})[:5] if agent_results else []
        risk_flags = sorted({item for result in agent_results for item in result.risk_flags})[:5] if agent_results else []
    else:
        hard_blocks = []
        if category_counts.get("terminal_risk"):
            hard_blocks.append("terminal-risk headline appeared")
        if candidate.fundamentals.structural_risk_penalty > 30:
            hard_blocks.append("structural risk penalty is high")
        if candidate.fundamentals.business_quality_score < 8 and candidate.deep_dive_score < 50:
            hard_blocks.append("business quality support is weak")
        if hard_blocks:
            stance = "negative"
            conclusion = "Risk gate blocks this candidate from the Focus list: " + "; ".join(hard_blocks) + "."
            confidence = 0.78
        elif category_counts.get("legal_regulatory") or candidate.fundamentals.structural_risk_penalty > 20:
            stance = "negative"
            conclusion = "Risk is high enough to require manual primary-source review before any Focus classification."
            confidence = 0.66
        elif candidate.agent_review.evidence_quality < 0.55:
            stance = "mixed"
            conclusion = "Risk is manageable, but evidence quality is not strong enough for high conviction."
            confidence = 0.60
        else:
            stance = "mixed_positive"
            conclusion = "No hard risk block was found in the available evidence."
            confidence = 0.62
        evidence = [e for tool in tool_results for e in tool.evidence]
        counterarguments = hard_blocks or candidate.deep_dive_risks[:3]
        missing = sorted({item for result in agent_results for item in result.missing_evidence})[:5] if agent_results else []
        risk_flags = hard_blocks[:6]

    return AgentResult(
        agent=task.agent,
        task=task.question,
        conclusion=conclusion,
        stance=stance,
        confidence=round(confidence, 2),
        evidence=evidence,
        counterarguments=counterarguments,
        missing_evidence=missing,
        risk_flags=risk_flags,
        next_steps=["Resolve the highest-impact missing evidence before treating a candidate as Focus."],
    )


def _build_task_result(candidate: Candidate, task: AgentTask, facts: dict | None, agent_results: list[AgentResult], mode: str, model: str, max_output_tokens: int, token_budget: int) -> tuple[AgentResult, list[ToolResult], dict[str, int] | None, int]:
    tool_results = _tool_bundle(candidate, task, facts, agent_results)
    fallback = _make_result_from_tools(candidate, task, tool_results, agent_results)
    prompt_estimate = 0
    usage = None
    if not _mode_uses_task_llm(mode, task.agent):
        payload = None
    else:
        prompt = build_agent_task_prompt(candidate, task, tool_results, [result.conclusion for result in agent_results[-3:]], token_budget)
        prompt_estimate = estimate_tokens(prompt)
        try:
            payload, usage = _call_openai_json(prompt, model, max_output_tokens)
        except Exception as exc:  # noqa: BLE001
            fallback.next_steps = [f"OpenAI task failed: {exc}"] + fallback.next_steps
            payload = None
    result = _parse_task_result(payload, fallback)
    for tool in tool_results:
        tool.metadata["agent"] = task.agent
    return result, tool_results, usage, prompt_estimate


def _final_review(candidate: Candidate, agent_results: list[AgentResult], tool_trace: list[ToolResult], mode: str, model: str, token_budget: int, max_output_tokens: int) -> tuple[str, str, str]:
    evidence_score = candidate.agent_review.evidence_quality
    review_prompt = build_llm_review_prompt(candidate, agent_results, token_budget)
    if _mode_uses_final_llm(mode):
        try:
            payload = _call_openai_json(review_prompt, model, max_output_tokens)
        except Exception:
            payload = None
    else:
        payload = None
    if isinstance(payload, dict):
        decision = str(payload.get("decision") or payload.get("action") or "").strip()
        reasoning = str(payload.get("main_bull_case") or "").strip() or "Agent review supports serious manual research."
        main_bear = str(payload.get("main_bear_case") or "").strip() or candidate.risks[0]
        if decision not in {"Focus", "Watch", "Pass", "Blocked"}:
            decision = "Focus" if candidate.deep_dive_decision == "Focus" and evidence_score >= 0.55 else "Watch"
        return decision, reasoning, main_bear
    if candidate.deep_dive_decision == "Focus" and evidence_score >= 0.55:
        return "Focus", "Agent review supports serious manual research.", candidate.deep_dive_risks[0] if candidate.deep_dive_risks else candidate.risks[0]
    if evidence_score < 0.45:
        return "Pass", "Evidence quality is too low for serious research this cycle.", candidate.risks[0]
    if candidate.deep_dive_score >= 35:
        return "Watch", "The setup is interesting but needs more evidence before Focus.", candidate.deep_dive_risks[0] if candidate.deep_dive_risks else candidate.risks[0]
    return "Pass", "The agent review does not find enough support to prioritize it.", candidate.risks[0]


def build_agent_review(candidate: Candidate, token_budget: int, mode: str = "deterministic", model: str = "gpt-4o-mini", max_output_tokens: int = 350) -> AgentReview:
    plan = build_agent_plan(candidate)
    ticker_map = _load_sec_ticker_map_safely()
    facts = fetch_sec_company_facts(candidate.ticker, ticker_map)
    if not candidate.data_confidence.sec_filings:
        candidate.data_confidence.sec_filings = fetch_recent_sec_filings(candidate.ticker, ticker_map, 14)
    _log(
        f"{candidate.ticker} start mode={mode} llm={_llm_mode_label(mode)} "
        f"deep_dive={candidate.deep_dive_decision} score={candidate.deep_dive_score:.2f} "
        f"token_budget={token_budget} model={model}"
    )
    agent_results: list[AgentResult] = []
    tool_trace: list[ToolResult] = []
    estimated_prompt_tokens = 0
    estimated_output_tokens = 0
    actual_input_tokens = 0
    actual_output_tokens = 0
    for task in plan.tasks:
        result, tool_results, usage, prompt_estimate = _build_task_result(candidate, task, facts, agent_results, mode, model, max_output_tokens, token_budget)
        agent_results.append(result)
        tool_trace.extend(tool_results)
        estimated_prompt_tokens += prompt_estimate
        estimated_output_tokens += max_output_tokens if _mode_uses_task_llm(mode, task.agent) else 0
        if usage:
            actual_input_tokens += usage.get("input_tokens", 0)
            actual_output_tokens += usage.get("output_tokens", 0)
        tool_summary = "; ".join(f"{tool.tool}:{tool.status}" for tool in tool_results) or "no-tools"
        _log(
            f"{candidate.ticker} task={task.agent} llm={'on' if usage else 'off'} "
            f"stance={result.stance} conf={result.confidence:.2f} prompt~{prompt_estimate} tool={tool_summary}"
        )

    evidence_quality_score = 0.45
    if candidate.data_confidence.sec_filings:
        evidence_quality_score += 0.20
    if facts:
        evidence_quality_score += 0.10
    if candidate.fundamentals.source_status == "SEC company facts":
        evidence_quality_score += 0.10
    if count_categories(candidate.events):
        evidence_quality_score += 0.10
    evidence_quality_score = clamp(evidence_quality_score)

    candidate.agent_review.evidence_quality = round(evidence_quality_score, 3)
    debate_result = next((result for result in agent_results if result.agent == "debate"), None)
    risk_result = next((result for result in agent_results if result.agent == "risk"), None)
    decision, reasoning, main_bear = _final_review(candidate, agent_results, tool_trace, mode, model, token_budget, max_output_tokens)
    if risk_result and risk_result.stance == "negative" and "blocks this candidate" in risk_result.conclusion:
        decision = "Blocked"
        reasoning = "Risk Agent vetoed the setup."
        main_bear = risk_result.conclusion

    review_score = candidate.deep_dive_score + candidate.agent_review.evidence_quality * 20
    if decision == "Blocked":
        review_score -= 20
    if risk_result and risk_result.stance == "negative":
        review_score -= 10
    if debate_result and debate_result.stance == "mixed_positive":
        review_score += 4

    if decision == "Blocked":
        risk_rating = "Blocked"
    elif risk_result and risk_result.stance == "negative":
        risk_rating = "High"
    elif risk_result and "No hard risk block" in risk_result.conclusion:
        risk_rating = "Low"
    else:
        risk_rating = "Medium"

    missing_evidence = sorted({item for result in agent_results for item in result.missing_evidence})[:8]
    invalidation = list(candidate.watchpoints[:2])
    if candidate.fundamentals.structural_risk_penalty > 0:
        invalidation.append("Structural risk rises or is confirmed by primary filings.")
    if candidate.agent_review.evidence_quality < 0.55:
        invalidation.append("Primary-source evidence remains unavailable.")

    review = AgentReview(
        decision=decision,
        review_score=round(review_score, 2),
        evidence_quality=candidate.agent_review.evidence_quality,
        risk_rating=risk_rating,
        reasoning=reasoning,
        main_bull_case=candidate.deep_dive_reasons[0] if candidate.deep_dive_reasons else candidate.thesis,
        main_bear_case=main_bear,
        missing_evidence=missing_evidence,
        invalidation_conditions=invalidation[:5],
        agent_results=agent_results,
        token_budget=token_budget,
        prompt_tokens_estimate=estimate_tokens(build_llm_review_prompt(candidate, agent_results, token_budget)),
        llm_mode=mode,
        llm_provider="openai" if _mode_uses_llm(mode) else "deterministic",
        llm_notes="true agent runtime with tool trace",
        agent_plan=plan.tasks,
        tool_trace=tool_trace,
    )
    total_estimated = estimated_prompt_tokens + estimated_output_tokens
    if actual_input_tokens or actual_output_tokens:
        _log(
            f"{candidate.ticker} done decision={review.decision} risk={review.risk_rating} llm={_llm_mode_label(mode)} "
            f"evidence={review.evidence_quality:.2f} score={review.review_score:.2f} "
            f"tokens actual_in={actual_input_tokens} actual_out={actual_output_tokens} "
            f"task_prompt_estimate={estimated_prompt_tokens} final_review_prompt_estimate={review.prompt_tokens_estimate} "
            f"task_total_estimate~{total_estimated}"
        )
    else:
        _log(
            f"{candidate.ticker} done decision={review.decision} risk={review.risk_rating} llm={_llm_mode_label(mode)} "
            f"evidence={review.evidence_quality:.2f} score={review.review_score:.2f} "
            f"task_prompt_estimate={estimated_prompt_tokens} final_review_prompt_estimate={review.prompt_tokens_estimate} "
            f"task_total_estimate~{total_estimated}"
        )
    return review


def apply_agent_reviews(candidates: list[Candidate], token_budget: int, mode: str = "deterministic", model: str = "gpt-4o-mini", max_output_tokens: int = 350, review_count: int = 1, verbose: bool = True) -> list[Candidate]:
    ranked = sorted(candidates, key=lambda item: item.deep_dive_score, reverse=True)
    review_candidates = {candidate.ticker for candidate in ranked[:review_count]}
    for candidate in candidates:
        if candidate.ticker in review_candidates:
            candidate.agent_review = build_agent_review(
                candidate,
                token_budget=token_budget,
                mode=mode,
                model=model,
                max_output_tokens=max_output_tokens,
            )
    if verbose:
        reviewed = [candidate for candidate in candidates if candidate.ticker in review_candidates]
        total_prompt = sum(candidate.agent_review.prompt_tokens_estimate for candidate in reviewed)
        _log(
            "review batch "
            f"candidates={len(reviewed)} mode={mode} llm={_llm_mode_label(mode)} "
            f"final_review_prompt_total={total_prompt} "
            f"review_count={review_count} model={model}"
        )
    return candidates
