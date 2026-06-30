from __future__ import annotations

from efsr.sources import (
    fetch_recent_sec_filings,
    fetch_sec_company_facts,
    fetch_stooq_price_stats,
    load_sec_ticker_map,
    latest_annual_value,
    latest_two_annual_values,
    latest_value,
)
from efsr.formatting import multiple, pct
from efsr.models import Candidate, DataConfidence, FundamentalScore, NewsItem, PriceStats


def add_score(breakdown: dict[str, float], label: str, value: float) -> None:
    breakdown[label] = round(breakdown.get(label, 0.0) + value, 2)


def score_fundamentals(candidate: Candidate, facts: dict | None) -> FundamentalScore:
    if not facts:
        return FundamentalScore(source_status="SEC company facts unavailable")

    revenue_values = latest_two_annual_values(
        facts,
        ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"],
    )
    latest_revenue = revenue_values[0][1] if revenue_values else None
    prior_revenue = revenue_values[1][1] if len(revenue_values) > 1 else None
    revenue_growth = (
        latest_revenue / prior_revenue - 1
        if latest_revenue and prior_revenue and prior_revenue > 0
        else None
    )
    net_income, _ = latest_annual_value(facts, ["NetIncomeLoss"])
    operating_cash_flow, _ = latest_annual_value(facts, ["NetCashProvidedByUsedInOperatingActivities"])
    capex, _ = latest_annual_value(facts, ["PaymentsToAcquirePropertyPlantAndEquipment"])
    assets, _ = latest_annual_value(facts, ["Assets"])
    liabilities, _ = latest_annual_value(facts, ["Liabilities"])
    shares = latest_value(facts, ["EntityCommonStockSharesOutstanding"], "shares")

    fcf = (
        operating_cash_flow - capex
        if operating_cash_flow is not None and capex is not None
        else None
    )
    net_margin = net_income / latest_revenue if net_income is not None and latest_revenue else None
    fcf_margin = fcf / latest_revenue if fcf is not None and latest_revenue else None
    liabilities_to_assets = liabilities / assets if liabilities is not None and assets else None
    market_cap = candidate.price.last_close * shares if shares else None
    price_to_sales = market_cap / latest_revenue if market_cap and latest_revenue else None
    price_to_earnings = market_cap / net_income if market_cap and net_income and net_income > 0 else None
    fcf_yield = fcf / market_cap if fcf is not None and market_cap else None

    quality = 0.0
    valuation = 0.0
    structural_penalty = 0.0
    reasons = []
    risks = []

    if revenue_growth is not None:
        if revenue_growth > 0.08:
            quality += 8
            reasons.append(f"Revenue growth is healthy at {pct(revenue_growth)}.")
        elif revenue_growth >= 0:
            quality += 4
            reasons.append(f"Revenue is still growing, but modestly at {pct(revenue_growth)}.")
        else:
            structural_penalty += 8
            risks.append(f"Revenue declined {pct(abs(revenue_growth))}, suggesting more than a simple event dip.")
    else:
        risks.append("Revenue growth could not be calculated from SEC company facts.")

    if net_margin is not None:
        if net_margin > 0.15:
            quality += 8
            reasons.append(f"Net margin is strong at {pct(net_margin)}.")
        elif net_margin > 0.05:
            quality += 4
            reasons.append(f"Net margin is positive at {pct(net_margin)}.")
        elif net_margin < 0:
            structural_penalty += 8
            risks.append("Latest annual net income is negative.")
    if fcf_margin is not None:
        if fcf_margin > 0.08:
            quality += 8
            reasons.append(f"Free-cash-flow margin is healthy at {pct(fcf_margin)}.")
        elif fcf_margin > 0:
            quality += 4
            reasons.append(f"Free cash flow is positive, with FCF margin at {pct(fcf_margin)}.")
        else:
            structural_penalty += 8
            risks.append("Free cash flow is negative on the latest annual SEC data.")
    if liabilities_to_assets is not None:
        if liabilities_to_assets < 0.55:
            quality += 6
            reasons.append(f"Balance-sheet leverage looks manageable with liabilities/assets at {pct(liabilities_to_assets)}.")
        elif liabilities_to_assets > 0.8:
            structural_penalty += 8
            risks.append(f"Liabilities/assets is high at {pct(liabilities_to_assets)}.")

    if price_to_sales is not None:
        if price_to_sales < 2:
            valuation += 8
            reasons.append(f"Price/sales looks reasonable at {multiple(price_to_sales)}.")
        elif price_to_sales < 5:
            valuation += 4
            reasons.append(f"Price/sales is not extreme at {multiple(price_to_sales)}.")
        elif price_to_sales > 8:
            structural_penalty += 5
            risks.append(f"Price/sales remains rich at {multiple(price_to_sales)} despite the selloff.")
    if price_to_earnings is not None:
        if price_to_earnings < 18:
            valuation += 8
            reasons.append(f"Trailing P/E is reasonable at {multiple(price_to_earnings)}.")
        elif price_to_earnings < 30:
            valuation += 4
            reasons.append(f"Trailing P/E is acceptable but not cheap at {multiple(price_to_earnings)}.")
        elif price_to_earnings > 45:
            structural_penalty += 4
            risks.append(f"Trailing P/E remains elevated at {multiple(price_to_earnings)}.")
    if fcf_yield is not None:
        if fcf_yield > 0.05:
            valuation += 9
            reasons.append(f"FCF yield is attractive at {pct(fcf_yield)}.")
        elif fcf_yield > 0.025:
            valuation += 5
            reasons.append(f"FCF yield is positive at {pct(fcf_yield)}.")
        elif fcf_yield < 0:
            structural_penalty += 6
            risks.append("FCF yield is negative.")

    structural_words = (
        "turnaround", "slower-than-expected", "debt", "cost cuts", "layoffs",
        "market share", "trust", "probe", "investigation", "lawsuit", "regulatory",
        "weak sales", "lack of catalysts", "guidance cut",
    )
    structural_hits = [
        event.title for event in candidate.events
        if any(word in event.title.lower() for word in structural_words)
    ]
    if structural_hits:
        penalty = min(len(structural_hits) * 4, 12)
        structural_penalty += penalty
        risks.append(f"{len(structural_hits)} headline(s) contain structural-risk language.")

    metrics = {
        "revenue_growth": revenue_growth,
        "net_margin": net_margin,
        "fcf_margin": fcf_margin,
        "liabilities_to_assets": liabilities_to_assets,
        "price_to_sales": price_to_sales,
        "price_to_earnings": price_to_earnings,
        "fcf_yield": fcf_yield,
    }
    if not reasons:
        reasons.append("SEC company facts were available, but no strong quality or valuation support was found.")
    if not risks:
        risks.append("No major structural warning was detected from the available SEC facts and event headlines.")
    return FundamentalScore(
        business_quality_score=round(min(quality, 30), 2),
        valuation_score=round(min(valuation, 25), 2),
        structural_risk_penalty=round(min(structural_penalty, 45), 2),
        reasons=reasons,
        risks=risks,
        metrics=metrics,
        source_status="SEC company facts",
    )


