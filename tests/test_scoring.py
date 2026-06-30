#!/usr/bin/env python3
"""Unit tests for the deterministic scoring core (efsr.scoring).

Pure functions, no network. These pin the behavior of the project's IP — the
event screen, fundamental scoring, deep-dive, and data-confidence logic — so a
weight or threshold change can't drift silently.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)

from efsr import scoring  # noqa: E402
from efsr.models import FilingItem, NewsItem, PriceStats  # noqa: E402


def _news(title, categories, sentiment=0, link="http://x"):
    return NewsItem(title, link, None, categories, sentiment)


def _price(last=100.0, c5=-3.0, c20=-9.0, dd=-18.0, lift=3.0, vol=1.4):
    return PriceStats(last_close=last, change_5d=c5, change_20d=c20,
                      drawdown_60d=dd, above_5d_low=lift, volume_ratio_5d_20d=vol)


def _facts():
    """Minimal SEC companyfacts payload that drives score_fundamentals."""
    def fy(val, year):
        return {"fy": year, "fp": "FY", "val": val, "filed": f"{year + 1}-02-01"}
    return {"facts": {
        "us-gaap": {
            "Revenues": {"units": {"USD": [fy(1000, 2024), fy(900, 2023)]}},
            "NetIncomeLoss": {"units": {"USD": [fy(200, 2024)]}},
            "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [fy(300, 2024)]}},
            "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [fy(50, 2024)]}},
            "Assets": {"units": {"USD": [fy(2000, 2024)]}},
            "Liabilities": {"units": {"USD": [fy(800, 2024)]}},
        },
        "dei": {
            "EntityCommonStockSharesOutstanding": {"units": {"shares": [{"val": 100, "filed": "2025-02-01"}]}},
        },
    }}


# --- event categorization / labels ---

def test_count_categories_and_top_labels():
    news = [_news("a", ["earnings_recoverable"]), _news("b", ["earnings_recoverable"]),
            _news("c", ["legal_regulatory"])]
    counts = scoring.count_categories(news)
    assert counts == {"earnings_recoverable": 2, "legal_regulatory": 1}
    assert scoring.top_category_labels(counts, 1) == [scoring.event_label("earnings_recoverable")]


def test_add_score_accumulates_and_rounds():
    bd: dict[str, float] = {}
    scoring.add_score(bd, "x", 1.111)
    scoring.add_score(bd, "x", 2.0)
    assert bd["x"] == 3.11


# --- first-pass event score ---

def test_terminal_risk_is_bucket_d():
    c = scoring.score_candidate("T", [_news("bankruptcy filing, shares halted", ["terminal_risk"], -3)],
                                _price(c5=-30, c20=-40, dd=-55, lift=0.5))
    assert c.bucket == "D"


def test_macro_only_event_is_penalized():
    c = scoring.score_candidate("M", [_news("sector tariff news", ["macro_sector"], 0)], _price())
    assert "weak company-specific event penalty" in c.score_breakdown
    assert c.score_breakdown["weak company-specific event penalty"] == -25


# --- fundamentals ---

def test_score_fundamentals_pinned():
    c = scoring.score_candidate("F", [_news("earnings update", ["earnings_recoverable"], 0)],
                                _price(last=10.0))
    fs = scoring.score_fundamentals(c, _facts())
    assert fs.source_status == "SEC company facts"
    assert (fs.business_quality_score, fs.valuation_score, fs.structural_risk_penalty) == (30.0, 25.0, 0.0)
    assert round(fs.metrics["revenue_growth"], 3) == 0.111
    assert fs.metrics["net_margin"] == 0.2
    assert fs.metrics["price_to_earnings"] == 5.0


def test_score_fundamentals_without_facts():
    c = scoring.score_candidate("F", [_news("earnings update", ["earnings_recoverable"])], _price())
    fs = scoring.score_fundamentals(c, None)
    assert fs.source_status == "SEC company facts unavailable"
    assert (fs.business_quality_score, fs.valuation_score, fs.structural_risk_penalty) == (0.0, 0.0, 0.0)


# --- deep dive ---

def test_deep_dive_rewards_recoverable_penalizes_terminal():
    base = scoring.score_candidate("D", [_news("revenue guidance", ["earnings_recoverable"])],
                                   _price(c20=-9, lift=3))
    good, _, _ = scoring.score_deep_dive(base)
    term = scoring.score_candidate("D", [_news("going concern, delisting", ["terminal_risk"])],
                                   _price(c20=-9, lift=3))
    bad, _, _ = scoring.score_deep_dive(term)
    assert good > bad


# --- data confidence ---

def test_data_confidence_high_vs_low():
    c = scoring.score_candidate("H", [_news("earnings", ["earnings_recoverable"]),
                                      _news("guidance", ["earnings_recoverable"]),
                                      _news("margin", ["earnings_recoverable"])], _price())
    filings = [FilingItem("8-K", "2026-06-20", "2026-06-20", "acc", "8-K")]
    high = scoring.build_data_confidence(c, filings, c.price)  # identical secondary price -> match
    assert high.level == "High"

    macro = scoring.score_candidate("L", [_news("tariffs", ["macro_sector"])], _price())
    low = scoring.build_data_confidence(macro, [], None)
    assert low.level == "Low"


def test_price_sources_match():
    primary = _price(last=100.0, dd=-18.0, c20=-9.0)
    matched, _ = scoring.price_sources_match(primary, _price(last=100.5, dd=-18.0, c20=-9.0))
    assert matched is True
    mismatched, _ = scoring.price_sources_match(primary, _price(last=120.0, dd=-30.0, c20=-20.0))
    assert mismatched is False
