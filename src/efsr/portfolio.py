from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

from efsr.sources import fetch_price_stats
from efsr.models import Candidate


DEFAULT_BUY_AMOUNT = 100.0


def _now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL UNIQUE,
            buy_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            notional REAL NOT NULL,
            price REAL NOT NULL,
            shares REAL NOT NULL,
            agent_decision TEXT NOT NULL,
            agent_review_score REAL NOT NULL,
            agent_risk TEXT NOT NULL,
            deep_dive_decision TEXT NOT NULL,
            deep_dive_score REAL NOT NULL,
            original_score REAL NOT NULL,
            evidence_quality REAL NOT NULL,
            thesis TEXT NOT NULL,
            main_risk TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skipped_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            skipped_duplicates TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reports (
            run_date TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            markdown_path TEXT NOT NULL,
            json_path TEXT NOT NULL,
            candidate_count INTEGER NOT NULL,
            top_tickers TEXT NOT NULL,
            paper_buy_status TEXT NOT NULL,
            paper_buy_ticker TEXT NOT NULL,
            paper_buy_result TEXT NOT NULL,
            markdown_body TEXT NOT NULL,
            json_body TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS position_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            ticker TEXT NOT NULL,
            buy_date TEXT NOT NULL,
            holding_days INTEGER NOT NULL,
            notional REAL NOT NULL,
            entry_price REAL NOT NULL,
            current_price REAL NOT NULL,
            shares REAL NOT NULL,
            market_value REAL NOT NULL,
            unrealized_pnl REAL NOT NULL,
            return_pct REAL NOT NULL,
            status TEXT NOT NULL,
            UNIQUE(run_date, ticker)
        )
        """
    )
    conn.commit()


def held_tickers(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT ticker FROM positions").fetchall()
    return {str(row["ticker"]).upper() for row in rows}


def portfolio_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS open_positions,
            COALESCE(SUM(notional), 0) AS total_notional
        FROM positions
        WHERE status = 'open'
        """
    ).fetchone()
    return {
        "open_positions": int(row["open_positions"] or 0),
        "total_notional": round(float(row["total_notional"] or 0), 2),
    }


def _parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return dt.datetime.now().date()


def _candidate_rank_key(candidate: Candidate) -> tuple[int, float, float, float]:
    if candidate.agent_review.decision == "Focus":
        tier = 0
    elif candidate.deep_dive_decision == "Focus":
        tier = 1
    elif candidate.agent_review.decision == "Watch":
        tier = 2
    else:
        tier = 3
    return (
        tier,
        -float(candidate.agent_review.review_score or 0),
        -float(candidate.deep_dive_score or 0),
        -float(candidate.score or 0),
    )


def select_paper_buy_candidate(candidates: list[Candidate], held: set[str]) -> tuple[Candidate | None, list[str]]:
    skipped_duplicates = []
    for candidate in sorted(candidates, key=_candidate_rank_key):
        ticker = candidate.ticker.upper()
        if ticker in held:
            skipped_duplicates.append(ticker)
            continue
        if candidate.price.last_close <= 0:
            continue
        return candidate, skipped_duplicates
    return None, skipped_duplicates


def build_position(candidate: Candidate, buy_amount: float, run_date: str) -> dict[str, Any]:
    price = float(candidate.price.last_close)
    shares = buy_amount / price
    if candidate.agent_review.main_bull_case:
        thesis = candidate.agent_review.main_bull_case
    elif candidate.deep_dive_reasons:
        thesis = candidate.deep_dive_reasons[0]
    else:
        thesis = candidate.thesis

    if candidate.agent_review.main_bear_case:
        main_risk = candidate.agent_review.main_bear_case
    elif candidate.deep_dive_risks:
        main_risk = candidate.deep_dive_risks[0]
    elif candidate.risks:
        main_risk = candidate.risks[0]
    else:
        main_risk = ""

    return {
        "ticker": candidate.ticker.upper(),
        "buy_date": run_date,
        "created_at": _now_utc(),
        "notional": round(buy_amount, 2),
        "price": round(price, 4),
        "shares": round(shares, 8),
        "agent_decision": candidate.agent_review.decision,
        "agent_review_score": round(float(candidate.agent_review.review_score or 0), 2),
        "agent_risk": candidate.agent_review.risk_rating,
        "deep_dive_decision": candidate.deep_dive_decision,
        "deep_dive_score": round(float(candidate.deep_dive_score or 0), 2),
        "original_score": round(float(candidate.score or 0), 2),
        "evidence_quality": round(float(candidate.agent_review.evidence_quality or 0), 3),
        "thesis": thesis,
        "main_risk": main_risk,
        "status": "open",
    }