def event_label(category: str) -> str:
    labels = {
        "earnings_miss": "earnings disappointment",
        "earnings_recoverable": "earnings or guidance event",
        "analyst_negative": "negative analyst action",
        "analyst_positive": "positive analyst action",
        "company_action_positive": "shareholder-friendly company action",
        "legal_regulatory": "legal or regulatory event",
        "terminal_risk": "terminal-risk event",
        "macro_sector": "macro or sector event",
    }
    return labels.get(category, category.replace("_", " "))


def top_category_labels(category_counts: dict[str, int], limit: int = 3) -> list[str]:
    categories = sorted(category_counts, key=lambda category: category_counts[category], reverse=True)[:limit]
    return [event_label(category) for category in categories]


def count_categories(news: list) -> dict[str, int]:
    category_counts: dict[str, int] = {}
    for item in news:
        for category in item.categories:
            category_counts[category] = category_counts.get(category, 0) + 1
    return category_counts


def build_reasons(news, category_counts, price: PriceStats, negative_event_count: int, positive_event_count: int) -> list[str]:
    reasons = []
    if price.drawdown_60d < -8:
        reasons.append(
            f"Price is {abs(price.drawdown_60d):.1f}% below its 60-day closing high, "
            "so the setup is actually a pullback rather than a momentum chase."
        )
    if price.change_20d < -5:
        reasons.append(
            f"The stock is down {abs(price.change_20d):.1f}% over the last 20 trading days, "
            "which gives the event enough price damage to review for a rebound setup."
        )
    if price.above_5d_low > 2:
        reasons.append(
            f"It has bounced {price.above_5d_low:.1f}% from its 5-day low, "
            "a small sign that selling pressure may be slowing."
        )
    if price.volume_ratio_5d_20d > 1.2:
        reasons.append(
            f"Recent volume is {price.volume_ratio_5d_20d:.1f}x the 20-day average, "
            "so the move is tied to active repricing rather than quiet drift."
        )
    if negative_event_count:
        reasons.append(
            f"{negative_event_count} company-specific negative event headline(s) created the selloff/catalyst to investigate."
        )
    if positive_event_count:
        reasons.append(
            f"{positive_event_count} positive or offsetting headline(s) suggest the story is not one-sided."
        )
    if category_counts.get("earnings_recoverable"):
        reasons.append("The event mix includes earnings, guidance, margin, or revenue language, which can be checked in the next report.")
    if category_counts.get("analyst_positive"):
        reasons.append("At least one analyst-positive event appeared after the pullback, which can support a watchlist case.")
    if category_counts.get("company_action_positive"):
        reasons.append("Company action such as buybacks, dividends, asset sales, or activism may provide a catalyst.")

    if not reasons:
        reasons.append("It ranked mainly because recent event activity and price damage passed the basic screen.")
    return reasons


