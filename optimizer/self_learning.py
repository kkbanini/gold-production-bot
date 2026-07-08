"""Weekend self-learning optimizer: APScheduler-gated rule-based parameter
shifting (at most one parameter change per iteration) plus a
1000-iteration Monte Carlo bootstrap validator over closed-trade P&L.

Isolation guarantee: this module only ever reads storage/'s CLOSED trade
history (via `StateManager.get_closed_trades()`) and appends to the
dedicated `parameter_history` table (via `StateManager.record_parameter_change()`).
It never calls `save_fsm_state()` and never reads/writes an open
`trade_ledger` row, so no active trading state can be touched by this
background module — scheduled or manually invoked.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from storage.state_manager import StateManager, TradeLedgerEntry

MIN_TRADES_FOR_ADJUSTMENT = 10
LOW_WIN_RATE_THRESHOLD = 0.40
LOW_PROFIT_FACTOR_THRESHOLD = 1.0
BOOTSTRAP_ITERATIONS = 1000

# APScheduler's day_of_week name for Saturday. The hour is chosen well
# within the fully-closed weekend window (market closes ~Friday 22:00 GMT,
# reopens ~Sunday 22:00 GMT) — not a policy decision, just "safely mid-Saturday".
WEEKEND_OPTIMIZATION_DAY_OF_WEEK = "sat"
WEEKEND_OPTIMIZATION_HOUR_UTC = 3
WEEKEND_OPTIMIZER_JOB_ID = "weekend_self_learning_optimizer"


@dataclass(frozen=True, slots=True)
class TunableParameter:
    """A single parameter eligible for rule-based adjustment, with the
    bounds within which `decide_parameter_shift` may move it."""

    name: str
    value: float
    min_value: float
    max_value: float
    step: float


@dataclass(frozen=True, slots=True)
class LedgerPerformanceMetrics:
    """Pure aggregation over already-closed trade_ledger rows."""

    trade_count: int
    win_rate: float
    profit_factor: float
    total_profit: float


@dataclass(frozen=True, slots=True)
class ParameterShiftDecision:
    """The single parameter change (if any) `decide_parameter_shift` chose
    to make this iteration."""

    parameter_name: str
    old_value: float
    new_value: float
    reason: str


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """Summary of a 1000-iteration (default) trade-sequence bootstrap."""

    iterations: int
    observed_final_pnl: float
    observed_max_drawdown: float
    bootstrap_p05_final_pnl: float
    bootstrap_p95_final_pnl: float
    fraction_profitable: float


@dataclass(frozen=True, slots=True)
class WeeklyOptimizationCycleResult:
    """Result of a single call to `run_weekly_optimization_cycle`."""

    ran: bool
    skipped_reason: str | None
    metrics: LedgerPerformanceMetrics | None
    shift_decision: ParameterShiftDecision | None
    bootstrap_result: BootstrapResult | None


def get_effective_parameter_value(
    state_manager: StateManager, parameter_name: str, default_value: float
) -> float:
    """The value the live strategy should actually use for `parameter_name`
    right now: the most recent shift `decide_parameter_shift()` applied
    (`state_manager.get_latest_parameter_value()`), or `default_value` if
    it's never been shifted.

    The single shared resolution point between the weekly job (which
    seeds each `TunableParameter.value` from here before deciding the
    *next* shift) and the live bar-close loop (which reads the same
    value every cycle to actually change behavior) — without this, a
    recorded shift would sit in `parameter_history` forever with nothing
    ever reading it back.
    """
    latest = state_manager.get_latest_parameter_value(parameter_name)
    return latest if latest is not None else default_value


def is_market_closed_for_optimization(now_utc: datetime) -> bool:
    """True only on Saturday (UTC) — the day the Gold market is fully
    closed all day (closes ~Friday 22:00 GMT, reopens ~Sunday 22:00 GMT).

    This is a second, independent gate beyond the scheduler's own
    `CronTrigger(day_of_week="sat")`: `run_weekly_optimization_cycle`
    re-checks it before doing any work, so even a manual/direct call to
    the job function outside the scheduler cannot run on a non-Saturday.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    return now_utc.astimezone(timezone.utc).weekday() == 5


