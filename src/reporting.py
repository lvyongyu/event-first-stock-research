from __future__ import annotations

import dataclasses
import datetime as dt
import json

from formatting import multiple, pct
from models import Candidate


def candidate_to_dict(candidate: Candidate) -> dict:
    return {
        "ticker": candidate.ticker,
        "score": candidate.score,
        "bucket": candidate.bucket,
        "thesis": candidate.thesis,
        "reasons": candidate.reasons,
        "risks": candidate.risks,
        "watchpoints": candidate.watchpoints,
        "score_breakdown": candidate.score_breakdown,
        "deep_dive": {
            "score": candidate.deep_dive_score,
            "decision": candidate.deep_dive_decision,
            "reasons": candidate.deep_dive_reasons,
            "risks": candidate.deep_dive_risks,
        },
        "data_confidence": {
            "level": candidate.data_confidence.level,
            "reasons": candidate.data_confidence.reasons,
            "price_source_status": candidate.data_confidence.price_source_status,
            "secondary_price": (
                dataclasses.asdict(candidate.data_confidence.secondary_price)
                if candidate.data_confidence.secondary_price
                else None
            ),
            "sec_filings": [
                dataclasses.asdict(filing)
                for filing in candidate.data_confidence.sec_filings
            ],
        },
        "fundamentals": {
            "business_quality_score": candidate.fundamentals.business_quality_score,
            "valuation_score": candidate.fundamentals.valuation_score,
            "structural_risk_penalty": candidate.fundamentals.structural_risk_penalty,
            "reasons": candidate.fundamentals.reasons,
            "risks": candidate.fundamentals.risks,
            "metrics": candidate.fundamentals.metrics,
            "source_status": candidate.fundamentals.source_status,
        },
        "agent_review": {
            "decision": candidate.agent_review.decision,
            "review_score": candidate.agent_review.review_score,
            "evidence_quality": candidate.agent_review.evidence_quality,
            "risk_rating": candidate.agent_review.risk_rating,
            "reasoning": candidate.agent_review.reasoning,
            "main_bull_case": candidate.agent_review.main_bull_case,
            "main_bear_case": candidate.agent_review.main_bear_case,
            "missing_evidence": candidate.agent_review.missing_evidence,
            "invalidation_conditions": candidate.agent_review.invalidation_conditions,
            "token_budget": candidate.agent_review.token_budget,
            "prompt_tokens_estimate": candidate.agent_review.prompt_tokens_estimate,
            "llm_mode": candidate.agent_review.llm_mode,
            "llm_provider": candidate.agent_review.llm_provider,
            "llm_notes": candidate.agent_review.llm_notes,
            "agent_plan": [
                {
                    "agent": task.agent,
                    "question": task.question,
                    "required_tools": task.required_tools,
                    "max_rounds": task.max_rounds,
                    "required_fields": task.required_fields,
                }
                for task in candidate.agent_review.agent_plan
            ],
            "tool_trace": [
                {
                    "tool": tool.tool,
                    "status": tool.status,
                    "summary": tool.summary,
                    "evidence": [dataclasses.asdict(evidence) for evidence in tool.evidence],
                    "metadata": tool.metadata,
                }
                for tool in candidate.agent_review.tool_trace
            ],
            "agent_results": [
                {
                    "agent": result.agent,
                    "task": result.task,
                    "conclusion": result.conclusion,
                    "stance": result.stance,
                    "confidence": result.confidence,
                    "evidence": [dataclasses.asdict(evidence) for evidence in result.evidence],
                    "counterarguments": result.counterarguments,
                    "missing_evidence": result.missing_evidence,
                    "risk_flags": result.risk_flags,
                    "next_steps": result.next_steps,
                }
                for result in candidate.agent_review.agent_results
            ],
        },
        "price": dataclasses.asdict(candidate.price),
        "events": [
            {
                "title": event.title,
                "link": event.link,
                "published": event.published.isoformat() if event.published else None,
                "categories": event.categories,
                "sentiment": event.sentiment,
            }
            for event in candidate.events
        ],
    }