def build_watchpoints(category_counts, price: PriceStats) -> list[str]:
    watchpoints = []
    if category_counts.get("earnings_miss") or category_counts.get("earnings_recoverable"):
        watchpoints.append("Read the latest earnings release or call transcript; confirm whether guidance weakness is temporary or structural.")
    if category_counts.get("analyst_negative"):
        watchpoints.append("Check whether downgrades are based on short-term valuation/catalysts or a deeper business deterioration.")
    if category_counts.get("legal_regulatory"):
        watchpoints.append("Do not treat this as a normal dip until the legal or regulatory downside is bounded.")
    if category_counts.get("terminal_risk"):
        watchpoints.append("Avoid unless primary filings prove terminal-risk language is not material.")
    if price.change_5d < -10:
        watchpoints.append("Wait for selling pressure to stabilize; the 5-day move is still sharply negative.")
    if price.above_5d_low > 2:
        watchpoints.append("Use the recent 5-day low as the first invalidation level for the rebound thesis.")
    else:
        watchpoints.append("Look for a close back above the event-day midpoint before treating it as stabilizing.")
    return watchpoints


def score_deep_dive(candidate: Candidate) -> tuple[float, list[str], list[str]]:
    category_counts = count_categories(candidate.events)
    reasons = []
    risks = []
    score = 0.0

    if category_counts.get("earnings_recoverable"):
        score += 18
        reasons.append("The main event is tied to earnings, guidance, revenue, or margin, which can be checked against the next report.")
    if category_counts.get("analyst_negative") and (
        category_counts.get("analyst_positive") or category_counts.get("earnings_recoverable")
    ):
        score += 12
        reasons.append("There is a negative catalyst, but it appears debatable rather than one-sided because offsetting events also appeared.")
    if category_counts.get("analyst_positive") or category_counts.get("company_action_positive"):
        score += 10
        reasons.append("A constructive analyst or company-action signal appeared after the selloff.")
    if -30 <= candidate.price.drawdown_60d <= -10:
        score += 16
        reasons.append("The drawdown is large enough to matter but not so extreme that the screen treats it as likely structural damage.")
    elif candidate.price.drawdown_60d < -30:
        score += 6
        risks.append("The drawdown is very deep, so the market may be pricing in more than a temporary event.")
    if candidate.price.change_20d < -5:
        score += 8
        reasons.append("The recent 20-day selloff gives the setup a clear event-driven repricing window.")
    if candidate.price.above_5d_low >= 2:
        score += 14
        reasons.append("The stock has started to lift from its 5-day low, which is a first sign that selling pressure may be cooling.")
    else:
        risks.append("There is not enough short-term stabilization yet; it may still be too early.")
    if candidate.price.change_5d < -10:
        score -= 12
        risks.append("The 5-day move is still sharply negative, so this can still be a falling-knife setup.")
    if candidate.price.volume_ratio_5d_20d > 1.2:
        score += 6
        reasons.append("Volume expanded around the move, suggesting the market is actively repricing the event.")

    legal = category_counts.get("legal_regulatory", 0)
    terminal = category_counts.get("terminal_risk", 0)
    if legal:
        score -= legal * 12
        risks.append("Legal or regulatory headlines make the downside harder to bound.")
    if terminal:
        score -= terminal * 40
        risks.append("Terminal-risk language appeared; this should not be a focus candidate without primary-source confirmation.")

    specific_events = [
        event for event in candidate.events
        if any(category != "macro_sector" for category in event.categories)
    ]
    if len(specific_events) >= 3:
        score += 8
        reasons.append("There are multiple company-specific headlines, so the setup is easier to audit than a broad macro move.")
    elif len(specific_events) == 1:
        score -= 6
        risks.append("Only one company-specific headline passed the filter, so the evidence base is thin.")

    score += candidate.fundamentals.business_quality_score
    score += candidate.fundamentals.valuation_score
    score -= candidate.fundamentals.structural_risk_penalty
    if candidate.fundamentals.business_quality_score:
        reasons.append(
            f"Business quality adds {candidate.fundamentals.business_quality_score:.1f} points based on SEC company facts."
        )
    if candidate.fundamentals.valuation_score:
        reasons.append(
            f"Valuation adds {candidate.fundamentals.valuation_score:.1f} points based on SEC-derived multiples."
        )
    if candidate.fundamentals.structural_risk_penalty:
        risks.append(
            f"Structural risk subtracts {candidate.fundamentals.structural_risk_penalty:.1f} points."
        )

    if not reasons:
        reasons.append("It remains on the research list, but the deep-dive layer did not find a strong reason to prioritize it.")
    if not risks:
        risks.append("The largest risk is headline interpretation; verify with primary filings or the latest earnings call.")

    return round(score, 2), reasons, risks