def compute_ledger_metrics(closed_trades: list[TradeLedgerEntry]) -> LedgerPerformanceMetrics:
    """Pure aggregation over already-closed trade_ledger rows."""
    profits = [trade.profit for trade in closed_trades if trade.profit is not None]
    if not profits:
        return LedgerPerformanceMetrics(
            trade_count=0, win_rate=0.0, profit_factor=0.0, total_profit=0.0
        )

    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    elif gross_win > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    return LedgerPerformanceMetrics(
        trade_count=len(profits),
        win_rate=len(wins) / len(profits),
        profit_factor=profit_factor,
        total_profit=sum(profits),
    )


def decide_parameter_shift(
    tunable_parameters: dict[str, TunableParameter], metrics: LedgerPerformanceMetrics
) -> ParameterShiftDecision | None:
    """Rule-based configuration shift: at most ONE parameter changes per
    call, and only if a rule fires — otherwise returns `None`.

    The specific thresholds and which parameter each rule adjusts
    (`LOW_WIN_RATE_THRESHOLD` -> `ADX_TREND_THRESHOLD`,
    `LOW_PROFIT_FACTOR_THRESHOLD` -> `TRAILING_ATR_MULTIPLIER`) are this
    implementation's design choice, not a pre-existing spec — the phase
    directive asked for "rule-based" shifting without giving exact
    trigger thresholds, flagged for review in `optimizer/README.md`.
    """
    if metrics.trade_count < MIN_TRADES_FOR_ADJUSTMENT:
        return None

    if metrics.win_rate < LOW_WIN_RATE_THRESHOLD:
        return _shift_toward_max(
            tunable_parameters.get("ADX_TREND_THRESHOLD"),
            reason=(
                f"win_rate {metrics.win_rate:.2%} below "
                f"{LOW_WIN_RATE_THRESHOLD:.0%} threshold; tightening trend-strength filter"
            ),
        )

    if metrics.profit_factor < LOW_PROFIT_FACTOR_THRESHOLD:
        return _shift_toward_max(
            tunable_parameters.get("TRAILING_ATR_MULTIPLIER"),
            reason=(
                f"profit_factor {metrics.profit_factor:.2f} below "
                f"{LOW_PROFIT_FACTOR_THRESHOLD:.2f} threshold; widening trailing stop"
            ),
        )

    return None


def _shift_toward_max(
    parameter: TunableParameter | None, *, reason: str
) -> ParameterShiftDecision | None:
    if parameter is None:
        return None
    new_value = min(parameter.value + parameter.step, parameter.max_value)
    if new_value == parameter.value:
        return None
    return ParameterShiftDecision(
        parameter_name=parameter.name,
        old_value=parameter.value,
        new_value=new_value,
        reason=reason,
    )


def run_monte_carlo_bootstrap(
    trade_profits: list[float],
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    rng: random.Random | None = None,
) -> BootstrapResult:
    """Resample `trade_profits` WITH replacement `iterations` times (each
    resample the same length as the original sequence), building a
    distribution of possible cumulative-P&L outcomes under random
    trade-sequence variation.

    Tests whether the observed performance is robust to trade ordering,
    rather than an artifact of a lucky sequence of wins and losses.
    """
    if iterations < 1:
        raise ValueError(f"iterations must be >= 1, got {iterations}")
    if not trade_profits:
        raise ValueError("trade_profits must not be empty")

    generator = rng if rng is not None else random.Random()
    n = len(trade_profits)

    observed_cumulative = _cumulative_sum(trade_profits)

    bootstrap_final_pnls = sorted(
        sum(generator.choice(trade_profits) for _ in range(n)) for _ in range(iterations)
    )
    fraction_profitable = sum(1 for pnl in bootstrap_final_pnls if pnl > 0) / iterations

    return BootstrapResult(
        iterations=iterations,
        observed_final_pnl=observed_cumulative[-1],
        observed_max_drawdown=_max_drawdown(observed_cumulative),
        bootstrap_p05_final_pnl=_percentile(bootstrap_final_pnls, 0.05),
        bootstrap_p95_final_pnl=_percentile(bootstrap_final_pnls, 0.95),
        fraction_profitable=fraction_profitable,
    )