def insert_position(conn: sqlite3.Connection, position: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO positions (
            ticker, buy_date, created_at, notional, price, shares,
            agent_decision, agent_review_score, agent_risk,
            deep_dive_decision, deep_dive_score, original_score,
            evidence_quality, thesis, main_risk, status
        )
        VALUES (
            :ticker, :buy_date, :created_at, :notional, :price, :shares,
            :agent_decision, :agent_review_score, :agent_risk,
            :deep_dive_decision, :deep_dive_score, :original_score,
            :evidence_quality, :thesis, :main_risk, :status
        )
        """,
        position,
    )
    conn.commit()


def paper_buy_already_recorded(conn: sqlite3.Connection, run_date: str) -> bool:
    position_row = conn.execute(
        "SELECT 1 FROM positions WHERE buy_date = ? LIMIT 1",
        (run_date,),
    ).fetchone()
    if position_row:
        return True
    skipped_row = conn.execute(
        "SELECT 1 FROM skipped_runs WHERE run_date = ? LIMIT 1",
        (run_date,),
    ).fetchone()
    return bool(skipped_row)


def apply_paper_buy(candidates: list[Candidate], db_path: str, buy_amount: float = DEFAULT_BUY_AMOUNT, run_date: str | None = None) -> dict[str, Any]:
    run_date = run_date or dt.datetime.now().strftime("%Y-%m-%d")
    with connect(db_path) as conn:
        skipped_duplicates: list[str] = []
        result: dict[str, Any] = {
            "run_date": run_date,
            "db_path": db_path,
            "buy_amount": buy_amount,
            "status": "no_candidate",
            "skipped_duplicates": skipped_duplicates,
            "position": None,
            **portfolio_summary(conn),
        }
        if paper_buy_already_recorded(conn, run_date):
            result["status"] = "already_recorded"
            return result

        candidate, skipped_duplicates = select_paper_buy_candidate(candidates, held_tickers(conn))
        result["skipped_duplicates"] = skipped_duplicates
        if candidate is None:
            conn.execute(
                """
                INSERT INTO skipped_runs (run_date, created_at, reason, skipped_duplicates)
                VALUES (?, ?, ?, ?)
                """,
                (
                    run_date,
                    _now_utc(),
                    "No new eligible ticker found in today's report.",
                    json.dumps(skipped_duplicates),
                ),
            )
            conn.commit()
            return result

        position = build_position(candidate, buy_amount, run_date)
        insert_position(conn, position)
        result["status"] = "bought"
        result["position"] = position
        result.update(portfolio_summary(conn))
        return result


def _candidate_price_map(candidates: list[Candidate]) -> dict[str, float]:
    return {
        candidate.ticker.upper(): float(candidate.price.last_close)
        for candidate in candidates
        if candidate.price.last_close > 0
    }


def update_portfolio_performance(db_path: str, candidates: list[Candidate], run_date: str | None = None) -> dict[str, Any]:
    run_date = run_date or dt.datetime.now().strftime("%Y-%m-%d")
    run_day = _parse_date(run_date)
    latest_prices = _candidate_price_map(candidates)
    snapshots = []
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT ticker, buy_date, notional, price, shares, status
            FROM positions
            WHERE status = 'open'
            ORDER BY buy_date, ticker
            """
        ).fetchall()
        for row in rows:
            ticker = str(row["ticker"]).upper()
            current_price = latest_prices.get(ticker)
            source = "report"
            if current_price is None:
                try:
                    stats = fetch_price_stats(ticker)
                    current_price = float(stats.last_close) if stats else None
                    source = "live_price"
                except Exception:  # noqa: BLE001 - price refresh should not break the report.
                    current_price = None
                    source = "missing"
            if current_price is None or current_price <= 0:
                continue

            notional = float(row["notional"])
            entry_price = float(row["price"])
            shares = float(row["shares"])
            market_value = shares * current_price
            unrealized_pnl = market_value - notional
            return_pct = (current_price / entry_price - 1) * 100 if entry_price else 0
            holding_days = max(0, (run_day - _parse_date(str(row["buy_date"]))).days)
            snapshot = {
                "run_date": run_date,
                "created_at": _now_utc(),
                "ticker": ticker,
                "buy_date": str(row["buy_date"]),
                "holding_days": holding_days,
                "notional": round(notional, 2),
                "entry_price": round(entry_price, 4),
                "current_price": round(current_price, 4),
                "shares": round(shares, 8),
                "market_value": round(market_value, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "return_pct": round(return_pct, 2),
                "status": str(row["status"]),
                "price_source": source,
            }
            snapshots.append(snapshot)
            conn.execute(
                """
                INSERT INTO position_snapshots (
                    run_date, created_at, ticker, buy_date, holding_days, notional,
                    entry_price, current_price, shares, market_value,
                    unrealized_pnl, return_pct, status
                )
                VALUES (
                    :run_date, :created_at, :ticker, :buy_date, :holding_days, :notional,
                    :entry_price, :current_price, :shares, :market_value,
                    :unrealized_pnl, :return_pct, :status
                )
                ON CONFLICT(run_date, ticker) DO UPDATE SET
                    created_at = excluded.created_at,
                    holding_days = excluded.holding_days,
                    current_price = excluded.current_price,
                    market_value = excluded.market_value,
                    unrealized_pnl = excluded.unrealized_pnl,
                    return_pct = excluded.return_pct,
                    status = excluded.status
                """,
                snapshot,
            )
        conn.commit()

    total_cost = sum(float(item["notional"]) for item in snapshots)
    total_value = sum(float(item["market_value"]) for item in snapshots)
    total_pnl = total_value - total_cost
    total_return_pct = (total_value / total_cost - 1) * 100 if total_cost else 0
    return {
        "run_date": run_date,
        "db_path": db_path,
        "open_positions": len(snapshots),
        "total_cost": round(total_cost, 2),
        "total_value": round(total_value, 2),
        "total_unrealized_pnl": round(total_pnl, 2),
        "total_return_pct": round(total_return_pct, 2),
        "positions": snapshots,
        "winners": sum(1 for item in snapshots if float(item["unrealized_pnl"]) > 0),
        "losers": sum(1 for item in snapshots if float(item["unrealized_pnl"]) < 0),
    }


def append_paper_buy_to_outputs(markdown_path: str, json_path: str, result: dict[str, Any]) -> None:
    position = result.get("position")
    with open(markdown_path, "a", encoding="utf-8") as handle:
        handle.write("\n## Paper Portfolio Buy\n\n")
        handle.write("This is a simulated validation portfolio, not a real trade or investment advice.\n\n")
        if result.get("status") == "bought" and isinstance(position, dict):
            handle.write(
                f"- Bought: {position['ticker']} paper ${position['notional']:.2f} "
                f"at ${position['price']:.2f}, {position['shares']:.6f} shares\n"
            )
            handle.write(f"- Thesis: {position.get('thesis', '')}\n")
            handle.write(f"- Main risk: {position.get('main_risk', '')}\n")
        elif result.get("status") == "already_recorded":
            handle.write("- No new paper buy today; this run date already has a recorded paper decision.\n")
        else:
            handle.write("- No new paper buy today; every eligible ticker was already held or no valid candidate was available.\n")
        if result.get("skipped_duplicates"):
            handle.write("- Skipped existing holdings: " + ", ".join(result["skipped_duplicates"]) + "\n")
        handle.write(f"- Open positions: {result.get('open_positions', 0)}\n")
        handle.write(f"- Total paper notional: ${float(result.get('total_notional', 0)):.2f}\n")
        handle.write(f"- Local DB: `{result.get('db_path')}`\n")

    try:
        payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(payload, dict):
        payload["paper_portfolio_buy"] = result
        Path(json_path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def append_performance_to_outputs(markdown_path: str, json_path: str, result: dict[str, Any]) -> None:
    positions = result.get("positions", [])
    with open(markdown_path, "a", encoding="utf-8") as handle:
        handle.write("\n## Paper Portfolio Performance\n\n")
        handle.write("Daily mark-to-market for the simulated validation portfolio.\n\n")
        handle.write(f"- Open positions: {result.get('open_positions', 0)}\n")
        handle.write(f"- Total cost: ${float(result.get('total_cost', 0)):.2f}\n")
        handle.write(f"- Current value: ${float(result.get('total_value', 0)):.2f}\n")
        handle.write(
            f"- Unrealized P/L: ${float(result.get('total_unrealized_pnl', 0)):.2f} "
            f"({float(result.get('total_return_pct', 0)):.2f}%)\n"
        )
        handle.write(f"- Winners / losers: {result.get('winners', 0)} / {result.get('losers', 0)}\n\n")
        if positions:
            handle.write("| Ticker | Buy Date | Days Held | Cost | Entry | Current | Value | P/L | Return |\n")
            handle.write("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |\n")
            for item in sorted(positions, key=lambda row: float(row.get("unrealized_pnl", 0)), reverse=True):
                handle.write(
                    f"| {item['ticker']} | {item['buy_date']} | {item['holding_days']} | "
                    f"${float(item['notional']):.2f} | ${float(item['entry_price']):.2f} | "
                    f"${float(item['current_price']):.2f} | ${float(item['market_value']):.2f} | "
                    f"${float(item['unrealized_pnl']):.2f} | {float(item['return_pct']):.2f}% |\n"
                )
        else:
            handle.write("No open paper positions yet.\n")

    try:
        payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(payload, dict):
        payload["paper_portfolio_performance"] = result
        Path(json_path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def archive_report(
    db_path: str,
    run_date: str,
    markdown_path: str,
    json_path: str,
    candidates: list[Candidate],
    paper_buy_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    paper_buy_result = paper_buy_result or {"status": "skipped", "position": None}
    position = paper_buy_result.get("position")
    paper_buy_ticker = position.get("ticker", "") if isinstance(position, dict) else ""
    markdown_body = Path(markdown_path).read_text(encoding="utf-8")
    json_body = Path(json_path).read_text(encoding="utf-8")
    top_tickers = [candidate.ticker.upper() for candidate in candidates]
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO reports (
                run_date, created_at, markdown_path, json_path, candidate_count,
                top_tickers, paper_buy_status, paper_buy_ticker, paper_buy_result,
                markdown_body, json_body
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_date) DO UPDATE SET
                created_at = excluded.created_at,
                markdown_path = excluded.markdown_path,
                json_path = excluded.json_path,
                candidate_count = excluded.candidate_count,
                top_tickers = excluded.top_tickers,
                paper_buy_status = excluded.paper_buy_status,
                paper_buy_ticker = excluded.paper_buy_ticker,
                paper_buy_result = excluded.paper_buy_result,
                markdown_body = excluded.markdown_body,
                json_body = excluded.json_body
            """,
            (
                run_date,
                _now_utc(),
                markdown_path,
                json_path,
                len(candidates),
                json.dumps(top_tickers),
                str(paper_buy_result.get("status", "")),
                paper_buy_ticker,
                json.dumps(paper_buy_result, ensure_ascii=False),
                markdown_body,
                json_body,
            ),
        )
        conn.commit()
    return {
        "run_date": run_date,
        "db_path": db_path,
        "candidate_count": len(candidates),
        "top_tickers": top_tickers,
        "paper_buy_status": paper_buy_result.get("status"),
        "paper_buy_ticker": paper_buy_ticker,
    }
