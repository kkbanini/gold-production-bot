"""Anchored walk-forward optimization (WFO) — `docs/adr/ADR-0004-anchored-
walk-forward-validation.md`'s sole sanctioned validation methodology,
parameterized exactly per `docs/RESEARCH.md` §4/§5, built on top of Phase
1's `backtester/simulator.py::run_backtest()` (reused unmodified for every
fold's actual simulation — no second, parity-risking engine).

Sweeps `ADX_TREND_THRESHOLD`/`TRAILING_ATR_MULTIPLIER` — the same two
parameters `optimizer/self_learning.py`'s live weekly tuner already
adjusts (`container.py`'s `ADX_TREND_THRESHOLD_BOUNDS`/
`TRAILING_ATR_MULTIPLIER_BOUNDS`), reused rather than reinvented.

Several mechanics genuinely aren't pinned down by RESEARCH.md/ADR-0004 at
the level of "how exactly does code implement this" — each such choice is
documented at its point of use below, and summarized in this phase's plan.
The two load-bearing ones:

1. Each fold's OOS segment is evaluated independently (fresh
   `starting_equity`, flat, `ACTIVE` drawdown state) rather than chaining
   state across folds — chaining would let one fold's `HARD_LOCK` freeze
   (a real, sticky failure mode Phase 1 already found live) silently zero
   out every later fold. Folds' OOS *return* series (not raw equity
   levels) are concatenated chronologically, then compounded into one
   synthetic OOS-only equity curve — RESEARCH.md §3's "concatenated...to
   preserve compounding/drawdown continuity across folds" read literally.
2. The Deflated Sharpe Ratio's `N` (trial count) is the parameter grid
   size, reused for both each fold's in-sample (IS) selection scoring and
   the final aggregate/promotion-gate DSR; `Var[SR]` for the final
   aggregate check is the mean of each fold's own IS cross-sectional
   Sharpe variance (RESEARCH.md doesn't specify this distinctly from the
   per-fold search step).
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from analytics.performance import (
    EquityCurve,
    FloatArray,
    PerformanceReport,
    build_performance_report,
    compute_returns,
    deflated_sharpe_ratio,
    infer_periods_per_year,
    kurtosis,
    sharpe_ratio,
    skewness,
)
from backtester.simulator import DEFAULT_STARTING_EQUITY, ClosedTrade, run_backtest
from broker.mt5_gateway import BarSeries

# docs/RESEARCH.md §4's anchored WFO fold parameterization.
DEFAULT_INITIAL_TRAIN_MONTHS = 24
DEFAULT_STEP_MONTHS = 1
DEFAULT_EMBARGO_DAYS = 1
DEFAULT_MIN_FOLDS = 12

# This session's reduced grid (see module docstring / this phase's plan for
# the ~7h-vs-~1.5h runtime rationale): ADX step 2.5 (7 values) x Trailing
# step 0.5 (5 values) = 35 combos. Pass adx_step=1.0, trailing_step=0.25 to
# `generate_parameter_grid()` for the full docs/RESEARCH.md-implied 144-combo
# grid (container.py's ADX_TREND_THRESHOLD_BOUNDS/TRAILING_ATR_MULTIPLIER_BOUNDS
# bounds, same parameters the live self-learning tuner already adjusts).
DEFAULT_ADX_MIN = 20.0
DEFAULT_ADX_MAX = 35.0
DEFAULT_ADX_STEP = 2.5
DEFAULT_TRAILING_MIN = 1.0
DEFAULT_TRAILING_MAX = 3.0
DEFAULT_TRAILING_STEP = 0.5

# A fold's OOS bar window starts this many calendar days before its own
# `test_start`, so `run_backtest()`'s own warmup logic (unmodified from
# Phase 1) has enough D1 history (`main.D1_BAR_COUNT`'s ~308-calendar-day
# requirement) to be evaluating real decisions right around `test_start`,
# not partway into the fold. Trades/equity-curve points before `test_start`
# are still discarded by `run_walk_forward_validation()` — this buffer only
# feeds warmup, real OOS accounting starts exactly at `test_start`.
OOS_WARMUP_BUFFER_DAYS = 350

# docs/RESEARCH.md §5's promotion gates. MAR_floor/max-OOS-drawdown-ceiling
# aren't defined anywhere concrete (RESEARCH.md defers them to a
# strategy/-owned parameter schema that doesn't exist in this codebase) —
# made-up-but-documented, flagged for review, same pattern as this
# codebase's other invented numeric defaults. MAX_OOS_DRAWDOWN_CEILING
# matches risk/drawdown_fsm.py's existing monthly HARD_LOCK ceiling for
# consistency, not a fresh number pulled from nowhere.
DEFAULT_DSR_GATE = 0.95
DEFAULT_IS_OOS_EFFICIENCY_GATE = 0.5
DEFAULT_MAR_FLOOR = 0.5
DEFAULT_MAX_OOS_DRAWDOWN_CEILING = 0.40
DEFAULT_MIN_OOS_TRADES_PER_FOLD = 30
DEFAULT_MIN_OOS_TRADES_TOTAL = 360


@dataclass(frozen=True, slots=True)
class Fold:
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime


def _add_months(moment: datetime, months: int) -> datetime:
    """Calendar month arithmetic (`timedelta` has no month unit). Assumes
    `moment.day` is valid in the target month — true for every call in
    this module, since `anchor`'s day-of-month never changes and is never
    late enough in a month to land on a nonexistent date (e.g. day 20 is
    valid in every month of the year, unlike day 31)."""
    total_month_index = moment.month - 1 + months
    year = moment.year + total_month_index // 12
    month = total_month_index % 12 + 1
    return moment.replace(year=year, month=month)


def generate_folds(
    anchor: datetime,
    data_end: datetime,
    *,
    initial_train_months: int = DEFAULT_INITIAL_TRAIN_MONTHS,
    step_months: int = DEFAULT_STEP_MONTHS,
    embargo_days: int = DEFAULT_EMBARGO_DAYS,
    min_folds: int = DEFAULT_MIN_FOLDS,
) -> list[Fold]:
    """Anchored walk-forward folds per `docs/RESEARCH.md` §4:
    `train_start` is always `anchor` (never advances — the defining
    "anchored" property); `train_end` starts at `anchor +
    initial_train_months` and advances by `step_months` each fold;
    `test_start = train_end + embargo_days`; `test_end = test_start +
    step_months`. Stops once a fold's `test_end` would exceed `data_end`.
    Raises `ValueError` if fewer than `min_folds` fit — a real, meaningful
    validation failure (insufficient history), not silently tolerated.
    """
    folds: list[Fold] = []
    train_end = _add_months(anchor, initial_train_months)
    while True:
        test_start = train_end + timedelta(days=embargo_days)
        test_end = _add_months(test_start, step_months)
        if test_end > data_end:
            break
        folds.append(
            Fold(train_start=anchor, train_end=train_end, test_start=test_start, test_end=test_end)
        )
        train_end = _add_months(train_end, step_months)

    if len(folds) < min_folds:
        raise ValueError(
            f"only {len(folds)} fold(s) fit in [{anchor}, {data_end}] with "
            f"initial_train_months={initial_train_months}, step_months={step_months}, "
            f"embargo_days={embargo_days} — docs/RESEARCH.md §4 requires at least "
            f"{min_folds}"
        )
    return folds


def _frange(start: float, stop: float, step: float) -> list[float]:
    count = round((stop - start) / step)
    return [start + i * step for i in range(count + 1)]


def generate_parameter_grid(
    *,
    adx_min: float = DEFAULT_ADX_MIN,
    adx_max: float = DEFAULT_ADX_MAX,
    adx_step: float = DEFAULT_ADX_STEP,
    trailing_min: float = DEFAULT_TRAILING_MIN,
    trailing_max: float = DEFAULT_TRAILING_MAX,
    trailing_step: float = DEFAULT_TRAILING_STEP,
) -> list[tuple[float, float]]:
    """`(adx_trend_threshold, trailing_atr_multiplier)` combinations to
    sweep. Defaults to this session's reduced 35-combo grid; pass
    `adx_step=1.0, trailing_step=0.25` for the full 144-combo
    `docs/RESEARCH.md`-implied grid."""
    adx_values = _frange(adx_min, adx_max, adx_step)
    trailing_values = _frange(trailing_min, trailing_max, trailing_step)
    return [(adx, trailing) for adx in adx_values for trailing in trailing_values]


def _slice_bars(bars: BarSeries, start_utc: datetime, end_utc: datetime) -> BarSeries:
    """Bars with `start_utc <= time_utc <= end_utc`, both inclusive."""
    times = bars.time_utc
    start_idx = bisect_left(times, start_utc)
    end_idx = bisect_right(times, end_utc)
    return BarSeries(
        open=bars.open[start_idx:end_idx],
        high=bars.high[start_idx:end_idx],
        low=bars.low[start_idx:end_idx],
        close=bars.close[start_idx:end_idx],
        tick_volume=bars.tick_volume[start_idx:end_idx],
        time_utc=times[start_idx:end_idx],
    )


@dataclass(frozen=True, slots=True)
class ComboResult:
    adx_trend_threshold: float
    trailing_atr_multiplier: float
    is_sharpe: float
    is_dsr: float
    is_trade_count: int


@dataclass(frozen=True, slots=True)
class FoldSelectionResult:
    fold: Fold
    winning_adx_trend_threshold: float
    winning_trailing_atr_multiplier: float
    winning_is_sharpe: float
    winning_is_dsr: float
    combo_results: tuple[ComboResult, ...]


def select_best_parameters_for_fold(
    d1_bars: BarSeries,
    h4_bars: BarSeries,
    h1_bars: BarSeries,
    fold: Fold,
    parameter_grid: list[tuple[float, float]],
    *,
    starting_equity: float = DEFAULT_STARTING_EQUITY,
) -> FoldSelectionResult:
    """Runs `run_backtest()` once per `parameter_grid` combo over this
    fold's IS window `[fold.train_start, fold.train_end]` (always
    `[anchor, train_end]` — the anchored property), scores every combo via
    `deflated_sharpe_ratio()` (`N` = grid size, `Var[SR]` = this fold's own
    cross-sectional IS Sharpe variance), and returns the winning combo.
    Every combo's IS result is recorded on `FoldSelectionResult` — never
    used as a promotion criterion (ADR-0004 §3), only diagnostic and to
    seed the next fold's OOS run.
    """
    is_d1 = _slice_bars(d1_bars, fold.train_start, fold.train_end)
    is_h4 = _slice_bars(h4_bars, fold.train_start, fold.train_end)
    is_h1 = _slice_bars(h1_bars, fold.train_start, fold.train_end)

    raw: list[tuple[float, float, float, float, float, int, int]] = []
    for adx, trailing in parameter_grid:
        result = run_backtest(
            is_d1,
            is_h4,
            is_h1,
            starting_equity=starting_equity,
            adx_trend_threshold=adx,
            trailing_atr_multiplier=trailing,
        )
        returns = compute_returns(result.equity_curve)
        periods_per_year = infer_periods_per_year(result.equity_curve)
        sharpe = sharpe_ratio(returns, periods_per_year=periods_per_year)
        raw.append(
            (
                adx,
                trailing,
                sharpe,
                skewness(returns),
                kurtosis(returns),
                len(returns),
                len(result.trades),
            )
        )

    n_trials = len(parameter_grid)
    sharpe_variance = float(np.var([r[2] for r in raw], ddof=0))

    combo_results = tuple(
        ComboResult(
            adx_trend_threshold=adx,
            trailing_atr_multiplier=trailing,
            is_sharpe=sharpe,
            is_dsr=deflated_sharpe_ratio(
                sharpe,
                n_observations=n_obs,
                skewness=skew,
                kurtosis=kurt,
                n_trials=n_trials,
                sharpe_variance_across_trials=sharpe_variance,
            ),
            is_trade_count=trade_count,
        )
        for adx, trailing, sharpe, skew, kurt, n_obs, trade_count in raw
    )
    winner = max(combo_results, key=lambda c: c.is_dsr)

    return FoldSelectionResult(
        fold=fold,
        winning_adx_trend_threshold=winner.adx_trend_threshold,
        winning_trailing_atr_multiplier=winner.trailing_atr_multiplier,
        winning_is_sharpe=winner.is_sharpe,
        winning_is_dsr=winner.is_dsr,
        combo_results=combo_results,
    )


@dataclass(frozen=True, slots=True)
class FoldRunResult:
    fold: Fold
    selection: FoldSelectionResult
    oos_trades: tuple[ClosedTrade, ...]
    oos_sharpe: float


@dataclass(frozen=True, slots=True)
class PromotionGateResult:
    """Each of `docs/RESEARCH.md` §5's 5 gates, individually and combined.
    Failing is not an error condition (RESEARCH.md §5: "logged at INFO/LOW
    severity, never treated as an incident") — the current parameter set
    just isn't promoted."""

    dsr_gate_passed: bool
    dsr_value: float
    is_oos_efficiency_gate_passed: bool
    is_oos_efficiency_ratio: float
    mar_gate_passed: bool
    mar_value: float
    max_drawdown_gate_passed: bool
    max_drawdown_value: float
    trade_count_gate_passed: bool
    total_oos_trades: int
    all_gates_passed: bool


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    folds: tuple[FoldRunResult, ...]
    concatenated_equity_curve: EquityCurve
    performance_report: PerformanceReport
    promotion_gates: PromotionGateResult


def _evaluate_promotion_gates(
    report: PerformanceReport,
    fold_results: tuple[FoldRunResult, ...],
    *,
    dsr_gate: float = DEFAULT_DSR_GATE,
    efficiency_gate: float = DEFAULT_IS_OOS_EFFICIENCY_GATE,
    mar_floor: float = DEFAULT_MAR_FLOOR,
    max_drawdown_ceiling: float = DEFAULT_MAX_OOS_DRAWDOWN_CEILING,
    min_trades_per_fold: int = DEFAULT_MIN_OOS_TRADES_PER_FOLD,
    min_trades_total: int = DEFAULT_MIN_OOS_TRADES_TOTAL,
) -> PromotionGateResult:
    dsr_value = report.deflated_sharpe_ratio if report.deflated_sharpe_ratio is not None else 0.0
    dsr_passed = bool(dsr_value >= dsr_gate)

    # IS/OOS efficiency ratio (RESEARCH.md §5 gate 2): the aggregate
    # concatenated OOS Sharpe against the mean of each fold's own winning-
    # combo IS Sharpe — RESEARCH.md doesn't specify per-fold-vs-aggregate
    # for a multi-fold WFO; this phase's documented interpretation.
    mean_winning_is_sharpe = float(np.mean([f.selection.winning_is_sharpe for f in fold_results]))
    efficiency_ratio = (
        report.sharpe_ratio / mean_winning_is_sharpe if mean_winning_is_sharpe != 0.0 else 0.0
    )
    efficiency_passed = bool(efficiency_ratio >= efficiency_gate)

    mar_passed = bool(report.mar_ratio >= mar_floor)
    drawdown_passed = bool(report.max_drawdown_pct <= max_drawdown_ceiling)

    total_oos_trades = sum(len(f.oos_trades) for f in fold_results)
    trades_per_fold_ok = all(len(f.oos_trades) >= min_trades_per_fold for f in fold_results)
    trade_count_passed = trades_per_fold_ok and total_oos_trades >= min_trades_total

    return PromotionGateResult(
        dsr_gate_passed=dsr_passed,
        dsr_value=dsr_value,
        is_oos_efficiency_gate_passed=efficiency_passed,
        is_oos_efficiency_ratio=efficiency_ratio,
        mar_gate_passed=mar_passed,
        mar_value=report.mar_ratio,
        max_drawdown_gate_passed=drawdown_passed,
        max_drawdown_value=report.max_drawdown_pct,
        trade_count_gate_passed=trade_count_passed,
        total_oos_trades=total_oos_trades,
        all_gates_passed=dsr_passed
        and efficiency_passed
        and mar_passed
        and drawdown_passed
        and trade_count_passed,
    )


def run_walk_forward_validation(
    d1_bars: BarSeries,
    h4_bars: BarSeries,
    h1_bars: BarSeries,
    *,
    parameter_grid: list[tuple[float, float]] | None = None,
    starting_equity: float = DEFAULT_STARTING_EQUITY,
    initial_train_months: int = DEFAULT_INITIAL_TRAIN_MONTHS,
    step_months: int = DEFAULT_STEP_MONTHS,
    embargo_days: int = DEFAULT_EMBARGO_DAYS,
    min_folds: int = DEFAULT_MIN_FOLDS,
) -> WalkForwardResult:
    """Orchestrates the full anchored WFO run: generates folds, for each
    one selects the winning parameter combo from its IS window then runs
    one fresh OOS `run_backtest()` with it, concatenates every fold's OOS
    *returns* (module docstring decision 1) into one synthetic compounded
    equity curve, computes the final `PerformanceReport` (with
    `deflated_sharpe_ratio` populated — module docstring decision 2), and
    evaluates `docs/RESEARCH.md` §5's 5 promotion gates.
    """
    if parameter_grid is None:
        parameter_grid = generate_parameter_grid()

    anchor = d1_bars.time_utc[0]
    data_end = h1_bars.time_utc[-1]
    folds = generate_folds(
        anchor,
        data_end,
        initial_train_months=initial_train_months,
        step_months=step_months,
        embargo_days=embargo_days,
        min_folds=min_folds,
    )

    fold_results: list[FoldRunResult] = []
    fold_is_variances: list[float] = []
    concatenated_returns: list[float] = []
    concatenated_trade_profits: list[float] = []
    synthetic_equity: list[float] = [starting_equity]
    synthetic_times: list[datetime] = [folds[0].test_start]

    for fold in folds:
        selection = select_best_parameters_for_fold(
            d1_bars, h4_bars, h1_bars, fold, parameter_grid, starting_equity=starting_equity
        )
        fold_is_variances.append(
            float(np.var([c.is_sharpe for c in selection.combo_results], ddof=0))
        )

        oos_window_start = fold.test_start - timedelta(days=OOS_WARMUP_BUFFER_DAYS)
        oos_d1 = _slice_bars(d1_bars, oos_window_start, fold.test_end)
        oos_h4 = _slice_bars(h4_bars, oos_window_start, fold.test_end)
        oos_h1 = _slice_bars(h1_bars, oos_window_start, fold.test_end)

        oos_result = run_backtest(
            oos_d1,
            oos_h4,
            oos_h1,
            starting_equity=starting_equity,
            adx_trend_threshold=selection.winning_adx_trend_threshold,
            trailing_atr_multiplier=selection.winning_trailing_atr_multiplier,
        )
        # Only the true OOS window counts — everything before test_start
        # was warmup for run_backtest()'s own indicator lookback, not real
        # OOS trading.
        oos_curve = [(t, e) for t, e in oos_result.equity_curve if t >= fold.test_start]
        oos_trades = tuple(t for t in oos_result.trades if t.exit_time >= fold.test_start)

        oos_returns: FloatArray = (
            compute_returns(oos_curve) if len(oos_curve) >= 2 else np.array([])
        )
        oos_periods_per_year = infer_periods_per_year(oos_curve) if len(oos_curve) >= 2 else 0.0
        oos_sharpe = sharpe_ratio(oos_returns, periods_per_year=oos_periods_per_year)

        fold_results.append(
            FoldRunResult(
                fold=fold, selection=selection, oos_trades=oos_trades, oos_sharpe=oos_sharpe
            )
        )
        concatenated_returns.extend(float(r) for r in oos_returns)
        concatenated_trade_profits.extend(t.profit for t in oos_trades)
        for r in oos_returns:
            synthetic_equity.append(synthetic_equity[-1] * (1.0 + float(r)))
        synthetic_times.extend(t for t, _ in oos_curve[1:])

    concatenated_equity_curve: EquityCurve = tuple(
        zip(synthetic_times, synthetic_equity, strict=True)
    )
    mean_is_variance = float(np.mean(fold_is_variances)) if fold_is_variances else 0.0

    report = build_performance_report(
        concatenated_equity_curve,
        concatenated_trade_profits,
        n_trials=len(parameter_grid),
        sharpe_variance_across_trials=mean_is_variance,
    )
    gates = _evaluate_promotion_gates(report, tuple(fold_results))

    return WalkForwardResult(
        folds=tuple(fold_results),
        concatenated_equity_curve=concatenated_equity_curve,
        performance_report=report,
        promotion_gates=gates,
    )