def markdown_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def event_display_label(category: str) -> str:
    labels = {
        "earnings_miss": "Earnings miss",
        "earnings_recoverable": "Earnings or guidance",
        "analyst_negative": "Negative analyst action",
        "analyst_positive": "Positive analyst action",
        "company_action_positive": "Company action positive",
        "legal_regulatory": "Legal or regulatory",
        "terminal_risk": "Terminal risk",
        "macro_sector": "Macro or sector",
    }
    return labels.get(category, category.replace("_", " ").title())


def write_outputs(candidates: list[Candidate], path_prefix: str) -> tuple[str, str]:
    json_path = f"{path_prefix}.json"
    md_path = f"{path_prefix}.md"
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "method": "event-first bottom-fishing watchlist; not investment advice",
        "candidates": [candidate_to_dict(candidate) for candidate in candidates],
    }
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)

    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write("# Daily Event-First Bottom-Fishing Watchlist\n\n")
        handle.write(f"Generated: {payload['generated_at']}\n\n")
        handle.write("This is a research watchlist, not investment advice or an auto-trading signal.\n\n")

        focus_candidates = [candidate for candidate in candidates if candidate.agent_review.decision == "Focus"]
        handle.write("## AI Agent Review Shortlist\n\n")
        if focus_candidates:
            handle.write("These are the 2-3 candidates the agent review thinks are most worth serious manual research today.\n\n")
            handle.write("| Rank | Ticker | Decision | Review Score | Evidence Quality | Risk | Token Est. | Main Bull Case | Main Bear Case |\n")
            handle.write("| ---: | --- | --- | ---: | ---: | --- | ---: | --- | --- |\n")
            for index, candidate in enumerate(
                sorted(focus_candidates, key=lambda item: item.agent_review.review_score, reverse=True),
                start=1,
            ):
                handle.write(
                    f"| {index} | {candidate.ticker} | {candidate.agent_review.decision} | "
                    f"{candidate.agent_review.review_score:.2f} | "
                    f"{candidate.agent_review.evidence_quality:.2f} | "
                    f"{candidate.agent_review.risk_rating} | "
                    f"{candidate.agent_review.prompt_tokens_estimate} | "
                    f"{markdown_escape(candidate.agent_review.main_bull_case)} | "
                    f"{markdown_escape(candidate.agent_review.main_bear_case)} |\n"
                )
        else:
            handle.write("No candidates passed the agent-review Focus threshold today.\n")

        handle.write("\n## Deep Dive Shortlist\n\n")
        deep_dive_focus = [candidate for candidate in candidates if candidate.deep_dive_decision == "Focus"]
        if deep_dive_focus:
            handle.write("This is the deterministic second-stage shortlist before the AI agent risk/debate overlay.\n\n")
            handle.write("| Rank | Ticker | Deep Dive | Quality | Valuation | Structural Risk | Confidence | Original Score | Why It Is A Focus Candidate | Main Risk |\n")
            handle.write("| ---: | --- | ---: | ---: | ---: | ---: | --- | ---: | --- | --- |\n")
            for index, candidate in enumerate(
                sorted(deep_dive_focus, key=lambda item: item.deep_dive_score, reverse=True),
                start=1,
            ):
                reason = candidate.deep_dive_reasons[0] if candidate.deep_dive_reasons else ""
                risk = candidate.deep_dive_risks[0] if candidate.deep_dive_risks else ""
                handle.write(
                    f"| {index} | {candidate.ticker} | {candidate.deep_dive_score:.2f} | "
                    f"{candidate.fundamentals.business_quality_score:.2f} | "
                    f"{candidate.fundamentals.valuation_score:.2f} | "
                    f"{candidate.fundamentals.structural_risk_penalty:.2f} | "
                    f"{candidate.data_confidence.level} | {candidate.score:.2f} | "
                    f"{markdown_escape(reason)} | {markdown_escape(risk)} |\n"
                )
        else:
            handle.write("No candidates passed the deterministic deep-dive focus threshold today.\n")

        handle.write("\n## Full Top-10 Event Screen\n\n")
        handle.write("| Rank | Ticker | Agent Decision | Evidence Quality | Agent Risk | Deep Dive | Confidence | Bucket | Score | Quality | Valuation | Structural Risk | Setup | Why It Made The List | Key Risk |\n")
        handle.write("| ---: | --- | --- | ---: | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- |\n")
        for index, candidate in enumerate(candidates, start=1):
            risk = candidate.risks[0] if candidate.risks else ""
            reason = candidate.reasons[0] if candidate.reasons else ""
            handle.write(
                f"| {index} | {candidate.ticker} | {candidate.agent_review.decision} | "
                f"{candidate.agent_review.evidence_quality:.2f} | {candidate.agent_review.risk_rating} | "
                f"{candidate.deep_dive_score:.2f} | {candidate.data_confidence.level} | {candidate.bucket} | "
                f"{candidate.score:.2f} | "
                f"{candidate.fundamentals.business_quality_score:.2f} | "
                f"{candidate.fundamentals.valuation_score:.2f} | "
                f"{candidate.fundamentals.structural_risk_penalty:.2f} | "
                f"{markdown_escape(candidate.thesis)} | "
                f"{markdown_escape(reason)} | {markdown_escape(risk)} |\n"
            )
        handle.write("\n## Candidate Rationale\n\n")
        for candidate in candidates:
            handle.write(f"### {candidate.ticker}\n\n")
            handle.write(f"**Score:** {candidate.score:.2f}  \n")
            handle.write(f"**Deep Dive Score:** {candidate.deep_dive_score:.2f}  \n")
            handle.write(f"**Deep Dive Decision:** {candidate.deep_dive_decision}  \n")
            handle.write(f"**Agent Decision:** {candidate.agent_review.decision}  \n")
            handle.write(f"**Agent Review Score:** {candidate.agent_review.review_score:.2f}  \n")
            handle.write(f"**Evidence Quality:** {candidate.agent_review.evidence_quality:.2f}  \n")
            handle.write(f"**Agent Risk:** {candidate.agent_review.risk_rating}  \n")
            handle.write(f"**Prompt Token Estimate:** {candidate.agent_review.prompt_tokens_estimate} / {candidate.agent_review.token_budget}  \n")
            handle.write(f"**LLM Mode:** {candidate.agent_review.llm_mode}  \n")
            handle.write(f"**LLM Provider:** {candidate.agent_review.llm_provider}  \n")
            handle.write(f"**Data Confidence:** {candidate.data_confidence.level}  \n")
            handle.write(f"**Business Quality Score:** {candidate.fundamentals.business_quality_score:.2f}  \n")
            handle.write(f"**Valuation Score:** {candidate.fundamentals.valuation_score:.2f}  \n")
            handle.write(f"**Structural Risk Penalty:** {candidate.fundamentals.structural_risk_penalty:.2f}  \n")
            handle.write(f"**Bucket:** {candidate.bucket}  \n")
            handle.write(f"**Setup:** {candidate.thesis}\n\n")

            handle.write("**AI agent review**\n\n")
            handle.write(f"- Decision: {candidate.agent_review.decision}\n")
            handle.write(f"- Reasoning: {candidate.agent_review.reasoning}\n")
            handle.write(f"- Main bull case: {candidate.agent_review.main_bull_case}\n")
            handle.write(f"- Main bear case: {candidate.agent_review.main_bear_case}\n")
            if candidate.agent_review.llm_notes:
                handle.write(f"- LLM/token notes: {candidate.agent_review.llm_notes}\n")
            if candidate.agent_review.missing_evidence:
                handle.write("- Missing evidence: " + "; ".join(candidate.agent_review.missing_evidence[:5]) + "\n")
            if candidate.agent_review.invalidation_conditions:
                handle.write("- Invalidation checks: " + "; ".join(candidate.agent_review.invalidation_conditions[:4]) + "\n")
            handle.write("\n")

            handle.write("**Agent committee**\n\n")
            for result in candidate.agent_review.agent_results:
                handle.write(
                    f"- {result.agent}: {result.stance}, confidence {result.confidence:.2f}. "
                    f"{result.conclusion}\n"
                )
            handle.write("\n")

            if candidate.agent_review.agent_plan:
                handle.write("**Agent plan**\n\n")
                for task in candidate.agent_review.agent_plan:
                    tools = ", ".join(task.required_tools) if task.required_tools else "none"
                    handle.write(f"- {task.agent}: {task.question} [{tools}]\n")
                handle.write("\n")

            if candidate.agent_review.tool_trace:
                handle.write("**Tool trace**\n\n")
                for tool in candidate.agent_review.tool_trace:
                    handle.write(f"- {tool.tool} ({tool.status}): {tool.summary}\n")
                handle.write("\n")

            handle.write("**Business quality, valuation, and structural risk**\n\n")
            handle.write(f"- Source: {candidate.fundamentals.source_status}\n")
            for reason in candidate.fundamentals.reasons:
                handle.write(f"- {reason}\n")
            for risk in candidate.fundamentals.risks:
                handle.write(f"- {risk}\n")
            metrics = candidate.fundamentals.metrics
            if metrics:
                handle.write(
                    "- Metrics: "
                    f"revenue growth {pct(metrics.get('revenue_growth'))}, "
                    f"net margin {pct(metrics.get('net_margin'))}, "
                    f"FCF margin {pct(metrics.get('fcf_margin'))}, "
                    f"liabilities/assets {pct(metrics.get('liabilities_to_assets'))}, "
                    f"P/S {multiple(metrics.get('price_to_sales'))}, "
                    f"P/E {multiple(metrics.get('price_to_earnings'))}, "
                    f"FCF yield {pct(metrics.get('fcf_yield'))}\n"
                )
            handle.write("\n")

            handle.write("**Data confidence**\n\n")
            for reason in candidate.data_confidence.reasons:
                handle.write(f"- {reason}\n")
            handle.write(f"- {candidate.data_confidence.price_source_status}\n")
            if candidate.data_confidence.sec_filings:
                handle.write("\nRecent SEC filings:\n\n")
                for filing in candidate.data_confidence.sec_filings:
                    description = f" - {filing.description}" if filing.description else ""
                    handle.write(
                        f"- {filing.filing_date}: {filing.form}{description} "
                        f"(accession {filing.accession_number})\n"
                    )
            handle.write("\n")

            handle.write("**Deep dive take**\n\n")
            for reason in candidate.deep_dive_reasons:
                handle.write(f"- {reason}\n")
            handle.write("\n")

            handle.write("**Deep dive risks**\n\n")
            for risk in candidate.deep_dive_risks:
                handle.write(f"- {risk}\n")
            handle.write("\n")

            handle.write("**Why it made the list**\n\n")
            for reason in candidate.reasons:
                handle.write(f"- {reason}\n")
            handle.write("\n")

            handle.write("**What could break the thesis**\n\n")
            for risk in candidate.risks:
                handle.write(f"- {risk}\n")
            handle.write("\n")

            handle.write("**What to verify next**\n\n")
            for watchpoint in candidate.watchpoints:
                handle.write(f"- {watchpoint}\n")
            handle.write("\n")

            handle.write("**Score breakdown**\n\n")
            for label, value in candidate.score_breakdown.items():
                handle.write(f"- {label}: {value:+.2f}\n")
            handle.write("\n")

            handle.write("**Event evidence**\n\n")
            for event in candidate.events[:5]:
                date = event.published.date().isoformat() if event.published else "unknown date"
                categories = ", ".join(event_display_label(category) for category in event.categories)
                handle.write(f"- {date}: [{event.title}]({event.link}) ({categories})\n")
            handle.write("\n")
    return json_path, md_path