def price_sources_match(primary: PriceStats, secondary: PriceStats) -> tuple[bool, str]:
    close_delta = abs(primary.last_close / secondary.last_close - 1) * 100
    drawdown_delta = abs(primary.drawdown_60d - secondary.drawdown_60d)
    change_20d_delta = abs(primary.change_20d - secondary.change_20d)
    status = (
        f"Yahoo vs Stooq: close delta {close_delta:.2f}%, "
        f"60-day drawdown delta {drawdown_delta:.2f} pts, "
        f"20-day change delta {change_20d_delta:.2f} pts"
    )
    return close_delta <= 1.0 and drawdown_delta <= 3.0 and change_20d_delta <= 3.0, status


def build_data_confidence(candidate: Candidate, sec_filings, secondary_price: PriceStats | None) -> DataConfidence:
    reasons = []
    score = 0
    price_source_status = "Stooq price check unavailable"

    if sec_filings:
        score += 2
        forms = ", ".join(sorted({filing.form for filing in sec_filings}))
        reasons.append(f"SEC recent filings found ({forms}), giving a primary-source audit trail.")
    else:
        reasons.append("No recent 8-K/10-Q/10-K style SEC filing found within the lookback window.")

    if secondary_price:
        matches, price_source_status = price_sources_match(candidate.price, secondary_price)
        if matches:
            score += 2
            reasons.append("Yahoo and Stooq price calculations are broadly consistent.")
        else:
            reasons.append("Yahoo and Stooq price calculations diverge enough to require manual price verification.")
    else:
        reasons.append("Second price source was unavailable, so the price signal relies on Yahoo only.")

    category_counts = count_categories(candidate.events)
    specific_events = [
        event for event in candidate.events
        if any(category != "macro_sector" for category in event.categories)
    ]
    if len(specific_events) >= 3:
        score += 1
        reasons.append("Multiple company-specific headlines support the event trail.")
    elif category_counts.get("macro_sector") == len(candidate.events):
        reasons.append("Event trail is mostly macro/sector commentary rather than company-specific evidence.")

    if score >= 4:
        level = "High"
    elif score >= 2:
        level = "Medium"
    else:
        level = "Low"

    return DataConfidence(
        level=level,
        reasons=reasons,
        sec_filings=sec_filings,
        secondary_price=secondary_price,
        price_source_status=price_source_status,
    )