def _cumulative_sum(values: list[float]) -> list[float]:
    result: list[float] = []
    total = 0.0
    for value in values:
        total += value
        result.append(total)
    return result


def _max_drawdown(cumulative: list[float]) -> float:
    peak = float("-inf")
    max_dd = 0.0
    for value in cumulative:
        peak = max(peak, value)
        max_dd = max(max_dd, peak - value)
    return max_dd


def _percentile(sorted_values: list[float], fraction: float) -> float:
    index = min(int(fraction * len(sorted_values)), len(sorted_values) - 1)
    return sorted_values[index]


def run_weekly_optimization_cycle(
    state_manager: StateManager,
    tunable_parameters: dict[str, TunableParameter],
    *,
    now_utc: datetime | None = None,
    bootstrap_iterations: int = BOOTSTRAP_ITERATIONS,
    rng: random.Random | None = None,
) -> WeeklyOptimizationCycleResult:
    """The single entry point the scheduled job calls.

    Isolation guarantee: reads only `state_manager.get_closed_trades()`
    (never an open position); the only write is
    `state_manager.record_parameter_change()`, which appends to the
    isolated `parameter_history` table. Never calls `save_fsm_state()`.
    """
    current_time = now_utc if now_utc is not None else datetime.now(timezone.utc)
    if not is_market_closed_for_optimization(current_time):
        return WeeklyOptimizationCycleResult(
            ran=False,
            skipped_reason=(
                "not Saturday (UTC); the weekend optimizer only runs when the market is closed"
            ),
            metrics=None,
            shift_decision=None,
            bootstrap_result=None,
        )

    closed_trades = state_manager.get_closed_trades()
    metrics = compute_ledger_metrics(closed_trades)

    shift_decision = decide_parameter_shift(tunable_parameters, metrics)
    if shift_decision is not None:
        state_manager.record_parameter_change(
            parameter_name=shift_decision.parameter_name,
            old_value=shift_decision.old_value,
            new_value=shift_decision.new_value,
            reason=shift_decision.reason,
        )

    profits = [trade.profit for trade in closed_trades if trade.profit is not None]
    bootstrap_result = (
        run_monte_carlo_bootstrap(profits, iterations=bootstrap_iterations, rng=rng)
        if profits
        else None
    )

    return WeeklyOptimizationCycleResult(
        ran=True,
        skipped_reason=None,
        metrics=metrics,
        shift_decision=shift_decision,
        bootstrap_result=bootstrap_result,
    )


def create_weekend_optimizer_scheduler(
    job: Callable[[], WeeklyOptimizationCycleResult],
) -> BackgroundScheduler:
    """Construct a `BackgroundScheduler` with a single job locked to
    Saturdays via `CronTrigger(day_of_week="sat")`.

    This is the primary gate; `run_weekly_optimization_cycle`'s own
    `is_market_closed_for_optimization` check is a second, independent
    gate so a direct call to `job` outside the scheduler still can't run
    on a non-Saturday.
    """
    scheduler = BackgroundScheduler()
    trigger = CronTrigger(
        day_of_week=WEEKEND_OPTIMIZATION_DAY_OF_WEEK,
        hour=WEEKEND_OPTIMIZATION_HOUR_UTC,
        timezone="UTC",
    )
    scheduler.add_job(job, trigger=trigger, id=WEEKEND_OPTIMIZER_JOB_ID)
    return scheduler
