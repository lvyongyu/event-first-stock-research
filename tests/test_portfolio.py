#!/usr/bin/env python3
"""Unit tests for the paper-portfolio benchmark math (efsr.portfolio).

Pure functions, no network: the SPY history is injected.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from types import SimpleNamespace

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


# ---------------------------------------------------------------------------
# Staged stop-loss: pure decision function
# ---------------------------------------------------------------------------
def test_evaluate_stop_holds_within_hard_stop():
    r = portfolio.evaluate_stop(100.0, 95.0, 100.0, False)  # -5%, above -10% floor
    assert r["triggered"] is False
    assert r["activated"] is False
    assert r["high_price"] == 100.0


def test_evaluate_stop_fires_hard_stop_at_threshold():
    r = portfolio.evaluate_stop(100.0, 90.0, 100.0, False)  # exactly -10%
    assert r["triggered"] is True
    assert r["reason"] == "hard_stop"
    assert r["activated"] is False


def test_evaluate_stop_arms_trailing_after_activation():
    r = portfolio.evaluate_stop(100.0, 112.0, 100.0, False)  # +12% -> arm trailing
    assert r["activated"] is True
    assert r["triggered"] is False           # 112 well above peak*0.9 = 100.8
    assert r["high_price"] == 112.0
    assert abs(r["stop_level"] - 100.8) < 1e-9


def test_evaluate_stop_trailing_fires_on_pullback_from_peak():
    # Peak already 120 (armed); price pulls back to 108 == 120*0.9 -> exit
    r = portfolio.evaluate_stop(100.0, 108.0, 120.0, True)
    assert r["triggered"] is True
    assert r["reason"] == "trailing_stop"


def test_evaluate_stop_hard_floor_never_triggers_a_winner():
    # A winner that never pulled back 10% from its peak keeps running.
    r = portfolio.evaluate_stop(100.0, 118.0, 120.0, True)  # only -1.7% off peak
    assert r["triggered"] is False
    assert r["high_price"] == 120.0


# ---------------------------------------------------------------------------
# Migration: old databases gain stop columns and backfill the trailing peak
# ---------------------------------------------------------------------------
def _make_legacy_db(path):
    """A pre-stop-loss database: positions without the stop columns, plus a snapshot
    history that records the position's peak (as real legacy databases do)."""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL UNIQUE, buy_date TEXT NOT NULL, created_at TEXT NOT NULL,
            notional REAL NOT NULL, price REAL NOT NULL, shares REAL NOT NULL,
            agent_decision TEXT NOT NULL, agent_review_score REAL NOT NULL, agent_risk TEXT NOT NULL,
            deep_dive_decision TEXT NOT NULL, deep_dive_score REAL NOT NULL, original_score REAL NOT NULL,
            evidence_quality REAL NOT NULL, thesis TEXT NOT NULL, main_risk TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE position_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_date TEXT NOT NULL, created_at TEXT NOT NULL,
            ticker TEXT NOT NULL, buy_date TEXT NOT NULL, holding_days INTEGER NOT NULL,
            notional REAL NOT NULL, entry_price REAL NOT NULL, current_price REAL NOT NULL,
            shares REAL NOT NULL, market_value REAL NOT NULL, unrealized_pnl REAL NOT NULL,
            return_pct REAL NOT NULL, status TEXT NOT NULL, UNIQUE(run_date, ticker)
        )
        """
    )
    conn.execute(
        "INSERT INTO positions (ticker, buy_date, created_at, notional, price, shares, "
        "agent_decision, agent_review_score, agent_risk, deep_dive_decision, deep_dive_score, "
        "original_score, evidence_quality, thesis, main_risk, status) VALUES "
        "('OLD','2026-06-01','t',100.0,100.0,1.0,'Watch',0,'Medium','Watch',0,0,0,'t','r','open')"
    )
    conn.execute(
        "INSERT INTO position_snapshots (run_date, created_at, ticker, buy_date, holding_days, "
        "notional, entry_price, current_price, shares, market_value, unrealized_pnl, return_pct, status) "
        "VALUES ('2026-06-05','t','OLD','2026-06-01',4,100.0,100.0,130.0,1.0,130.0,30.0,30.0,'open')"
    )
    conn.commit()
    conn.close()


def test_migration_backfills_high_price_from_snapshots(tmp_path):
    db = str(tmp_path / "legacy.sqlite")
    _make_legacy_db(db)
    conn = portfolio.connect(db)  # runs init_db + _migrate_positions -> backfill from snapshots
    row = conn.execute("SELECT high_price, stop_activated FROM positions WHERE ticker='OLD'").fetchone()
    conn.close()
    assert row[0] == 130.0                 # backfilled to the observed peak
    assert row[1] == 1                     # 130 >= 100 * 1.10 -> trailing armed


# ---------------------------------------------------------------------------
# End-to-end: a position stops out, is realized, and is never re-bought
# ---------------------------------------------------------------------------
def test_stop_out_closes_realizes_and_blocks_rebuy(tmp_path, monkeypatch):
    monkeypatch.setattr(portfolio, "fetch_close_history", lambda *a, **k: {})  # no benchmark/network
    db = str(tmp_path / "p.sqlite")
    conn = portfolio.connect(db)
    portfolio.insert_position(conn, {
        "ticker": "QCOM", "buy_date": "2026-06-25", "created_at": "t",
        "notional": 100.0, "price": 100.0, "shares": 1.0,
        "agent_decision": "Watch", "agent_review_score": 0.0, "agent_risk": "Medium",
        "deep_dive_decision": "Watch", "deep_dive_score": 0.0, "original_score": 0.0,
        "evidence_quality": 0.0, "thesis": "t", "main_risk": "r", "status": "open",
        "high_price": 100.0, "stop_activated": 0,
    })
    conn.close()

    # Mark at $88 (-12%) with no candidate price -> falls back to (stubbed) live price.
    monkeypatch.setattr(portfolio, "fetch_price_stats", lambda t: SimpleNamespace(last_close=88.0))
    res = portfolio.update_portfolio_performance(db, [], run_date="2026-07-01")

    assert res["open_positions"] == 0
    assert res["closed_positions"] == 1
    assert res["realized_pnl_total"] == -12.0
    assert [s["ticker"] for s in res["stopped_this_run"]] == ["QCOM"]
    assert res["stopped_this_run"][0]["exit_reason"] == "hard_stop"

    conn = portfolio.connect(db)
    assert "QCOM" in portfolio.held_tickers(conn)     # still blocked from re-buy
    status = conn.execute("SELECT status FROM positions WHERE ticker='QCOM'").fetchone()[0]
    conn.close()
    assert status == "stopped"
