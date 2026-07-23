"""CLI entry point: fetches the same real, audited XAUUSD history
`backtester/run_backtest.py` uses and runs Phase 2's anchored walk-forward
validation over it, printing a per-fold table plus the final aggregate
report and `docs/RESEARCH.md` §5 promotion-gate verdicts, and writing full
results to a JSON file.

Read-only against the live account (`get_bars_range()` only — no orders are
ever placed); safe to run alongside `main.py`'s own live connection. With
the default reduced 35-combo grid over ~16 folds this takes roughly
1.5-2 hours — intended to be run as a background task.

Usage: `python -m backtester.run_walk_forward`
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from backtester.historical_data import fetch_audited_history
from backtester.run_backtest import HISTORY_START_UTC, TIMEFRAME_MINUTES
from backtester.simulator import DEFAULT_STARTING_EQUITY
from backtester.walk_forward import WalkForwardResult, run_walk_forward_validation
from broker.mt5_gateway import TIMEFRAME_D1, TIMEFRAME_H1, TIMEFRAME_H4, MT5Gateway
from config.config_manager import ConfigManager

logger = logging.getLogger(__name__)

RESULTS_PATH = Path(__file__).resolve().parent / "walk_forward_results.json"


def _print_report(result: WalkForwardResult) -> None:
    print("=" * 88)
    print("Anchored walk-forward validation — WAIT_FOR_CONDITIONS strategy, XAUUSD H1")
    print("=" * 88)
    print(
        f"{'Fold test window':<45} {'Winner (ADX,Trail)':<20} {'OOS Sharpe':>10} {'OOS trades':>10}"
    )
    for fold_result in result.folds:
        f = fold_result.fold
        s = fold_result.selection
        window = f"{f.test_start.date()} -> {f.test_end.date()}"
        winner = f"({s.winning_adx_trend_threshold:.2f}, {s.winning_trailing_atr_multiplier:.2f})"
        oos_trade_count = len(fold_result.oos_trades)
        print(f"{window:<45} {winner:<20} {fold_result.oos_sharpe:>10.3f} {oos_trade_count:>10}")
    print("=" * 88)

    report = result.performance_report
    gates = result.promotion_gates
    print(f"Concatenated OOS period: {report.period_start_utc} -> {report.period_end_utc}")
    print(f"Total OOS trades:        {report.total_trades}")
    print(f"Win rate:                {report.win_rate_pct:.1%}")
    print(f"Profit factor:           {report.profit_factor:.2f}")
    print(f"Sharpe ratio:            {report.sharpe_ratio:.3f}")
    print(f"Sortino ratio:           {report.sortino_ratio:.3f}")
    print(f"MAR ratio:               {report.mar_ratio:.3f}")
    print(f"Max drawdown:            {report.max_drawdown_pct:.1%}")
    print(f"Max drawdown length:     {report.max_drawdown_duration}")
    print(f"Deflated Sharpe Ratio:   {report.deflated_sharpe_ratio:.6f}")
    print("=" * 88)
    print("Promotion gates (docs/RESEARCH.md §5):")
    print(f"  1. DSR >= 0.95:                       {gates.dsr_gate_passed}", end="  ")
    print(f"(value={gates.dsr_value:.6f})")
    print(
        f"  2. IS/OOS efficiency ratio >= 0.5:    {gates.is_oos_efficiency_gate_passed}  "
        f"(value={gates.is_oos_efficiency_ratio:.3f})"
    )
    print(f"  3. MAR >= 0.5:                        {gates.mar_gate_passed}", end="  ")
    print(f"(value={gates.mar_value:.3f})")
    print(
        f"  4. Max OOS drawdown <= 40%:           {gates.max_drawdown_gate_passed}  "
        f"(value={gates.max_drawdown_value:.1%})"
    )
    print(
        f"  5. Trade count (>=30/fold, >=360 total): {gates.trade_count_gate_passed}  "
        f"(total={gates.total_oos_trades})"
    )
    print("=" * 88)
    verdict = (
        "PASSED — all 5 gates cleared"
        if gates.all_gates_passed
        else "NOT PROMOTED — see gates above"
    )
    print(f"VERDICT: {verdict}")
    print(
        "This is this codebase's best-effort, documented interpretation of "
        "docs/RESEARCH.md's formulas — see backtester/walk_forward.py's module "
        "docstring for every interpretive choice made where the spec didn't "
        "fully pin down fold-vs-engine mechanics."
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
        "Fetched %d D1, %d H4, %d H1 bars. Running anchored walk-forward validation "
        "(this takes a while — reduced grid ~1.5-2h)...",
        len(d1_bars.close),
        len(h4_bars.close),
        len(h1_bars.close),
    )
    result = run_walk_forward_validation(
        d1_bars, h4_bars, h1_bars, starting_equity=DEFAULT_STARTING_EQUITY
    )
    _print_report(result)

    serializable = {
        "folds": [
            {
                "test_start": fr.fold.test_start.isoformat(),
                "test_end": fr.fold.test_end.isoformat(),
                "winning_adx_trend_threshold": fr.selection.winning_adx_trend_threshold,
                "winning_trailing_atr_multiplier": fr.selection.winning_trailing_atr_multiplier,
                "winning_is_sharpe": fr.selection.winning_is_sharpe,
                "winning_is_dsr": fr.selection.winning_is_dsr,
                "oos_sharpe": fr.oos_sharpe,
                "oos_trade_count": len(fr.oos_trades),
                "oos_trades": [
                    {
                        **asdict(t),
                        "entry_time": t.entry_time.isoformat(),
                        "exit_time": t.exit_time.isoformat(),
                    }
                    for t in fr.oos_trades
                ],
            }
            for fr in result.folds
        ],
        "performance_report": {
            **asdict(result.performance_report),
            "period_start_utc": result.performance_report.period_start_utc.isoformat(),
            "period_end_utc": result.performance_report.period_end_utc.isoformat(),
            "max_drawdown_duration": str(result.performance_report.max_drawdown_duration),
        },
        "promotion_gates": asdict(result.promotion_gates),
    }
    RESULTS_PATH.write_text(json.dumps(serializable, indent=2))
    print(f"\nFull results written to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
