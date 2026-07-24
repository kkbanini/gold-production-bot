"""CLI entry point: fetches the same real, audited XAUUSD H1 history
`backtester/run_backtest.py` uses and trains/validates the ML candidate
signal models (`backtester/ml_signal_model.py`) against it, printing a
comparison table (naive heuristic vs. majority-class baseline vs. logistic
regression vs. gradient boosting) plus the promotion-bar verdict — and, if
logistic regression clears it, the exact raw-space coefficients to
hardcode into `monitoring/telegram_bot.py`.

Read-only against the live account (`get_bars_range()` only — no orders
are ever placed); safe to run alongside `main.py`'s own live connection.

Usage: `python -m backtester.run_ml_signal_research`
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from backtester.historical_data import fetch_audited_history
from backtester.ml_signal_model import (
    MIN_ROC_AUC_FOR_PROMOTION,
    MLValidationReport,
    binomial_ci_lower_bound,
    evaluate_promotion_bar,
    train_and_validate_models,
)
from backtester.run_backtest import HISTORY_START_UTC, TIMEFRAME_MINUTES
from broker.mt5_gateway import TIMEFRAME_H1, MT5Gateway
from config.config_manager import ConfigManager

logger = logging.getLogger(__name__)

RESULTS_PATH = Path(__file__).resolve().parent / "ml_signal_research_results.json"


def _print_report(report: MLValidationReport, *, promoted: bool) -> None:
    lr = report.logistic_regression
    gb = report.gradient_boosting

    print("=" * 88)
    print("ML signal research — /condition BUY/SELL candidate models, XAUUSD H1")
    print("=" * 88)
    print(f"Horizon:                 {report.horizon_bars} bars")
    print(f"Train / OOS sample size: {report.n_train} / {report.n_oos}")
    print(f"Majority-class baseline: {report.majority_class_baseline:.4f}")
    print(f"Naive heuristic (OOS):   {report.naive_heuristic_oos_accuracy:.4f}")
    print("-" * 88)
    print(f"{'Model':<24} {'Accuracy':>10} {'Precision':>10} {'Recall':>10} {'ROC-AUC':>10}")
    print(
        f"{'Logistic regression':<24} {lr.evaluation.oos_accuracy:>10.4f} "
        f"{lr.evaluation.oos_precision:>10.4f} {lr.evaluation.oos_recall:>10.4f} "
        f"{lr.evaluation.oos_roc_auc:>10.4f}"
    )
    print(
        f"{'Gradient boosting':<24} {gb.oos_accuracy:>10.4f} {gb.oos_precision:>10.4f} "
        f"{gb.oos_recall:>10.4f} {gb.oos_roc_auc:>10.4f}"
    )
    print("=" * 88)
    ci_lower_bound = binomial_ci_lower_bound(lr.evaluation.oos_accuracy, report.n_oos)
    print(f"Logistic regression selected C: {lr.selected_c}")
    print("Promotion bar (all 4 required for live deployment):")
    print(
        f"  1. OOS accuracy's 95% CI lower bound > 50%: {ci_lower_bound:.4f} -> passes: "
        f"{ci_lower_bound > 0.5}"
    )
    print(
        f"  2. Beats majority-class baseline ({report.majority_class_baseline:.4f}): "
        f"{lr.evaluation.oos_accuracy > report.majority_class_baseline}"
    )
    print(
        f"  3. Beats naive heuristic ({report.naive_heuristic_oos_accuracy:.4f}): "
        f"{lr.evaluation.oos_accuracy > report.naive_heuristic_oos_accuracy}"
    )
    print(
        f"  4. OOS ROC-AUC > {MIN_ROC_AUC_FOR_PROMOTION} (real discrimination, not just "
        f"majority-class guessing): {lr.evaluation.oos_roc_auc:.4f} -> passes: "
        f"{lr.evaluation.oos_roc_auc > MIN_ROC_AUC_FOR_PROMOTION}"
    )
    print("=" * 88)
    if promoted:
        print("VERDICT: PROMOTED — logistic regression cleared the bar.")
        print("Raw-space coefficients to hardcode into monitoring/telegram_bot.py:")
        for name, coefficient in zip(report.feature_names, lr.raw_space_coefficients, strict=True):
            print(f"  {name:<24} = {coefficient!r}")
        print(f"  {'intercept':<24} = {lr.raw_space_intercept!r}")
    else:
        print("VERDICT: NOT PROMOTED — see gates above. /condition's live signal stays unchanged.")


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
        logger.info("Fetching audited H1 history: %s -> %s", HISTORY_START_UTC, now)
        h1_bars = fetch_audited_history(
            gateway, TIMEFRAME_H1, TIMEFRAME_MINUTES[TIMEFRAME_H1], HISTORY_START_UTC, now
        )
    finally:
        gateway.disconnect()

    logger.info("Fetched %d H1 bars. Training/validating ML signal models...", len(h1_bars.close))
    report = train_and_validate_models(h1_bars)
    promoted = evaluate_promotion_bar(report)
    _print_report(report, promoted=promoted)

    serializable = {
        "feature_names": report.feature_names,
        "horizon_bars": report.horizon_bars,
        "n_train": report.n_train,
        "n_oos": report.n_oos,
        "majority_class_baseline": report.majority_class_baseline,
        "naive_heuristic_oos_accuracy": report.naive_heuristic_oos_accuracy,
        "logistic_regression": {
            **asdict(report.logistic_regression.evaluation),
            "raw_space_coefficients": report.logistic_regression.raw_space_coefficients,
            "raw_space_intercept": report.logistic_regression.raw_space_intercept,
            "selected_c": report.logistic_regression.selected_c,
        },
        "gradient_boosting": asdict(report.gradient_boosting),
        "promoted": promoted,
    }
    RESULTS_PATH.write_text(json.dumps(serializable, indent=2))
    print(f"\nFull results written to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
