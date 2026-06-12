#!/usr/bin/env python3
"""Event-only daily US stock bottom-fishing screener.

This script intentionally avoids valuation and broad multi-factor models.
It uses public, no-key data sources and produces a research watchlist.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Iterable

from llm_prompts import (
    OPENAI_REVIEW_SYSTEM_PROMPT,
    build_llm_review_prompt,
    compact_text,
    estimate_tokens,
    multiple,
    pct,
)
from agent_runtime import apply_agent_reviews as run_agent_reviews
from reporting import write_outputs as render_outputs


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_UNIVERSE = os.path.join(ROOT, "config", "universe_sp100.txt")
DEFAULT_ALIASES = os.path.join(ROOT, "config", "company_aliases.json")
OUTPUT_DIR = os.path.join(ROOT, "outputs")
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_USER_AGENT = "auto-trading-research/0.1 lvyongyu@gmail.com"

EVENT_KEYWORDS = {
    "earnings_miss": {
        "miss", "misses", "missed", "disappoint", "disappoints", "weak guidance",
        "cuts forecast", "cut forecast", "lowers guidance", "guidance cut",
    },
    "earnings_recoverable": {
        "earnings", "revenue", "sales", "guidance", "margin", "forecast",
        "outlook", "profit", "free cash flow",
    },
    "analyst_negative": {
        "downgrade", "downgrades", "price target cut", "target cut", "sell rating",
        "underperform", "bearish",
    },
    "analyst_positive": {
        "upgrade", "upgrades", "price target raised", "target raised", "buy rating",
        "outperform", "bullish",
    },
    "company_action_positive": {
        "buyback", "repurchase", "dividend increase", "raises dividend", "spin off",
        "spinoff", "strategic review", "asset sale", "activist",
    },
    "legal_regulatory": {
        "lawsuit", "sues", "sued", "investigation", "probe", "doj", "ftc", "sec",
        "antitrust", "fda", "warning letter", "recall",
    },
    "terminal_risk": {
        "bankruptcy", "chapter 11", "going concern", "delisting", "fraud",
        "accounting irregularities", "restatement", "halted", "insolvency",
    },
    "macro_sector": {
        "tariff", "rates", "inflation", "oil", "chip", "semiconductor", "ai",
        "consumer spending", "housing", "drug pricing",
    },
}

NEGATIVE_WORDS = {
    "miss", "misses", "missed", "falls", "drops", "tumbles", "plunges", "slumps",
    "cuts", "cut", "lowers", "downgrade", "downgrades", "probe", "investigation",
    "lawsuit", "recall", "weak", "disappointing", "concern", "pressure",
}

POSITIVE_WORDS = {
    "beats", "beat", "raises", "raised", "upgrade", "upgrades", "surges", "jumps",
    "buyback", "repurchase", "dividend", "approval", "record", "strong",
}


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
    llm_mode: str = "deterministic"
    llm_provider: str = "deterministic"
    llm_notes: str = ""
    agent_plan: list = dataclasses.field(default_factory=list)
    tool_trace: list = dataclasses.field(default_factory=list)


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


def fetch_url(url: str, timeout: int = 12) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 event-bottom-fishing-agent/0.1",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def fetch_sec_url(url: str, timeout: int = 12) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": SEC_USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def load_universe(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as handle:
        tickers = []
        for line in handle:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                tickers.append(stripped.upper())
        return tickers


def load_aliases(path: str) -> dict[str, list[str]]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return {str(key).upper(): [str(item) for item in value] for key, value in payload.items()}


def parse_rss_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def categorize(title: str) -> list[str]:
    lower = title.lower()
    categories = []
    for category, words in EVENT_KEYWORDS.items():
        if any(word in lower for word in words):
            categories.append(category)
    return categories


def headline_sentiment(title: str) -> int:
    words = set(re.findall(r"[a-z0-9]+", title.lower()))
    neg = len(words & NEGATIVE_WORDS)
    pos = len(words & POSITIVE_WORDS)
    return max(-3, min(3, pos - neg))


def is_relevant_title(ticker: str, aliases: list[str], title: str) -> bool:
    lower = title.lower()
    normalized_ticker = ticker.replace("-", ".").lower()
    ticker_pattern = re.compile(rf"(?<![a-z0-9]){re.escape(normalized_ticker)}(?![a-z0-9])")
    if ticker_pattern.search(lower):
        return True
    return any(alias.lower() in lower for alias in aliases)


def fetch_news(
    ticker: str,
    aliases: list[str],
    max_items: int,
    lookback_days: int,
    allow_broad_news: bool,
) -> list[NewsItem]:
    symbol = ticker.replace("-", ".")
    params = urllib.parse.urlencode({"s": symbol, "region": "US", "lang": "en-US"})
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?{params}"
    raw = fetch_url(url)
    root = ET.fromstring(raw)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
    items = []
    for item in root.findall("./channel/item"):
        title = html.unescape((item.findtext("title") or "").strip())
        if not allow_broad_news and not is_relevant_title(ticker, aliases, title):
            continue
        link = (item.findtext("link") or "").strip()
        published = parse_rss_date(item.findtext("pubDate"))
        if published and published < cutoff:
            continue
        categories = categorize(title)
        if not categories:
            continue
        items.append(
            NewsItem(
                title=title,
                link=link,
                published=published,
                categories=categories,
                sentiment=headline_sentiment(title),
            )
        )
        if len(items) >= max_items:
            break
    return items


def fetch_price_stats(ticker: str) -> PriceStats | None:
    symbol = urllib.parse.quote(ticker.replace("-", "-"))
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{symbol}?range=6mo&interval=1d&includePrePost=false"
    )
    raw = fetch_url(url)
    payload = json.loads(raw.decode("utf-8"))
    result = payload.get("chart", {}).get("result") or []
    if not result:
        return None
    quote = result[0]["indicators"]["quote"][0]
    closes = [x for x in quote.get("close", []) if isinstance(x, (int, float))]
    volumes = [x for x in quote.get("volume", []) if isinstance(x, (int, float))]
    if len(closes) < 61 or len(volumes) < 25:
        return None
    last = closes[-1]
    high_60 = max(closes[-60:])
    low_5 = min(closes[-5:])
    avg_vol_5 = sum(volumes[-5:]) / 5
    avg_vol_20 = sum(volumes[-20:]) / 20
    return PriceStats(
        last_close=last,
        change_5d=(last / closes[-6] - 1) * 100,
        change_20d=(last / closes[-21] - 1) * 100,
        drawdown_60d=(last / high_60 - 1) * 100,
        above_5d_low=(last / low_5 - 1) * 100,
        volume_ratio_5d_20d=avg_vol_5 / avg_vol_20 if avg_vol_20 else 0,
    )


def parse_csv_rows(raw: bytes) -> list[dict[str, str]]:
    lines = raw.decode("utf-8").strip().splitlines()
    if len(lines) < 2:
        return []
    headers = lines[0].split(",")
    rows = []
    for line in lines[1:]:
        values = line.split(",")
        if len(values) == len(headers):
            rows.append(dict(zip(headers, values)))
    return rows


def stooq_symbol(ticker: str) -> str:
    return ticker.lower().replace("-", ".") + ".us"


def fetch_stooq_price_stats(ticker: str) -> PriceStats | None:
    end = dt.datetime.now(dt.timezone.utc).date()
    start = end - dt.timedelta(days=220)
    params = urllib.parse.urlencode(
        {
            "s": stooq_symbol(ticker),
            "d1": start.strftime("%Y%m%d"),
            "d2": end.strftime("%Y%m%d"),
            "i": "d",
        }
    )
    url = f"https://stooq.com/q/d/l/?{params}"
    rows = parse_csv_rows(fetch_url(url))
    closes = []
    volumes = []
    for row in rows:
        try:
            closes.append(float(row["Close"]))
            volumes.append(float(row.get("Volume") or 0))
        except (KeyError, TypeError, ValueError):
            continue
    if len(closes) < 61 or len(volumes) < 25:
        return None
    last = closes[-1]
    high_60 = max(closes[-60:])
    low_5 = min(closes[-5:])
    avg_vol_5 = sum(volumes[-5:]) / 5
    avg_vol_20 = sum(volumes[-20:]) / 20
    return PriceStats(
        last_close=last,
        change_5d=(last / closes[-6] - 1) * 100,
        change_20d=(last / closes[-21] - 1) * 100,
        drawdown_60d=(last / high_60 - 1) * 100,
        above_5d_low=(last / low_5 - 1) * 100,
        volume_ratio_5d_20d=avg_vol_5 / avg_vol_20 if avg_vol_20 else 0,
    )


def load_sec_ticker_map() -> dict[str, str]:
    payload = json.loads(fetch_sec_url(SEC_TICKERS_URL).decode("utf-8"))
    ticker_map = {}
    for record in payload.values():
        ticker = str(record.get("ticker", "")).upper()
        cik = str(record.get("cik_str", "")).zfill(10)
        if ticker and cik:
            ticker_map[ticker] = cik
    return ticker_map


def fetch_recent_sec_filings(ticker: str, cik_by_ticker: dict[str, str], lookback_days: int) -> list[FilingItem]:
    cik = cik_by_ticker.get(ticker.upper())
    if not cik:
        return []
    payload = json.loads(fetch_sec_url(SEC_SUBMISSIONS_URL.format(cik=cik)).decode("utf-8"))
    recent = payload.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    accession_numbers = recent.get("accessionNumber", [])
    descriptions = recent.get("primaryDocDescription", [])
    cutoff = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=lookback_days)
    interesting_forms = {"8-K", "10-Q", "10-K", "6-K", "20-F"}
    filings = []
    for index, form in enumerate(forms):
        filing_date = filing_dates[index] if index < len(filing_dates) else ""
        try:
            parsed_date = dt.date.fromisoformat(filing_date)
        except ValueError:
            continue
        if parsed_date < cutoff or form not in interesting_forms:
            continue
        filings.append(
            FilingItem(
                form=form,
                filing_date=filing_date,
                report_date=report_dates[index] if index < len(report_dates) else "",
                accession_number=accession_numbers[index] if index < len(accession_numbers) else "",
                description=descriptions[index] if index < len(descriptions) else "",
            )
        )
        if len(filings) >= 5:
            break
    return filings


def fetch_sec_company_facts(ticker: str, cik_by_ticker: dict[str, str]) -> dict | None:
    cik = cik_by_ticker.get(ticker.upper())
    if not cik:
        return None
    return json.loads(fetch_sec_url(SEC_COMPANY_FACTS_URL.format(cik=cik)).decode("utf-8"))


def fact_units(payload: dict, tag_names: list[str], unit: str = "USD") -> list[dict]:
    facts = payload.get("facts", {})
    for taxonomy in ("us-gaap", "dei"):
        taxonomy_facts = facts.get(taxonomy, {})
        for tag in tag_names:
            units = taxonomy_facts.get(tag, {}).get("units", {})
            if unit in units:
                return units[unit]
    return []


def latest_annual_value(payload: dict, tag_names: list[str], unit: str = "USD") -> tuple[float | None, str | None]:
    values = [
        item for item in fact_units(payload, tag_names, unit)
        if item.get("fy") and item.get("fp") == "FY" and isinstance(item.get("val"), (int, float))
    ]
    values.sort(key=lambda item: (item.get("fy", 0), item.get("filed", "")), reverse=True)
    if not values:
        return None, None
    return float(values[0]["val"]), str(values[0].get("fy"))


def latest_two_annual_values(payload: dict, tag_names: list[str], unit: str = "USD") -> list[tuple[int, float]]:
    values_by_year: dict[int, float] = {}
    values = [
        item for item in fact_units(payload, tag_names, unit)
        if item.get("fy") and item.get("fp") == "FY" and isinstance(item.get("val"), (int, float))
    ]
    values.sort(key=lambda item: (item.get("fy", 0), item.get("filed", "")), reverse=True)
    for item in values:
        year = int(item["fy"])
        values_by_year.setdefault(year, float(item["val"]))
        if len(values_by_year) >= 2:
            break
    return sorted(values_by_year.items(), reverse=True)


def latest_value(payload: dict, tag_names: list[str], unit: str) -> float | None:
    values = [
        item for item in fact_units(payload, tag_names, unit)
        if isinstance(item.get("val"), (int, float))
    ]
    values.sort(key=lambda item: item.get("filed", ""), reverse=True)
    return float(values[0]["val"]) if values else None


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


def add_score(breakdown: dict[str, float], label: str, value: float) -> None:
    breakdown[label] = round(breakdown.get(label, 0.0) + value, 2)


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
    categories = sorted(category_counts, key=category_counts.get, reverse=True)[:limit]
    return [event_label(category) for category in categories]


def count_categories(news: list[NewsItem]) -> dict[str, int]:
    category_counts: dict[str, int] = {}
    for item in news:
        for category in item.categories:
            category_counts[category] = category_counts.get(category, 0) + 1
    return category_counts


def build_reasons(
    news: list[NewsItem],
    category_counts: dict[str, int],
    price: PriceStats,
    negative_event_count: int,
    positive_event_count: int,
) -> list[str]:
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


def build_watchpoints(category_counts: dict[str, int], price: PriceStats) -> list[str]:
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


def build_data_confidence(
    candidate: Candidate,
    sec_filings: list[FilingItem],
    secondary_price: PriceStats | None,
) -> DataConfidence:
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


def apply_data_confidence(
    candidates: list[Candidate],
    lookback_days: int,
    sleep_seconds: float,
    cik_by_ticker: dict[str, str] | None = None,
) -> list[Candidate]:
    cik_by_ticker = cik_by_ticker or load_sec_ticker_map_safely()
    for candidate in candidates:
        sec_filings: list[FilingItem] = []
        secondary_price: PriceStats | None = None
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


def load_sec_ticker_map_safely() -> dict[str, str]:
    try:
        return load_sec_ticker_map()
    except Exception:
        return {}


def apply_deep_dive(candidates: list[Candidate], focus_count: int) -> list[Candidate]:
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


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def evidence_quality(candidate: Candidate) -> tuple[float, list[str]]:
    category_counts = count_categories(candidate.events)
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


def apply_agent_reviews(candidates: list[Candidate], args: argparse.Namespace) -> list[Candidate]:
    return run_agent_reviews(
        candidates,
        token_budget=args.agent_token_budget,
        mode=args.agent_mode,
        model=args.agent_model,
        max_output_tokens=args.agent_max_output_tokens,
        review_count=args.agent_review_count,
    )


def prepare_selected_candidates(
    candidates: list[Candidate],
    args: argparse.Namespace,
) -> list[Candidate]:
    selected = candidates[: args.top] if args.include_avoid else select_investable_candidates(candidates, args.top)
    cik_by_ticker = load_sec_ticker_map_safely()
    selected = apply_fundamental_scores(selected, cik_by_ticker, args.sleep)
    selected = apply_deep_dive(selected, args.deep_dive_focus)
    if not args.skip_data_confidence:
        selected = apply_data_confidence(selected, args.lookback_days, args.sleep, cik_by_ticker)
    if not args.skip_agent_review:
        selected = apply_agent_reviews(selected, args)
    return selected


def select_investable_candidates(candidates: list[Candidate], top: int) -> list[Candidate]:
    investable = [candidate for candidate in candidates if candidate.bucket != "D"]
    if len(investable) >= top:
        return investable[:top]
    avoid = [candidate for candidate in candidates if candidate.bucket == "D"]
    return (investable + avoid)[:top]


def apply_fundamental_scores(
    candidates: list[Candidate],
    cik_by_ticker: dict[str, str],
    sleep_seconds: float,
) -> list[Candidate]:
    for candidate in candidates:
        try:
            facts = fetch_sec_company_facts(candidate.ticker, cik_by_ticker)
            candidate.fundamentals = score_fundamentals(candidate, facts)
            time.sleep(sleep_seconds)
        except Exception:
            candidate.fundamentals = FundamentalScore(source_status="SEC company facts unavailable")
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
def scan(args: argparse.Namespace) -> list[Candidate]:
    tickers = load_universe(args.universe)
    aliases_by_ticker = load_aliases(args.aliases)
    candidates = []
    for index, ticker in enumerate(tickers, start=1):
        try:
            news = fetch_news(
                ticker,
                aliases_by_ticker.get(ticker, []),
                args.max_news,
                args.lookback_days,
                args.allow_broad_news,
            )
            if not news:
                continue
            price = fetch_price_stats(ticker)
            if not price:
                continue
            candidate = score_candidate(ticker, news, price)
            candidates.append(candidate)
            if args.verbose:
                print(f"[{index}/{len(tickers)}] {ticker}: {candidate.score:.2f}", flush=True)
            time.sleep(args.sleep)
        except Exception as exc:  # noqa: BLE001 - scanner should continue per ticker.
            if args.verbose:
                print(f"[{index}/{len(tickers)}] {ticker}: skipped ({exc})", file=sys.stderr, flush=True)
            continue
    candidates.sort(key=lambda item: item.score, reverse=True)
    return prepare_selected_candidates(candidates, args)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", default=DEFAULT_UNIVERSE)
    parser.add_argument("--aliases", default=DEFAULT_ALIASES)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--lookback-days", type=int, default=14)
    parser.add_argument("--max-news", type=int, default=8)
    parser.add_argument("--deep-dive-focus", type=int, default=3)
    parser.add_argument("--sleep", type=float, default=0.15)
    parser.add_argument("--allow-broad-news", action="store_true")
    parser.add_argument("--include-avoid", action="store_true")
    parser.add_argument("--skip-data-confidence", action="store_true")
    parser.add_argument("--skip-agent-review", action="store_true")
    parser.add_argument(
        "--agent-mode",
        choices=("deterministic", "lean", "full"),
        default=os.environ.get("AGENT_MODE"),
        help="Agent mode: deterministic for no LLM, lean for one final LLM synthesis, full for LLM at every agent step.",
    )
    parser.add_argument(
        "--agent-model",
        default=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        help="OpenAI model used only when --agent-mode is lean or full and OPENAI_API_KEY is set.",
    )
    parser.add_argument(
        "--agent-token-budget",
        type=int,
        default=int(os.environ.get("AGENT_TOKEN_BUDGET", "900")),
        help="Approximate per-candidate prompt token budget for optional LLM review.",
    )
    parser.add_argument(
        "--agent-max-output-tokens",
        type=int,
        default=int(os.environ.get("AGENT_MAX_OUTPUT_TOKENS", "350")),
        help="Maximum output tokens for optional LLM review.",
    )
    parser.add_argument(
        "--agent-review-count",
        "--agent-llm-count",
        dest="agent_review_count",
        type=int,
        default=int(os.environ.get("AGENT_REVIEW_COUNT", os.environ.get("AGENT_LLM_COUNT", "1"))),
        help="Only score this many top candidates with the agent review.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.agent_mode is None:
        legacy_provider = os.environ.get("AGENT_PROVIDER", "deterministic")
        if legacy_provider == "openai":
            args.agent_mode = "full"
        elif os.environ.get("OPENAI_API_KEY"):
            args.agent_mode = "lean"
        else:
            args.agent_mode = "deterministic"

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    candidates = scan(args)
    if not candidates:
        print("No candidates found. Check network access or widen the universe/lookback window.")
        return 1

    today = dt.datetime.now().strftime("%Y-%m-%d")
    path_prefix = os.path.join(OUTPUT_DIR, f"daily_event_bottom_fishing_{today}")
    json_path, md_path = render_outputs(candidates, path_prefix)
    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    print()
    for index, candidate in enumerate(candidates, start=1):
        print(f"{index:>2}. {candidate.ticker:<6} {candidate.bucket} {candidate.score:>6.2f}  {candidate.thesis}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
