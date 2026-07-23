"""CLI entry point: fetches real, audited XAUUSD history from the
connected MT5 account and runs Phase 1's backtest over it, printing a
performance summary and writing the full trade log to a JSON file.

Read-only against the live account (`get_bars_range()` only — no orders
are ever placed); safe to run alongside `main.py`'s own live connection.

Usage: `python -m backtester.run_backtest`
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from analytics.performance import PerformanceReport, build_performance_report
from backtester.historical_data import fetch_audited_history
from backtester.simulator import DEFAULT_STARTING_EQUITY, run_backtest
from broker.mt5_gateway import TIMEFRAME_D1, TIMEFRAME_H1, TIMEFRAME_H4, MT5Gateway
from config.config_manager import ConfigManager

logger = logging.getLogger(__name__)

# The real broker history retention floor for this account, confirmed
# live via `mt5.copy_rates_range()` (D1/H4/H1 all share this same
# earliest bar) — not the docs/RESEARCH.md-targeted 5 years; see
# `docs/adr/ADR-0004-anchored-walk-forward-validation.md`'s Phase 1 scope
# note for why this phase proceeds with what's actually available instead.
HISTORY_START_UTC = datetime(2023, 3, 20, tzinfo=timezone.utc)

TIMEFRAME_MINUTES = {TIMEFRAME_D1: 24 * 60, TIMEFRAME_H4: 4 * 60, TIMEFRAME_H1: 60}

TRADE_LOG_PATH = Path(__file__).resolve().parent / "backtest_trade_log.json"


def _print_report(report: PerformanceReport) -> None:
    print("=" * 60)
    print("Backtest report — WAIT_FOR_CONDITIONS strategy, XAUUSD H1")
    print("=" * 60)
    print(f"Period:              {report.period_start_utc} -> {report.period_end_utc}")
    print(f"Total trades:        {report.total_trades}")
    print(f"Win rate:            {report.win_rate_pct:.1%}")
    print(f"Profit factor:       {report.profit_factor:.2f}")
    print(f"Sharpe ratio:        {report.sharpe_ratio:.3f}")
    print(f"Sortino ratio:       {report.sortino_ratio:.3f}")
    print(f"MAR ratio:           {report.mar_ratio:.3f}")
    print(f"Max drawdown:        {report.max_drawdown_pct:.1%}")
    print(f"Max drawdown length: {report.max_drawdown_duration}")
    print("=" * 60)
    print(
        "NOTE: single historical run, in-sample only (no train/test split). "
        "A good result here is necessary but NOT sufficient — real "
        "walk-forward out-of-sample validation (ADR-0004) is still required "
        "before this changes what capital is risked live."
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = ConfigManager.load()
    gateway = MT5Gateway(
        login=config.mt5_login,
        password=config.mt5_password,
        server=config.mt5_server,
        magic_number=config.strategy_magic_number,
    )
    gateway.connect()
    try:
        now = datetime.now(timezone.utc)
        logger.info("Fetching audited D1/H4/H1 history: %s -> %s", HISTORY_START_UTC, now)
        d1_bars = fetch_audited_history(
            gateway, TIMEFRAME_D1, TIMEFRAME_MINUTES[TIMEFRAME_D1], HISTORY_START_UTC, now
        )
        h4_bars = fetch_audited_history(
            gateway, TIMEFRAME_H4, TIMEFRAME_MINUTES[TIMEFRAME_H4], HISTORY_START_UTC, now
        )
        h1_bars = fetch_audited_history(
            gateway, TIMEFRAME_H1, TIMEFRAME_MINUTES[TIMEFRAME_H1], HISTORY_START_UTC, now
        )
    finally:
        gateway.disconnect()

    logger.info(
        "Fetched %d D1, %d H4, %d H1 bars. Running backtest...",
        len(d1_bars.close),
        len(h4_bars.close),
        len(h1_bars.close),
    )
    result = run_backtest(d1_bars, h4_bars, h1_bars, starting_equity=DEFAULT_STARTING_EQUITY)

    if not result.equity_curve:
        print("No equity curve produced (insufficient history for warm-up) — nothing to report.")
        return

    report = build_performance_report(result.equity_curve, [t.profit for t in result.trades])
    _print_report(report)

    trade_log = [
        {
            **asdict(trade),
            "entry_time": trade.entry_time.isoformat(),
            "exit_time": trade.exit_time.isoformat(),
        }
        for trade in result.trades
    ]
    TRADE_LOG_PATH.write_text(json.dumps(trade_log, indent=2))
    print(f"\nFull trade log ({len(trade_log)} rows) written to {TRADE_LOG_PATH}")


if __name__ == "__main__":
    main()