def load_sec_ticker_map_safely() -> dict[str, str]:
    try:
        return load_sec_ticker_map()
    except Exception:
        return {}


def apply_fundamental_scores(candidates, cik_by_ticker: dict[str, str], sleep_seconds: float):
    for candidate in candidates:
        try:
            facts = fetch_sec_company_facts(candidate.ticker, cik_by_ticker)
            candidate.fundamentals = score_fundamentals(candidate, facts)
            import time

            time.sleep(sleep_seconds)
        except Exception:
            candidate.fundamentals = FundamentalScore(source_status="SEC company facts unavailable")
    return candidates


def apply_data_confidence(candidates, lookback_days: int, sleep_seconds: float, cik_by_ticker: dict[str, str] | None = None):
    import time

    cik_by_ticker = cik_by_ticker or load_sec_ticker_map_safely()
    for candidate in candidates:
        sec_filings = []
        secondary_price = None
        try:
            sec_filings = fetch_recent_sec_filings(candidate.ticker, cik_by_ticker, lookback_days)
            time.sleep(sleep_seconds)
        except Exception:
            sec_filings = []
        try:
            secondary_price = fetch_stooq_price_stats(candidate.ticker)
            time.sleep(sleep_seconds)
        except Exception:
            secondary_price = None
        candidate.data_confidence = build_data_confidence(candidate, sec_filings, secondary_price)
    return candidates


def apply_deep_dive(candidates, focus_count: int):
    for candidate in candidates:
        score, reasons, risks = score_deep_dive(candidate)
        candidate.deep_dive_score = score
        candidate.deep_dive_reasons = reasons
        candidate.deep_dive_risks = risks

    ranked = sorted(candidates, key=lambda item: item.deep_dive_score, reverse=True)
    focus_eligible = [
        candidate for candidate in ranked
        if (
            candidate.deep_dive_score > 0
            and candidate.fundamentals.structural_risk_penalty <= 25
            and candidate.fundamentals.business_quality_score >= 8
            and (
                candidate.fundamentals.valuation_score >= 8
                or candidate.fundamentals.business_quality_score >= 18
            )
        )
    ]
    focus_tickers = {candidate.ticker for candidate in focus_eligible[:focus_count]}
    for candidate in candidates:
        if candidate.ticker in focus_tickers:
            candidate.deep_dive_decision = "Focus"
        elif candidate.fundamentals.structural_risk_penalty > 30:
            candidate.deep_dive_decision = "Pass"
        elif candidate.deep_dive_score >= 35:
            candidate.deep_dive_decision = "Watch"
        else:
            candidate.deep_dive_decision = "Pass"
    return candidates


