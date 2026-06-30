#!/usr/bin/env python3
"""Offline smoke test for the efsr package.

No network, no API key, does not touch the paper portfolio DB. Run with either:

    python3 tests/test_smoke_offline.py
    pytest

It guards the failure modes that matter after the modularization + packaging:
- the package wires up (every submodule imports);
- "single source of truth" — the CLI reuses scoring.py / formatting.py rather
  than a stale duplicate (the silent-divergence trap);
- deterministic scoring stays pinned (catches accidental weight changes);
- both agent implementations run and serialize through reporting cleanly.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import sys
import tempfile

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)

from efsr import cli, formatting, prompts, reporting, scoring  # noqa: E402
from efsr.models import (  # noqa: E402
    DataConfidence, FilingItem, FundamentalScore, NewsItem, PriceStats,
)

UTC = dt.timezone.utc

PACKAGE_MODULES = [
    "efsr", "efsr.cli", "efsr.email_report", "efsr.models", "efsr.formatting",
    "efsr.sources", "efsr.scoring", "efsr.prompts", "efsr.reporting",
    "efsr.portfolio", "efsr.agents", "efsr.agents.runtime", "efsr.agents.legacy",
]


def _news():
    return [
        NewsItem("Acme misses on revenue, lowers guidance after weak quarter", "http://a/1",
                 dt.datetime(2026, 6, 20, tzinfo=UTC), ["earnings_miss", "earnings_recoverable"], -2),
        NewsItem("Analyst upgrades Acme to buy rating on valuation", "http://a/2",
                 dt.datetime(2026, 6, 22, tzinfo=UTC), ["analyst_positive"], 1),
        NewsItem("Acme faces lawsuit and FTC probe over pricing", "http://a/3",
                 dt.datetime(2026, 6, 23, tzinfo=UTC), ["legal_regulatory"], -1),
        NewsItem("Sector tariff and chip news weigh on AI names", "http://a/4",
                 dt.datetime(2026, 6, 24, tzinfo=UTC), ["macro_sector"], 0),
    ]


def _price():
    return PriceStats(last_close=100.0, change_5d=-13.0, change_20d=-9.5,
                      drawdown_60d=-18.0, above_5d_low=3.2, volume_ratio_5d_20d=1.6)


def _enriched_candidate():
    c = scoring.score_candidate("ACME", _news(), _price())
    c.fundamentals = FundamentalScore(22.0, 12.0, 5.0, ["good biz"], ["minor risk"],
                                      {"revenue_growth": 0.1}, "SEC company facts")
    c.data_confidence = DataConfidence("High", ["sec ok"],
                                       [FilingItem("8-K", "2026-06-20", "2026-06-20", "acc", "8-K filed")],
                                       None, "ok")
    c.deep_dive_score, c.deep_dive_reasons, c.deep_dive_risks = scoring.score_deep_dive(c)
    c.deep_dive_decision = "Focus"
    return c


def test_package_imports():
    # Every submodule imports — catches packaging / import-path breakage.
    for name in PACKAGE_MODULES:
        importlib.import_module(name)


def test_single_source_of_truth():
    # The CLI must reuse the extracted modules, not a re-introduced duplicate.
    assert cli.score_candidate is scoring.score_candidate, "score_candidate diverged from scoring"
    assert scoring.pct is formatting.pct, "scoring.pct is not the shared formatting.pct"
    assert prompts.multiple is formatting.multiple, "prompts.multiple is not shared"
    assert reporting.pct is formatting.pct, "reporting.pct is not the shared formatting.pct"


def test_score_candidate_pinned():
    # Pinned values catch silent changes to the deterministic event score.
    mixed = scoring.score_candidate("ACME", _news(), _price())
    assert (mixed.score, mixed.bucket) == (74.2, "A"), (mixed.score, mixed.bucket)
    assert mixed.reasons and mixed.risks and mixed.score_breakdown

    terminal_news = [NewsItem("Globex bankruptcy and going concern doubt; shares halted",
                              "x", None, ["terminal_risk"], -3)]
    terminal_price = PriceStats(4.0, -30.0, -40.0, -55.0, 0.5, 3.0)
    term = scoring.score_candidate("GLBX", terminal_news, terminal_price)
    assert (term.score, term.bucket) == (35.5, "D"), (term.score, term.bucket)


def test_legacy_agent_runs_and_serializes():
    # runtime fetches SEC data (network) -> only the legacy impl is run offline.
    c = _enriched_candidate()
    args = cli.parse_args(["--agent-impl", "legacy", "--agent-mode", "deterministic",
                           "--agent-review-count", "1"])
    cli.apply_agent_reviews([c], args)
    r = c.agent_review
    assert r.decision in {"Focus", "Watch", "Pass", "Blocked"}, r.decision
    assert 0.0 <= r.evidence_quality <= 1.0, r.evidence_quality
    assert len(r.agent_results) == 7, len(r.agent_results)

    with tempfile.TemporaryDirectory() as tmp:
        json_path, md_path = reporting.write_outputs([c], os.path.join(tmp, "report"))
        assert os.path.exists(json_path) and os.path.exists(md_path)
        payload = json.loads(open(json_path, encoding="utf-8").read())
        assert payload["candidates"][0]["ticker"] == "ACME"
        assert payload["candidates"][0]["agent_review"]["decision"] == r.decision


def test_agent_impl_routing():
    seen = []
    real_runtime, real_legacy = cli.run_agent_reviews, cli.apply_agent_reviews_legacy
    cli.run_agent_reviews = lambda c, **k: (seen.append("runtime"), c)[1]
    cli.apply_agent_reviews_legacy = lambda c, **k: (seen.append("legacy"), c)[1]
    try:
        for argv, _ in (([], "runtime"), (["--agent-impl", "legacy"], "legacy"),
                        (["--agent-impl", "runtime"], "runtime")):
            cli.apply_agent_reviews([], cli.parse_args(argv))
        assert seen == ["runtime", "legacy", "runtime"], seen
    finally:
        cli.run_agent_reviews, cli.apply_agent_reviews_legacy = real_runtime, real_legacy


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
