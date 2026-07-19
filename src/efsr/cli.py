#!/usr/bin/env python3
"""Event-only daily US stock bottom-fishing screener.

This script intentionally avoids valuation and broad multi-factor models.
It uses public, no-key data sources and produces a research watchlist.

This module is the pipeline/CLI orchestration layer only. Domain logic lives in:
- models.py          shared dataclasses
- data_sources.py    universe, aliases, news, price, and SEC retrieval
- scoring.py         deterministic scoring (event screen, deep dive, fundamentals)
- agent_runtime.py   the live multi-agent research layer
- reporting.py       Markdown/JSON rendering
- paper_portfolio.py validation ledger
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import logging
import os
from typing import Sequence

from efsr.agents.legacy import apply_agent_reviews_legacy
from efsr.agents.runtime import apply_agent_reviews as run_agent_reviews
from efsr.sources import (
    fetch_news,
    fetch_price_stats,
    load_aliases as load_aliases_source,
    load_universe as load_universe_source,
)
from efsr.models import Candidate
from efsr.portfolio import (
    apply_paper_buy,
    append_paper_buy_to_outputs,
    append_performance_to_outputs,
    archive_report,
    update_portfolio_performance,
)
from efsr.reporting import write_outputs as render_outputs
from efsr.scoring import (
    apply_data_confidence,
    apply_deep_dive,
    apply_fundamental_scores,
    load_sec_ticker_map_safely,
    score_candidate,
)

logger = logging.getLogger(__name__)


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_UNIVERSE = "sp500-live"
DEFAULT_ALIASES = "auto"
DEFAULT_UNIVERSE_FALLBACK = os.path.join(ROOT, "config", "universe_sp100.txt")
DEFAULT_ALIASES_OVERRIDE = os.path.join(ROOT, "config", "company_aliases.json")
OUTPUT_DIR = os.path.join(ROOT, "outputs")
DEFAULT_PAPER_PORTFOLIO_DB = os.path.join(ROOT, "state", "paper_portfolio.sqlite")


def load_universe(path: str) -> list[str]:
    return load_universe_source(path, fallback_path=DEFAULT_UNIVERSE_FALLBACK)


def load_aliases(path: str, universe: list[str] | None = None) -> dict[str, list[str]]:
    return load_aliases_source(path, universe=universe, manual_override_path=DEFAULT_ALIASES_OVERRIDE)


def apply_agent_reviews(candidates: list[Candidate], args: argparse.Namespace) -> list[Candidate]:
    impl = getattr(args, "agent_impl", "runtime")
    review = apply_agent_reviews_legacy if impl == "legacy" else run_agent_reviews
    return review(
        candidates,
        token_budget=args.agent_token_budget,
        mode=args.agent_mode,
        model=args.agent_model,
        max_output_tokens=args.agent_max_output_tokens,
        review_count=args.agent_review_count,
        verbose=True,
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


def build_candidate(
    index: int,
    ticker: str,
    aliases_by_ticker: dict[str, list[str]],
    args: argparse.Namespace,
) -> tuple[int, Candidate | None, str | None]:
    try:
        news = fetch_news(
            ticker,
            aliases_by_ticker.get(ticker, []),
            args.max_news,
            args.lookback_days,
            args.allow_broad_news,
        )
        if not news:
            return index, None, None
        price = fetch_price_stats(ticker)
        if not price:
            return index, None, None
        return index, score_candidate(ticker, news, price), None
    except Exception as exc:  # noqa: BLE001 - scanner should continue per ticker.
        return index, None, str(exc)


def scan(args: argparse.Namespace) -> list[Candidate]:
    tickers = load_universe(args.universe)
    aliases_by_ticker = load_aliases(args.aliases, universe=tickers)
    candidates = []
    errors = 0
    max_workers = max(1, args.scan_workers)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(build_candidate, index, ticker, aliases_by_ticker, args)
            for index, ticker in enumerate(tickers, start=1)
        ]
        for future in concurrent.futures.as_completed(futures):
            index, candidate, error = future.result()
            ticker = tickers[index - 1]
            if error:
                errors += 1
                logger.debug("[%d/%d] %s: skipped (%s)", index, len(tickers), ticker, error)
            if candidate:
                candidates.append(candidate)
                logger.debug("[%d/%d] %s: %.2f", index, len(tickers), ticker, candidate.score)
    # Distinguish "quiet market" from "upstream broken": a high error rate means the
    # data sources likely failed rather than there being no event-driven names today.
    log = logger.warning if errors > len(tickers) // 2 else logger.info
    log("scan complete: %d/%d tickers yielded candidates, %d fetch errors",
        len(candidates), len(tickers), errors)
    candidates.sort(key=lambda item: item.score, reverse=True)
    return prepare_selected_candidates(candidates, args)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", default=DEFAULT_UNIVERSE)
    parser.add_argument("--aliases", default=DEFAULT_ALIASES)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--lookback-days", type=int, default=14)
    parser.add_argument("--max-news", type=int, default=8)
    parser.add_argument("--deep-dive-focus", type=int, default=3)
    parser.add_argument("--sleep", type=float, default=0.15)
    parser.add_argument(
        "--scan-workers",
        type=int,
        default=int(os.environ.get("SCAN_WORKERS", "12")),
        help="Number of parallel ticker fetch workers used during the initial scan.",
    )
    parser.add_argument("--allow-broad-news", action="store_true")
    parser.add_argument("--include-avoid", action="store_true")
    parser.add_argument("--skip-data-confidence", action="store_true")
    parser.add_argument("--skip-agent-review", action="store_true")
    parser.add_argument(
        "--agent-impl",
        choices=("runtime", "legacy"),
        default=os.environ.get("AGENT_IMPL", "runtime"),
        help="Agent-review implementation: 'runtime' (default; agent_runtime.py with "
        "tool/plan trace) or 'legacy' (the reserved committee with the 6-factor "
        "evidence model and optional final LLM overlay).",
    )
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
    parser.add_argument(
        "--skip-paper-portfolio",
        action="store_true",
        help="Skip the paper portfolio validation buy after report generation.",
    )
    parser.add_argument(
        "--paper-buy-amount",
        type=float,
        default=float(os.environ.get("PAPER_BUY_AMOUNT", "100")),
        help="Paper notional to buy after each report run.",
    )
    parser.add_argument(
        "--paper-portfolio-db",
        default=os.environ.get("PAPER_PORTFOLIO_DB", DEFAULT_PAPER_PORTFOLIO_DB),
        help="SQLite DB path for the paper validation portfolio.",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    args = build_arg_parser().parse_args(argv)
    if args.agent_mode is None:
        legacy_provider = os.environ.get("AGENT_PROVIDER", "deterministic")
        if legacy_provider == "openai":
            args.agent_mode = "full"
        elif os.environ.get("OPENAI_API_KEY"):
            args.agent_mode = "lean"
        else:
            args.agent_mode = "deterministic"
    return args


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    _configure_logging(args.verbose)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    candidates = scan(args)
    if not candidates:
        logger.error("No candidates found. Check network access or widen the universe/lookback window.")
        return 1

    today = dt.datetime.now().strftime("%Y-%m-%d")
    path_prefix = os.path.join(OUTPUT_DIR, f"daily_event_bottom_fishing_{today}")
    json_path, md_path = render_outputs(candidates, path_prefix)
    paper_result = {"status": "skipped", "position": None, "db_path": args.paper_portfolio_db}
    if not args.skip_paper_portfolio:
        paper_result = apply_paper_buy(
            candidates,
            db_path=args.paper_portfolio_db,
            buy_amount=args.paper_buy_amount,
            run_date=today,
        )
        append_paper_buy_to_outputs(md_path, json_path, paper_result)
        if paper_result["status"] == "bought" and paper_result["position"]:
            position = paper_result["position"]
            logger.info(
                "[paper] bought %s $%.2f at $%.2f; shares=%.6f; db=%s",
                position["ticker"], position["notional"], position["price"],
                position["shares"], args.paper_portfolio_db,
            )
        else:
            logger.info("[paper] no new buy; db=%s", args.paper_portfolio_db)
    performance_result = update_portfolio_performance(
        args.paper_portfolio_db,
        candidates,
        run_date=today,
    )
    append_performance_to_outputs(md_path, json_path, performance_result)
    logger.info(
        "[paper] performance positions=%s value=$%.2f pnl=$%.2f return=%.2f%%",
        performance_result["open_positions"], performance_result["total_value"],
        performance_result["total_unrealized_pnl"], performance_result["total_return_pct"],
    )
    for stop in performance_result.get("stopped_this_run", []):
        logger.info(
            "[paper] stopped out %s via %s at $%.2f (%.2f%%, realized $%.2f); it will not be re-bought",
            stop["ticker"], stop["exit_reason"], stop["exit_price"],
            stop["return_pct"], stop["realized_pnl"],
        )
    archive_result = archive_report(
        args.paper_portfolio_db,
        run_date=today,
        markdown_path=md_path,
        json_path=json_path,
        candidates=candidates,
        paper_buy_result=paper_result,
    )
    logger.info(
        "[paper] archived report date=%s candidates=%s db=%s",
        archive_result["run_date"], archive_result["candidate_count"], archive_result["db_path"],
    )
    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    print()
    for index, candidate in enumerate(candidates, start=1):
        print(f"{index:>2}. {candidate.ticker:<6} {candidate.bucket} {candidate.score:>6.2f}  {candidate.thesis}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