def score_candidate(ticker: str, news: list[NewsItem], price: PriceStats) -> Candidate:
    category_counts = count_categories(news)

    specific_events = [
        item for item in news
        if any(category != "macro_sector" for category in item.categories)
    ]
    macro_event_count = len(news) - len(specific_events)
    negative_event_count = sum(1 for item in specific_events if item.sentiment < 0)
    positive_event_count = sum(1 for item in specific_events if item.sentiment > 0)
    terminal = category_counts.get("terminal_risk", 0)
    legal = category_counts.get("legal_regulatory", 0)

    breakdown: dict[str, float] = {}
    add_score(breakdown, "company-specific event count", min(len(specific_events), 6) * 4)
    add_score(breakdown, "macro/sector event count", min(macro_event_count, 3) * 1)
    add_score(breakdown, "negative event catalyst", min(negative_event_count, 4) * 6)
    add_score(breakdown, "positive offsetting catalyst", min(positive_event_count, 3) * 2)
    add_score(
        breakdown,
        "60-day drawdown",
        min(abs(price.drawdown_60d), 35) * 1.1 if price.drawdown_60d < -8 else -8,
    )
    add_score(
        breakdown,
        "20-day selloff",
        min(abs(price.change_20d), 25) * 0.8 if price.change_20d < -5 else -5,
    )
    add_score(breakdown, "bounce from 5-day low", min(price.above_5d_low, 10) * 2.0)
    add_score(breakdown, "recent volume expansion", min(max(price.volume_ratio_5d_20d - 1, 0), 3) * 4)

    if category_counts.get("earnings_recoverable"):
        add_score(breakdown, "recoverable earnings/guidance event", 8)
    if category_counts.get("analyst_positive") or category_counts.get("company_action_positive"):
        add_score(breakdown, "constructive analyst/company action", 6)
    if category_counts.get("earnings_miss"):
        add_score(breakdown, "earnings disappointment catalyst", 5)
    if legal:
        add_score(breakdown, "legal/regulatory penalty", legal * -8)
    if terminal:
        add_score(breakdown, "terminal-risk penalty", terminal * -30)
    if price.change_5d < -12 and price.above_5d_low < 2:
        add_score(breakdown, "falling-knife penalty", -12)
    if not specific_events:
        add_score(breakdown, "weak company-specific event penalty", -25)

    score = sum(breakdown.values())

    risks = []
    if terminal:
        risks.append("terminal-risk language appeared in recent event headlines")
    if legal:
        risks.append("legal/regulatory event may be hard to handicap")
    if price.change_5d < -10:
        risks.append("short-term price action is still falling sharply")
    if price.drawdown_60d < -25:
        risks.append("deep drawdown may reflect real fundamental damage")
    if not risks:
        risks.append("event interpretation may be noisy; read the primary source")

    if terminal:
        bucket = "D"
    elif score >= 70:
        bucket = "A"
    elif score >= 50:
        bucket = "B"
    elif score >= 30:
        bucket = "C"
    else:
        bucket = "D"

    thesis_parts = []
    if price.drawdown_60d < -8:
        thesis_parts.append(f"{price.drawdown_60d:.1f}% below its 60-day closing high")
    if price.above_5d_low > 2:
        thesis_parts.append(f"{price.above_5d_low:.1f}% above its 5-day low")
    if category_counts:
        thesis_parts.append("recent events: " + ", ".join(top_category_labels(category_counts)))
    thesis = "; ".join(thesis_parts) or "event activity detected but signal is weak"
    reasons = build_reasons(news, category_counts, price, negative_event_count, positive_event_count)
    watchpoints = build_watchpoints(category_counts, price)

    return Candidate(
        ticker=ticker,
        score=round(score, 2),
        bucket=bucket,
        thesis=thesis,
        reasons=reasons,
        risks=risks,
        watchpoints=watchpoints,
        score_breakdown=breakdown,
        events=news,
        price=price,
    )
