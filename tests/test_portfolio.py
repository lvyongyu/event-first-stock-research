#!/usr/bin/env python3
"""Unit tests for the paper-portfolio benchmark math (efsr.portfolio).

Pure functions, no network: the SPY history is injected.
"""
from __future__ import annotations

import os
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)

from efsr import portfolio  # noqa: E402


def test_close_on_or_before_handles_gaps():
    history = {"2026-06-01": 100.0, "2026-06-03": 102.0, "2026-06-05": 101.0}
    assert portfolio._close_on_or_before(history, "2026-06-04") == 102.0  # weekend -> prior close
    assert portfolio._close_on_or_before(history, "2026-06-01") == 100.0
    assert portfolio._close_on_or_before(history, "2026-05-30") is None    # before history


def test_compute_benchmark_returns_and_beat_count():
    spy = {"2026-06-01": 100.0, "2026-06-10": 110.0}  # +10% over the window
    snapshots = [
        {"buy_date": "2026-06-01", "notional": 100.0, "return_pct": 20.0},  # beats SPY
        {"buy_date": "2026-06-01", "notional": 100.0, "return_pct": 5.0},   # lags SPY
    ]
    b = portfolio.compute_benchmark(snapshots, spy)
    assert b is not None
    assert b["benchmark_ticker"] == "SPY"
    assert b["benchmark_return_pct"] == 10.0
    assert b["positions_covered"] == 2
    assert b["positions_beating_benchmark"] == 1


def test_compute_benchmark_none_when_no_data():
    assert portfolio.compute_benchmark([], {"2026-06-01": 100.0}) is None
    assert portfolio.compute_benchmark(
        [{"buy_date": "2026-06-01", "notional": 100.0, "return_pct": 0.0}], {}
    ) is None
