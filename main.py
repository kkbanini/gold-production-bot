"""Master FSM orchestration loop: connects every module built in Phases
1-9 into a single event-driven process that acts once per new M5 bar
close (docs/RUNBOOK.md §1's boot sequence, ADR-0001's event-driven
principle applied without a full EventBus/`core`, which no phase built).

This module owns the trading state machine (TradingState) and the 200ms
processing-cap metric. Global capital protection (drawdown) is Phase 11d's
`risk/drawdown_fsm.py` — a separate, centralized pure-function FSM
(`docs/PRODUCTION_SPEC.md` §6) this module only *consumes* (calls
`classify_drawdown_event()`/`transition_drawdown_state()` and applies the
resulting `blocks_new_entries()`/`blocks_position_management()`
predicates), never reimplements. It does NOT implement a pre-trade risk
gate or slippage guard (RQ-009/RQ-010) — those remain open gaps, see
docs/TRACEABILITY_MATRIX.md and docs/ARCHITECTURE_SUMMARY.md.

`run_bar_close_cycle()` is a pure decision function: no I/O, fully
deterministic given its inputs, and the unit of testing.
`ApplicationContainer.build()` (container.py) and `main()` are the impure
I/O layer that fetches real data and executes decisions against the
broker — reviewed for correctness but not executed against a live/demo
account in this environment (no real MT5 credentials exist here; see
docs/ARCHITECTURE_SUMMARY.md "Before your first live/demo run").

`submit_with_pre_flight_ledger()` (Phase 11c, docs/PRODUCTION_SPEC.md §4)
wraps every real broker-submission call site in `main()`'s loop body
(position actions, a new entry, and Phase 11d's emergency liquidation): it
writes a REQUESTED event to the storage/ Event Store before the payload
reaches the MT5 gateway, then SENT + a terminal FILLED/MODIFIED event on
success or REJECTED (re-raising unchanged) on rejection. Unlike the rest
of `main()`'s loop body, this function takes its I/O as injected
parameters (a StateManager and a submit callable) rather than reaching
for globals, so it is directly unit-tested
(tests/unit/test_unit.py::TestSubmitWithPreFlightLedger) despite being
impure. It does not itself retry — no automated order-submission retry
loop exists in this module (see docs/ARCHITECTURE_SUMMARY.md §5).

`_seed_initial_fsm_context()` (Phase 11e, docs/PRODUCTION_SPEC.md §7)
is Disaster Recovery's second half: `main()` calls it right after
`ApplicationContainer.build()` to seed the starting `FSMContext` directly
from the broker's live open positions (`container.gateway.get_open_positions_by_magic()`)
rather than trusting a possibly-stale local snapshot, and from
`container.initial_drawdown_state` (computed by `build()`'s reconciliation
— `MANUAL_RESET_REQUIRED` if a broker/ledger divergence was found and
settled, `ACTIVE` otherwise). Formally tested against a real
`ApplicationContainer` built with a `FakeMT5`
(tests/integration/test_integration.py::TestApplicationContainer).

`main()`'s loop also skips a cycle entirely (no MT5 calls at all) during
the weekly forex/CFD market closure (`broker.mt5_gateway.is_weekend_market_closed()`,
Friday 22:00 UTC - Sunday 22:00 UTC), rechecking every `WEEKEND_RECHECK_SECONDS`
instead of every 5-minute bar close. This is a separate axis from
`is_within_execution_window()`'s daily GMT hour filter, which remains
unwired (see docs/ARCHITECTURE_SUMMARY.md §5).

`config.trading_mode` (`WAIT_FOR_CONDITIONS`/`SHORT_TERM`/`BOTH`) selects
between the original D1+H4+H1-aligned strategy above and a second,
independent short-term (scalp) mode (`decide_short_term_entry_signal()`,
`strategy.trend_filter.evaluate_short_term_trend()`): relaxed to H1
direction + a lower ADX bar, fixed minimum lot, an ATR-based SL and a
fixed-dollar-profit TP (`SHORT_TERM_TP_TARGET_USD`, converted to a price
distance via `risk.risk_manager.calculate_price_distance_for_target_profit()`)
that MT5 closes automatically, or the profit-peak lock
(`decide_short_term_profit_lock()`) closes early if price retraces from
its best-favorable point while still in profit — so, unlike the regular
position, it is never tracked in `FSMContext` (stateless; `main()`
re-queries it from the broker every cycle via
`config.short_term_magic_number`, a distinct magic number so the two
modes' positions never collide). Short-term fills ARE written to
`trade_ledger` (recorded `OPEN` at entry; reconciled `CLOSED` with the
real outcome once `_fetch_short_term_position()` detects the close
mid-loop, or `MT5Gateway.reconcile_short_term_closes()` catches it at the
next boot if the process was stopped when it happened), feeding
`optimizer/self_learning.py`'s analytics.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, TypeVar

from broker.mt5_gateway import (
    TIMEFRAME_D1,
    TIMEFRAME_H1,
    TIMEFRAME_H4,
    WEEKEND_CLOSE_HOUR_UTC,
    WEEKEND_REOPEN_HOUR_UTC,
    AccountState,
    BrokerOrderRejectedError,
    BrokerPosition,
    MT5Gateway,
    is_weekend_market_closed,
)
from config.feature_flags import FeatureFlagManager
from container import ApplicationContainer
from execution.position_manager import (
    BASE_TP_ATR_MULTIPLIER,
    EMERGENCY_LIQUIDATION_COMMENT,
    TRAILING_ATR_MULTIPLIER,
    OrderActionPayload,
    PositionState,
    build_emergency_liquidation_action,
    build_short_term_liquidation_action,
    calculate_trailing_stop,
    evaluate_partial_close_and_breakeven,
)
from indicators.math_engine import atr, ema
from news.news_engine import EconomicEvent, is_trade_entry_locked
from optimizer.self_learning import get_effective_parameter_value
from risk.drawdown_fsm import (
    BaselineEpoch,
    DrawdownClassification,
    DrawdownEvent,
    DrawdownState,
    EquityBaselines,
    blocks_new_entries,
    blocks_position_management,
    classify_drawdown_event,
    decide_hard_lock_response,
    roll_equity_baselines,
    seed_equity_baselines,
    transition_drawdown_state,
)
from risk.risk_manager import (
    calculate_compounded_lot_size,
    calculate_price_distance_for_target_profit,
)
from storage.state_manager import OrderLifecycleState, StateManager, TradeLedgerEntry
from strategy.execution_triggers import (
    BreakoutSignal,
    PullbackSignal,
    WickFillResult,
    analyze_wick_fill,
    detect_breakout,
    detect_pullback,
)
from strategy.trend_filter import (
    ADX_TREND_THRESHOLD,
    H1_EMA_PERIOD,
    ShortTermTrendAlignment,
    TrendAlignment,
    evaluate_master_trend,
    evaluate_short_term_trend,
)

logger = logging.getLogger(__name__)

Direction = Literal["BUY", "SELL", "NONE"]

PROCESSING_CAP_MS = 200.0
BAR_CLOSE_TIMEFRAME_MINUTES = 5

# How often main() rechecks whether the weekend market closure has ended,
# while it's still closed. Deliberately longer than the 5-minute bar-close
# cadence — nothing is going to change for hours during a weekend closure,
# so there is no reason to burn a full bar-close cycle's worth of MT5
# calls every 5 minutes just to find that out again.
WEEKEND_RECHECK_SECONDS = 900.0

# Distance (in ATR multiples) from the current price to the initial
# stop-loss on a new entry. Mirrors position_manager's Base_TP multiplier
# for symmetry; not independently specified anywhere, flagged for review.
ENTRY_ATR_STOP_MULTIPLIER = BASE_TP_ATR_MULTIPLIER

# Mirrors config.config_manager.VALID_TRADING_MODES exactly — kept as
# plain string literals here (not re-exported constants) since
# config/config_manager.py is the sole validator/owner of this value;
# main.py only ever compares against it, never re-derives it.
TRADING_MODE_WAIT_FOR_CONDITIONS = "WAIT_FOR_CONDITIONS"
TRADING_MODE_SHORT_TERM = "SHORT_TERM"
TRADING_MODE_BOTH = "BOTH"

# Short-term (scalp) mode's fixed SL (ATR-based) and TP (a fixed dollar
# profit target, converted to a price distance via the broker's own
# tick_value/tick_size — risk.risk_manager.calculate_price_distance_for_target_profit()
# — so a fixed lot still means the SAME dollar outcome regardless of
# instrument/broker, unlike a fixed ATR multiple). MT5 closes the
# position automatically at whichever is hit first (or the profit-peak
# lock below fires first), so no per-cycle trailing/partial-close
# management like the regular position gets. No spec reference for
# either — made-up-but-documented defaults.
SHORT_TERM_SL_ATR_MULTIPLIER = 1.0
SHORT_TERM_TP_TARGET_USD = 5.0

# The profit-peak lock's retracement trigger: once a short-term position
# has been in profit and price retraces from its best-favorable point by
# this many ATR, close immediately rather than riding it back down to the
# fixed SL or waiting for the fixed TP. No spec reference — a made-up-
# but-documented default, independent of the fixed TP/SL above.
SHORT_TERM_PROFIT_LOCK_RETRACEMENT_ATR_MULTIPLIER = 0.5

# Distinct from EMERGENCY_LIQUIDATION_COMMENT: main()'s existing
# `if action.comment == EMERGENCY_LIQUIDATION_COMMENT` check clears the
# *regular* position's FSMContext, which must never happen for a
# short-term-only profit-lock close unrelated to a real HARD_LOCK.
SHORT_TERM_PROFIT_LOCK_COMMENT = "short_term_profit_lock"

D1_BAR_COUNT = 220
H4_BAR_COUNT = 70
H1_BAR_COUNT = 67
M5_BAR_COUNT = 40


class TradingState(str, Enum):
    """Master FSM states for the orchestration loop: flat vs. in-position
    bookkeeping only. Capital-protection halting is a fully orthogonal
    axis (`FSMContext.drawdown_state`, `risk/drawdown_fsm.py`) — a
    position can be `IN_POSITION` while the account is simultaneously
    `SOFT_LOCK`ed, since `SOFT_LOCK` explicitly keeps position management
    running (docs/PRODUCTION_SPEC.md §6). Phase 11d removed the old
    `HALTED` member, which conflated these two concerns."""

    INITIALIZING = "INITIALIZING"
    IDLE = "IDLE"
    IN_POSITION = "IN_POSITION"


@dataclass(frozen=True, slots=True)
class ProcessingCapResult:
    duration_ms: float
    exceeded_cap: bool


@dataclass(frozen=True, slots=True)
class EntryDecision:
    """Result of combining the master trend filter with the three
    independent entry-trigger signals into a single entry direction."""

    direction: Direction
    reason: str


@dataclass(frozen=True, slots=True)
class FSMContext:
    """The orchestration loop's full state, threaded through every
    bar-close cycle. `state`/`position` track flat-vs-in-position;
    `drawdown_state`/`drawdown_reason` (Phase 11d) track the independent
    capital-protection FSM (`risk/drawdown_fsm.py`)."""

    state: TradingState
    position: PositionState | None
    drawdown_state: DrawdownState
    drawdown_reason: str | None


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Everything about the current market/account `run_bar_close_cycle`
    needs, gathered by the (impure) caller before invoking it.

    `short_term_trend`/`short_term_pullback` are the short-term mode's own
    relaxed trend/pullback signals (default `None` when that mode isn't
    enabled) — `short_term_pullback` is a SEPARATE computation from
    `pullback` above, not a reuse of it, since `pullback` is computed
    against the *regular* trend's direction, which can disagree with the
    short-term trend's direction (that disagreement is the whole point of
    relaxing the condition)."""

    now_utc: datetime
    account_state: AccountState
    current_price: float | None
    atr_value: float
    trend: TrendAlignment
    breakout: BreakoutSignal
    pullback: PullbackSignal
    wick_fill: WickFillResult
    news_events: list[EconomicEvent]
    short_term_trend: ShortTermTrendAlignment | None = None
    short_term_pullback: PullbackSignal | None = None


@dataclass(frozen=True, slots=True)
class SymbolConstraints:
    point: float
    volume_min: float
    volume_max: float
    volume_step: float
    magic_number: int
    tick_value: float
    tick_size: float


@dataclass(frozen=True, slots=True)
class BarCloseCycleResult:
    """Everything one call to `run_bar_close_cycle` decided. `context`
    only reflects the drawdown-FSM transition (`drawdown_state`/
    `drawdown_reason`) — flat/in-position transitions happen in `main()`
    after the broker confirms the corresponding action succeeded, which
    this pure function cannot know."""

    context: FSMContext
    drawdown: DrawdownClassification
    processing: ProcessingCapResult
    entry_decision: EntryDecision | None
    entry_stop_loss: float | None
    entry_volume: float | None
    position_actions: tuple[OrderActionPayload, ...]
    short_term_entry_decision: EntryDecision | None = None
    short_term_entry_stop_loss: float | None = None
    short_term_entry_take_profit: float | None = None
    short_term_entry_volume: float | None = None
    short_term_peak_price: float | None = None


def seconds_until_next_bar_close(
    now_utc: datetime, timeframe_minutes: int = BAR_CLOSE_TIMEFRAME_MINUTES
) -> float:
    """Seconds remaining until the next bar-close boundary for
    `timeframe_minutes` (default M5): e.g. at 12:03:30 with a 5-minute
    timeframe, the next boundary is 12:05:00, so this returns 90.0.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    interval_seconds = timeframe_minutes * 60
    seconds_into_interval = now_utc.timestamp() % interval_seconds
    return interval_seconds - seconds_into_interval if seconds_into_interval > 0 else 0.0


def evaluate_processing_time(duration_seconds: float) -> ProcessingCapResult:
    """Convert a measured wall-clock duration into a `ProcessingCapResult`.

    A breach is logged by the caller, not a trading halt — the 200ms cap
    is an operational performance metric, not a risk circuit breaker.
    """
    if duration_seconds < 0:
        raise ValueError(f"duration_seconds must be >= 0, got {duration_seconds}")
    duration_ms = duration_seconds * 1000.0
    return ProcessingCapResult(
        duration_ms=duration_ms, exceeded_cap=duration_ms > PROCESSING_CAP_MS
    )


def _select_trigger_signal(
    trend_direction: Direction,
    breakout: BreakoutSignal,
    pullback: PullbackSignal,
    wick_fill: WickFillResult,
) -> EntryDecision:
    """Pick the first of breakout/pullback/wick-fill that agrees with
    `trend_direction`, shared by both `decide_entry_signal()` and
    `decide_short_term_entry_signal()` — the exact same selection
    procedure, only the trend-validity gate upstream of it differs."""
    if breakout.is_valid and breakout.direction == trend_direction:
        return EntryDecision(trend_direction, f"breakout confirms {trend_direction} trend")
    if pullback.is_valid and pullback.direction == trend_direction:
        return EntryDecision(trend_direction, f"pullback confirms {trend_direction} trend")
    if wick_fill.is_significant and wick_fill.rejection == trend_direction:
        return EntryDecision(
            trend_direction, f"wick-fill rejection confirms {trend_direction} trend"
        )
    return EntryDecision("NONE", "no entry trigger agrees with the confirmed trend direction")


def decide_entry_signal(
    trend: TrendAlignment,
    breakout: BreakoutSignal,
    pullback: PullbackSignal,
    wick_fill: WickFillResult,
    news_locked: bool,
) -> EntryDecision:
    """Combine the master trend filter with the three independent
    entry-trigger signals into a single entry decision.

    Flagged design choice: no prior phase specified how to combine these
    three independent signals (each explicitly deferred this "to
    execution/, a later phase" — see strategy/README.md). Rule: an entry
    requires (1) a valid, ADX-confirmed master trend alignment, (2) no
    active news blackout, and (3) at least one trigger signal (breakout,
    pullback, or a wick-fill rejection) agreeing with the trend direction.
    """
    if news_locked:
        return EntryDecision("NONE", "news blackout window active")
    if not trend.is_valid:
        return EntryDecision("NONE", "no confirmed master trend alignment")

    trend_direction: Direction = "BUY" if trend.direction == "BULLISH" else "SELL"
    return _select_trigger_signal(trend_direction, breakout, pullback, wick_fill)


def decide_short_term_entry_signal(
    short_trend: ShortTermTrendAlignment,
    breakout: BreakoutSignal,
    short_term_pullback: PullbackSignal,
    wick_fill: WickFillResult,
    news_locked: bool,
) -> EntryDecision:
    """The short-term (scalp) mode's entry decision: identical shape to
    `decide_entry_signal()`, but gated on `short_trend.is_valid` (H1
    direction + a lower ADX bar, no D1/H4 alignment requirement) instead
    of the full master trend. `short_term_pullback` must be computed
    against `short_trend.direction`, not the regular trend's direction —
    see `MarketSnapshot`'s docstring.
    """
    if news_locked:
        return EntryDecision("NONE", "news blackout window active")
    if not short_trend.is_valid:
        return EntryDecision("NONE", "no confirmed short-term trend alignment")

    trend_direction: Direction = "BUY" if short_trend.direction == "BULLISH" else "SELL"
    return _select_trigger_signal(trend_direction, breakout, short_term_pullback, wick_fill)


def _evaluate_drawdown_transition(
    context: FSMContext,
    snapshot: MarketSnapshot,
    baselines: EquityBaselines,
    *,
    manual_reset_confirmed: bool,
) -> tuple[FSMContext, DrawdownClassification]:
    """Classifies this cycle's drawdown severity, transitions
    `context.drawdown_state` through `risk/drawdown_fsm.py`'s pure FSM,
    logs at a severity matching the resulting state, and returns the
    updated `FSMContext` (drawdown fields only; `state`/`position` pass
    through unchanged) alongside the raw classification. Factored out of
    `run_bar_close_cycle()` to keep that function's cyclomatic complexity
    under this project's `ruff`-enforced limit (`pyproject.toml`
    `max-complexity = 10`).
    """
    classification = classify_drawdown_event(snapshot.account_state.equity, baselines)
    drawdown_event = (
        DrawdownEvent.MANUAL_RESET_CONFIRMED if manual_reset_confirmed else classification.event
    )
    new_drawdown_state = transition_drawdown_state(context.drawdown_state, drawdown_event)

    drawdown_reason: str | None = None
    if new_drawdown_state != DrawdownState.ACTIVE:
        drawdown_reason = (
            f"drawdown_state={new_drawdown_state.value} event={drawdown_event.value} "
            f"(daily={classification.daily_drawdown_pct:.1%}, "
            f"weekly={classification.weekly_drawdown_pct:.1%}, "
            f"monthly={classification.monthly_drawdown_pct:.1%})"
        )
        if new_drawdown_state == DrawdownState.WARNING:
            logger.warning("Drawdown WARNING: %s", drawdown_reason)
        elif new_drawdown_state == DrawdownState.SOFT_LOCK:
            logger.error("Drawdown SOFT_LOCK (new entries frozen): %s", drawdown_reason)
        else:
            logger.critical("Drawdown %s: %s", new_drawdown_state.value, drawdown_reason)

    updated_context = FSMContext(
        state=context.state,
        position=context.position,
        drawdown_state=new_drawdown_state,
        drawdown_reason=drawdown_reason,
    )
    return updated_context, classification


def _handle_hard_lock_response(
    context: FSMContext,
    updated_context: FSMContext,
    classification: DrawdownClassification,
    processing: ProcessingCapResult,
    feature_flags: FeatureFlagManager,
    short_term_position: BrokerPosition | None,
) -> BarCloseCycleResult | None:
    """Returns a terminal `BarCloseCycleResult` if `updated_context` is a
    fresh `HARD_LOCK` breach this cycle (either an emergency-liquidation
    action, or an empty-action freeze), or `None` if no `HARD_LOCK`
    response applies so `run_bar_close_cycle()` should continue its normal
    decision flow. Factored out for the same `max-complexity` reason as
    `_evaluate_drawdown_transition()`.

    `short_term_position`, if open, is liquidated/frozen in lockstep with
    `context.position` — `should_liquidate` gates both symmetrically
    (never liquidate one while freezing the other in place).
    """
    hard_lock_response = decide_hard_lock_response(
        updated_context.drawdown_state, liquidate_on_hard_lock=feature_flags.liquidate_on_hard_lock
    )
    if hard_lock_response is None:
        return None

    empty_result = BarCloseCycleResult(
        context=updated_context,
        drawdown=classification,
        processing=processing,
        entry_decision=None,
        entry_stop_loss=None,
        entry_volume=None,
        position_actions=(),
    )
    if not hard_lock_response.should_liquidate:
        logger.critical("Absolute system freeze triggered: %s", hard_lock_response.reason)
        return empty_result
    if context.position is None and short_term_position is None:
        logger.critical("Absolute system freeze triggered: %s", hard_lock_response.reason)
        return empty_result

    logger.critical("Emergency liquidation triggered: %s", hard_lock_response.reason)
    actions: list[OrderActionPayload] = []
    if context.position is not None:
        actions.append(build_emergency_liquidation_action(context.position))
    if short_term_position is not None:
        actions.append(
            build_short_term_liquidation_action(
                ticket=short_term_position.ticket,
                symbol=short_term_position.symbol,
                magic_number=short_term_position.magic,
                volume=short_term_position.volume,
            )
        )
    return BarCloseCycleResult(
        context=updated_context,
        drawdown=classification,
        processing=processing,
        entry_decision=None,
        entry_stop_loss=None,
        entry_volume=None,
        position_actions=tuple(actions),
    )


def _evaluate_short_term_entry(
    trading_mode: str,
    short_term_position: BrokerPosition | None,
    new_drawdown_state: DrawdownState,
    news_locked: bool,
    snapshot: MarketSnapshot,
    constraints: SymbolConstraints,
) -> tuple[EntryDecision | None, float | None, float | None, float | None]:
    """Independent short-term (scalp) entry evaluation — orthogonal to the
    regular mode's `context.position is None` branch in
    `run_bar_close_cycle()`, since a short-term position is never tracked
    in `FSMContext` (stateless; MT5 manages its exit itself via a fixed
    SL/TP, so no per-cycle trailing/partial-close is needed). Returns
    `(entry_decision, stop_loss, take_profit, volume)`, all `None` when
    short-term mode is disabled, a short-term position is already open,
    new entries are currently blocked, or required snapshot data is
    missing.
    """
    if trading_mode not in (TRADING_MODE_SHORT_TERM, TRADING_MODE_BOTH):
        return None, None, None, None
    if short_term_position is not None:
        return None, None, None, None
    if blocks_new_entries(new_drawdown_state):
        return None, None, None, None
    if snapshot.short_term_trend is None or snapshot.short_term_pullback is None:
        return None, None, None, None

    decision = decide_short_term_entry_signal(
        snapshot.short_term_trend,
        snapshot.breakout,
        snapshot.short_term_pullback,
        snapshot.wick_fill,
        news_locked,
    )
    if decision.direction == "NONE" or snapshot.current_price is None:
        return decision, None, None, None

    volume = constraints.volume_min
    stop_distance = SHORT_TERM_SL_ATR_MULTIPLIER * snapshot.atr_value
    tp_distance = calculate_price_distance_for_target_profit(
        SHORT_TERM_TP_TARGET_USD, volume, constraints.tick_value, constraints.tick_size
    )
    if decision.direction == "BUY":
        stop_loss = snapshot.current_price - stop_distance
        take_profit = snapshot.current_price + tp_distance
    else:
        stop_loss = snapshot.current_price + stop_distance
        take_profit = snapshot.current_price - tp_distance

    return decision, stop_loss, take_profit, volume


def _update_short_term_peak_price(
    position: BrokerPosition | None, previous_peak_price: float | None
) -> float | None:
    """Track the best favorable price reached while the short-term
    mode's own `position` is open. Resets to `None` whenever flat;
    initializes fresh the first cycle a position is seen open (a `None`
    `previous_peak_price` while `position is not None` unambiguously
    means "just started tracking", since this is always reset to `None`
    the moment a position closes — short-term mode never holds more than
    one position at a time, so there's no scenario where a *different*
    position could be mistaken for a continuing one); otherwise extends
    the running max (`BUY`) / min (`SELL`) with `price_current`. Pure —
    `run_bar_close_cycle()`'s caller threads the return value back in as
    next cycle's `previous_peak_price`.
    """
    if position is None:
        return None
    if previous_peak_price is None:
        return position.price_current
    if position.side == "BUY":
        return max(previous_peak_price, position.price_current)
    return min(previous_peak_price, position.price_current)


def decide_short_term_profit_lock(
    position: BrokerPosition,
    current_price: float,
    atr_value: float,
    peak_price: float,
    *,
    retracement_atr_multiplier: float = SHORT_TERM_PROFIT_LOCK_RETRACEMENT_ATR_MULTIPLIER,
) -> bool:
    """`True` iff `position` is still in profit but price has retraced
    from `peak_price` (the best favorable price reached so far) by at
    least `retracement_atr_multiplier * atr_value` — locks in whatever
    profit remains rather than riding the retracement back down to the
    fixed SL, or waiting for a fixed TP that may now be further away than
    the peak ever got.
    """
    if position.side == "BUY":
        in_profit = current_price > position.price_open
        retracement = peak_price - current_price
    else:
        in_profit = current_price < position.price_open
        retracement = current_price - peak_price
    if not in_profit:
        return False
    return retracement >= retracement_atr_multiplier * atr_value


def _evaluate_short_term_position_management(
    short_term_position: BrokerPosition | None,
    previous_peak_price: float | None,
    atr_value: float,
) -> tuple[float | None, OrderActionPayload | None]:
    """The short-term mode's "already open" complement to
    `_evaluate_short_term_entry()`'s "flat" case — independent of the
    regular mode's own position management, runs whenever a short-term
    position is open regardless of what the regular mode is doing this
    cycle. Returns `(new_peak_price, close_action)`: `close_action` is
    non-`None` only when `decide_short_term_profit_lock()` fires.
    """
    new_peak_price = _update_short_term_peak_price(short_term_position, previous_peak_price)
    if short_term_position is None or new_peak_price is None:
        return new_peak_price, None

    if decide_short_term_profit_lock(
        short_term_position, short_term_position.price_current, atr_value, new_peak_price
    ):
        action = build_short_term_liquidation_action(
            ticket=short_term_position.ticket,
            symbol=short_term_position.symbol,
            magic_number=short_term_position.magic,
            volume=short_term_position.volume,
            comment=SHORT_TERM_PROFIT_LOCK_COMMENT,
        )
        return new_peak_price, action
    return new_peak_price, None


def run_bar_close_cycle(
    context: FSMContext,
    snapshot: MarketSnapshot,
    baselines: EquityBaselines,
    constraints: SymbolConstraints,
    feature_flags: FeatureFlagManager,
    *,
    cycle_duration_seconds: float,
    manual_reset_confirmed: bool = False,
    trading_mode: str = TRADING_MODE_WAIT_FOR_CONDITIONS,
    short_term_position: BrokerPosition | None = None,
    trailing_atr_multiplier: float = TRAILING_ATR_MULTIPLIER,
    short_term_peak_price: float | None = None,
) -> BarCloseCycleResult:
    """Pure decision function for a single M5 bar-close cycle: no I/O,
    entirely deterministic given its inputs. `main()` fetches the inputs
    (broker/storage calls) and executes the resulting
    `position_actions`/entry decision against the broker, then persists
    the outcome and constructs the next cycle's `FSMContext`.

    `manual_reset_confirmed` is the sole channel for a human to clear a
    locked `drawdown_state` (`risk/drawdown_fsm.py`'s
    `DrawdownEvent.MANUAL_RESET_CONFIRMED`) — always `False` today, since
    `main()` has no live control channel (API/CLI/admin signal) for an
    operator to actually set it yet; this is an open gap, see
    `docs/ARCHITECTURE_SUMMARY.md` §5.

    `trading_mode`/`short_term_position` drive the short-term (scalp)
    mode's entry evaluation (`_evaluate_short_term_entry()`), independent
    of — and orthogonal to — the regular mode's `context.position is None`
    branch below: either mode can hold a position while the other is
    flat. `short_term_peak_price` drives that same mode's *position*
    management (`_evaluate_short_term_position_management()`, the profit-
    peak lock) — the caller persists the returned
    `BarCloseCycleResult.short_term_peak_price` and passes it back in as
    this same argument next cycle. All new parameters default to values
    that make this function behave exactly as before short-term mode
    existed.
    """
    processing = evaluate_processing_time(cycle_duration_seconds)
    if processing.exceeded_cap:
        logger.warning(
            "Bar-close cycle exceeded the 200ms processing cap: %.1fms", processing.duration_ms
        )

    updated_context, classification = _evaluate_drawdown_transition(
        context, snapshot, baselines, manual_reset_confirmed=manual_reset_confirmed
    )
    new_drawdown_state = updated_context.drawdown_state
    no_action = BarCloseCycleResult(
        context=updated_context,
        drawdown=classification,
        processing=processing,
        entry_decision=None,
        entry_stop_loss=None,
        entry_volume=None,
        position_actions=(),
    )

    hard_lock_result = _handle_hard_lock_response(
        context, updated_context, classification, processing, feature_flags, short_term_position
    )
    if hard_lock_result is not None:
        return hard_lock_result

    if blocks_position_management(new_drawdown_state):
        # MANUAL_RESET_REQUIRED (carried over from a prior cycle's
        # HARD_LOCK): no operations at all until a human confirms a reset,
        # for either mode.
        return no_action

    news_locked = is_trade_entry_locked(snapshot.now_utc, snapshot.news_events)

    (
        short_term_entry_decision,
        short_term_entry_stop_loss,
        short_term_entry_take_profit,
        short_term_entry_volume,
    ) = _evaluate_short_term_entry(
        trading_mode, short_term_position, new_drawdown_state, news_locked, snapshot, constraints
    )

    # Independent of the flat-vs-in-position branch below: a short-term
    # position (if any) is managed every cycle regardless of what the
    # regular mode does this cycle — mutually exclusive with the entry
    # evaluation above by construction (one requires flat, the other
    # requires open).
    new_short_term_peak_price, short_term_close_action = _evaluate_short_term_position_management(
        short_term_position, short_term_peak_price, snapshot.atr_value
    )
    short_term_actions: tuple[OrderActionPayload, ...] = (
        (short_term_close_action,) if short_term_close_action is not None else ()
    )

    if context.position is None:
        if blocks_new_entries(new_drawdown_state):
            # SOFT_LOCK, flat: nothing to manage and no new *regular*
            # entries allowed (short-term evaluation above already
            # applied this same gate independently).
            return BarCloseCycleResult(
                context=updated_context,
                drawdown=classification,
                processing=processing,
                entry_decision=None,
                entry_stop_loss=None,
                entry_volume=None,
                position_actions=short_term_actions,
                short_term_entry_decision=short_term_entry_decision,
                short_term_entry_stop_loss=short_term_entry_stop_loss,
                short_term_entry_take_profit=short_term_entry_take_profit,
                short_term_entry_volume=short_term_entry_volume,
                short_term_peak_price=new_short_term_peak_price,
            )

        entry_decision = decide_entry_signal(
            snapshot.trend, snapshot.breakout, snapshot.pullback, snapshot.wick_fill, news_locked
        )
        if entry_decision.direction == "NONE" or snapshot.current_price is None:
            return BarCloseCycleResult(
                context=updated_context,
                drawdown=classification,
                processing=processing,
                entry_decision=entry_decision,
                entry_stop_loss=None,
                entry_volume=None,
                position_actions=short_term_actions,
                short_term_entry_decision=short_term_entry_decision,
                short_term_entry_stop_loss=short_term_entry_stop_loss,
                short_term_entry_take_profit=short_term_entry_take_profit,
                short_term_entry_volume=short_term_entry_volume,
                short_term_peak_price=new_short_term_peak_price,
            )

        stop_distance = ENTRY_ATR_STOP_MULTIPLIER * snapshot.atr_value
        entry_stop_loss = (
            snapshot.current_price - stop_distance
            if entry_decision.direction == "BUY"
            else snapshot.current_price + stop_distance
        )
        entry_volume = calculate_compounded_lot_size(
            snapshot.account_state.equity,
            constraints.volume_min,
            constraints.volume_max,
            constraints.volume_step,
        )
        return BarCloseCycleResult(
            context=updated_context,
            drawdown=classification,
            processing=processing,
            entry_decision=entry_decision,
            entry_stop_loss=entry_stop_loss,
            entry_volume=entry_volume,
            position_actions=short_term_actions,
            short_term_entry_decision=short_term_entry_decision,
            short_term_entry_stop_loss=short_term_entry_stop_loss,
            short_term_entry_take_profit=short_term_entry_take_profit,
            short_term_entry_volume=short_term_entry_volume,
            short_term_peak_price=new_short_term_peak_price,
        )

    if snapshot.current_price is None:
        return BarCloseCycleResult(
            context=updated_context,
            drawdown=classification,
            processing=processing,
            entry_decision=None,
            entry_stop_loss=None,
            entry_volume=None,
            position_actions=short_term_actions,
            short_term_entry_decision=short_term_entry_decision,
            short_term_entry_stop_loss=short_term_entry_stop_loss,
            short_term_entry_take_profit=short_term_entry_take_profit,
            short_term_entry_volume=short_term_entry_volume,
            short_term_peak_price=new_short_term_peak_price,
        )

    # SOFT_LOCK reaches here too (blocks_new_entries is True for it, but
    # that only gated the flat/no-position branch above): position
    # management keeps running under SOFT_LOCK by design
    # (docs/PRODUCTION_SPEC.md §6).
    actions = evaluate_partial_close_and_breakeven(
        context.position,
        snapshot.current_price,
        snapshot.atr_value,
        constraints.volume_min,
        constraints.volume_max,
        constraints.volume_step,
    )
    if not actions:
        trailing_action = calculate_trailing_stop(
            context.position,
            snapshot.current_price,
            snapshot.atr_value,
            trailing_atr_multiplier=trailing_atr_multiplier,
        )
        if trailing_action is not None:
            actions = [trailing_action]

    return BarCloseCycleResult(
        context=updated_context,
        drawdown=classification,
        processing=processing,
        entry_decision=None,
        entry_stop_loss=None,
        entry_volume=None,
        position_actions=tuple(actions) + short_term_actions,
        short_term_entry_decision=short_term_entry_decision,
        short_term_entry_stop_loss=short_term_entry_stop_loss,
        short_term_entry_take_profit=short_term_entry_take_profit,
        short_term_entry_volume=short_term_entry_volume,
        short_term_peak_price=new_short_term_peak_price,
    )


# ---------------------------------------------------------------------------
# Impure I/O layer: the live loop (bootstrap itself now lives in
# container.py's ApplicationContainer, docs/PRODUCTION_SPEC.md's DI
# composition root). Reviewed for correctness but not executed against a
# live/demo account in this environment — see docs/ARCHITECTURE_SUMMARY.md
# "Before your first live/demo run".
# ---------------------------------------------------------------------------

_SubmitResult = TypeVar("_SubmitResult")


def submit_with_pre_flight_ledger(
    state_manager: StateManager,
    submit: Callable[[str], _SubmitResult],
    metadata: dict[str, Any],
    terminal_state: OrderLifecycleState,
) -> tuple[str, _SubmitResult]:
    """Wrap one broker submission with the pre-flight idempotency write
    `docs/PRODUCTION_SPEC.md` §4 requires: a fresh `client_order_id`
    (UUIDv4) is written to the Event Store as `REQUESTED` — atomically,
    via `StateManager.record_order_event()` — *before* `submit` (the
    payload's actual route to the MT5 gateway) is called at all.

    On success, records `SENT` then `terminal_state` (`FILLED` for a new
    market order or a partial-close deal, `MODIFIED` for an SLTP change).
    On `BrokerOrderRejectedError`, records `REJECTED` (capturing the error
    in metadata) and re-raises unchanged — this wrapper only adds an audit
    trail around the call, it does not change what the caller observes on
    failure, and it does not itself retry (`docs/PRODUCTION_SPEC.md` §7's
    backoff-driven retry loop is a separate, later sub-phase; see
    `docs/ARCHITECTURE_SUMMARY.md` §5 for what a future retry loop would
    still need to call — `execution.validation.check_duplicate_order_before_retry()`
    plus `MT5Gateway.is_ticket_still_open()` — before resubmitting).

    Returns `(client_order_id, submit`'s return value`)` so callers that
    need the generated id (e.g. to persist a `TradeLedgerEntry`) don't have
    to generate a second one.
    """
    client_order_id = str(uuid.uuid4())
    state_manager.record_order_event(client_order_id, OrderLifecycleState.REQUESTED, metadata)
    try:
        result = submit(client_order_id)
    except BrokerOrderRejectedError as exc:
        state_manager.record_order_event(
            client_order_id, OrderLifecycleState.REJECTED, {**metadata, "error": str(exc)}
        )
        raise
    state_manager.record_order_event(client_order_id, OrderLifecycleState.SENT, metadata)
    state_manager.record_order_event(client_order_id, terminal_state, metadata)
    return client_order_id, result


def _fetch_market_snapshot(
    gateway: MT5Gateway,
    magic_number: int,
    news_events: list[EconomicEvent],
    *,
    adx_trend_threshold: float = ADX_TREND_THRESHOLD,
) -> MarketSnapshot:
    """Fetch bars/account state and evaluate the trend filter and entry
    triggers on the H1 timeframe (the finest granularity trend_filter
    already evaluates ADX on).

    `adx_trend_threshold` defaults to the module constant but is meant to
    be overridden with the self-learning optimizer's latest applied value
    (`optimizer.self_learning.get_effective_parameter_value()`), resolved
    fresh by `main()` every cycle.
    """
    d1_bars = gateway.get_bars(TIMEFRAME_D1, D1_BAR_COUNT)
    h4_bars = gateway.get_bars(TIMEFRAME_H4, H4_BAR_COUNT)
    h1_bars = gateway.get_bars(TIMEFRAME_H1, H1_BAR_COUNT)

    trend = evaluate_master_trend(
        d1_bars.close,
        h4_bars.high,
        h4_bars.low,
        h4_bars.close,
        h1_bars.high,
        h1_bars.low,
        h1_bars.close,
        adx_trend_threshold=adx_trend_threshold,
    )
    atr_value = float(atr(h1_bars.high, h1_bars.low, h1_bars.close, 14)[-1])
    breakout = detect_breakout(
        h1_bars.high,
        h1_bars.low,
        h1_bars.close,
        h1_bars.tick_volume,
        gateway.symbol_spec.point,
    )
    if trend.direction == "BULLISH":
        trend_side: Direction = "BUY"
    elif trend.direction == "BEARISH":
        trend_side = "SELL"
    else:
        trend_side = "NONE"
    # reference level = the H1 EMA trend_filter itself evaluates alignment
    # against (H1_EMA_PERIOD) — NOT h1_bars.close, which would make the
    # pullback check compare the latest close to itself and never fire.
    h1_ema = ema(h1_bars.close, H1_EMA_PERIOD)
    pullback = detect_pullback(
        h1_bars.high,
        h1_bars.low,
        h1_bars.close,
        h1_ema,
        trend_side,
    )
    wick_fill = analyze_wick_fill(h1_bars.open, h1_bars.high, h1_bars.low, h1_bars.close)
    account_state = gateway.get_account_state()
    # Live tick, NOT h1_bars.close[-1]: get_bars() now returns closed bars
    # only, so the latest bar close can be up to an hour old — stale for
    # entry-stop/trailing math.
    tick_price = gateway.get_current_price()

    # Short-term mode's own relaxed trend + a SEPARATE pullback computed
    # against ITS direction (not `trend_side` above) — they can legitimately
    # disagree, since relaxing the D1+H4 requirement is the whole point.
    short_term_trend = evaluate_short_term_trend(h1_bars.high, h1_bars.low, h1_bars.close)
    if short_term_trend.direction == "BULLISH":
        short_term_side: Direction = "BUY"
    elif short_term_trend.direction == "BEARISH":
        short_term_side = "SELL"
    else:
        short_term_side = "NONE"
    short_term_pullback = detect_pullback(
        h1_bars.high,
        h1_bars.low,
        h1_bars.close,
        h1_ema,
        short_term_side,
    )

    return MarketSnapshot(
        now_utc=datetime.now(timezone.utc),
        account_state=account_state,
        current_price=tick_price,
        atr_value=atr_value,
        trend=trend,
        breakout=breakout,
        pullback=pullback,
        wick_fill=wick_fill,
        news_events=news_events,
        short_term_trend=short_term_trend,
        short_term_pullback=short_term_pullback,
    )


def _seed_initial_fsm_context(container: ApplicationContainer) -> FSMContext:
    """Disaster Recovery's second half (`docs/PRODUCTION_SPEC.md` §7):
    seeds `main()`'s starting `FSMContext` directly from the broker's live
    open positions (the authoritative source) rather than a possibly-stale
    local snapshot. `container.initial_drawdown_state` already reflects
    whether `ApplicationContainer.build()`'s reconciliation found a
    divergence requiring manual review.

    If exactly one open position exists under this gateway's magic
    number, resumes `IN_POSITION` with it — `partial_closed`/
    `breakeven_set` default to `False` since neither is derivable from
    broker-reported fields alone (a known limitation: resuming
    mid-position after a crash may repeat an already-completed
    partial-close/breakeven step; see `docs/ARCHITECTURE_SUMMARY.md` §5).
    Zero or more than one open position starts flat (`IDLE`) — "more than
    one" is anomalous for this single-instrument system and would already
    be caught by the disaster-recovery reconciliation's own divergence
    check in the ordinary case.
    """
    open_positions = container.gateway.get_open_positions_by_magic()
    if len(open_positions) == 1:
        broker_position = open_positions[0]
        return FSMContext(
            state=TradingState.IN_POSITION,
            position=PositionState(
                ticket=broker_position.ticket,
                symbol=broker_position.symbol,
                side=broker_position.side,  # type: ignore[arg-type]
                volume=broker_position.volume,
                entry_price=broker_position.price_open,
                stop_loss=broker_position.stop_loss,
                magic_number=broker_position.magic,
                partial_closed=False,
                breakeven_set=False,
            ),
            drawdown_state=container.initial_drawdown_state,
            drawdown_reason=None,
        )
    return FSMContext(
        state=TradingState.IDLE,
        position=None,
        drawdown_state=container.initial_drawdown_state,
        drawdown_reason=None,
    )


def _record_short_term_close(container: ApplicationContainer, closed_ticket: int) -> None:
    """Reconcile a short-term position's real outcome into `trade_ledger`
    once `_fetch_short_term_position()` detects it's no longer open.

    Looks up the closing deal (`MT5Gateway.get_closing_deal()`) and the
    still-`OPEN` ledger row this ticket was recorded under
    (`StateManager.get_open_trade_by_ticket()`); if both are found,
    re-records the same `client_order_id` with `status="CLOSED"` and the
    real `close_price`/`profit` (`record_trade()`'s upsert-by-id turns the
    existing row `CLOSED` in place). Silently does nothing if either
    lookup comes back empty — MT5's 24h deal-history lookback comfortably
    covers this loop's 5-minute detection cadence, so a miss here would
    indicate the position was never recorded in the first place (e.g. it
    predates this feature) rather than a real error to raise on.
    """
    closed_deal = container.gateway.get_closing_deal(closed_ticket)
    ledger_entry = container.state_manager.get_open_trade_by_ticket(closed_ticket)
    if closed_deal is None or ledger_entry is None:
        return
    container.state_manager.record_trade(
        TradeLedgerEntry(
            client_order_id=ledger_entry.client_order_id,
            symbol=ledger_entry.symbol,
            side=ledger_entry.side,
            volume_lots=ledger_entry.volume_lots,
            status="CLOSED",
            opened_at_utc=ledger_entry.opened_at_utc,
            open_price=ledger_entry.open_price,
            close_price=closed_deal.close_price,
            stop_loss_price=ledger_entry.stop_loss_price,
            take_profit_price=ledger_entry.take_profit_price,
            profit=closed_deal.profit,
            magic_number=ledger_entry.magic_number,
            broker_ticket=ledger_entry.broker_ticket,
            closed_at_utc=closed_deal.closed_at_utc.isoformat(),
        )
    )


def _fetch_short_term_position(
    container: ApplicationContainer, previous_short_term_position: BrokerPosition | None
) -> BrokerPosition | None:
    """Query the broker for the short-term mode's currently open position
    (if any), under its own distinct magic number — `None` when
    `trading_mode` doesn't enable short-term mode at all. When a
    previously-open short-term position has disappeared (closed by MT5's
    own SL/TP, never by `main()` itself — see this module's docstring),
    logs it and reconciles the real outcome into `trade_ledger` via
    `_record_short_term_close()`.
    """
    if container.config.trading_mode not in (TRADING_MODE_SHORT_TERM, TRADING_MODE_BOTH):
        return None
    positions = container.gateway.get_open_positions_by_magic(
        container.config.short_term_magic_number
    )
    short_term_position = positions[0] if positions else None
    if previous_short_term_position is not None and short_term_position is None:
        logger.info(
            "Short-term position closed (SL/TP hit, broker-managed): ticket=%s",
            previous_short_term_position.ticket,
        )
        _record_short_term_close(container, previous_short_term_position.ticket)
    return short_term_position


def _submit_short_term_entry_if_any(
    container: ApplicationContainer, result: BarCloseCycleResult
) -> None:
    """Submit `result`'s short-term entry (if any) via the same pre-flight
    idempotency wrapper the regular entry path uses, then record the fill
    as an `OPEN` `trade_ledger` row (`broker_ticket` set, so
    `_record_short_term_close()` can find and close it later) — the same
    OPEN-then-CLOSED lifecycle the regular entry path already has, closing
    the previously-flagged gap where short-term fills were invisible to
    `optimizer/self_learning.py`'s analytics. Deliberately does NOT touch
    `FSMContext` — the short-term position is never tracked there (see
    this module's docstring and `_evaluate_short_term_entry()`'s).
    """
    if (
        result.short_term_entry_decision is None
        or result.short_term_entry_decision.direction == "NONE"
        or result.short_term_entry_stop_loss is None
        or result.short_term_entry_take_profit is None
        or result.short_term_entry_volume is None
    ):
        return

    side = result.short_term_entry_decision.direction
    stop_loss = result.short_term_entry_stop_loss
    take_profit = result.short_term_entry_take_profit
    volume = result.short_term_entry_volume
    metadata: dict[str, Any] = {
        "symbol": container.gateway.symbol_spec.name,
        "side": side,
        "volume": volume,
        "magic_number": container.config.short_term_magic_number,
    }

    def _submit(
        cid: str,
        *,
        bound_side: Literal["BUY", "SELL"] = side,
        bound_volume: float = volume,
        bound_stop_loss: float = stop_loss,
        bound_take_profit: float = take_profit,
    ) -> BrokerPosition:
        return container.gateway.submit_market_order(
            side=bound_side,
            volume=bound_volume,
            stop_loss=bound_stop_loss,
            take_profit=bound_take_profit,
            comment=cid,
            magic_number=container.config.short_term_magic_number,
        )

    client_order_id, broker_position = submit_with_pre_flight_ledger(
        container.state_manager, _submit, metadata, OrderLifecycleState.FILLED
    )
    container.state_manager.record_trade(
        TradeLedgerEntry(
            client_order_id=client_order_id,
            symbol=broker_position.symbol,
            side=broker_position.side,
            volume_lots=broker_position.volume,
            status="OPEN",
            opened_at_utc=broker_position.opened_at_utc.isoformat(),
            open_price=broker_position.price_open,
            stop_loss_price=broker_position.stop_loss,
            take_profit_price=broker_position.take_profit,
            magic_number=broker_position.magic,
            broker_ticket=broker_position.ticket,
        )
    )


def main() -> None:
    """Entry point: bootstrap, then loop forever, acting once per new M5
    bar close. Not executed in this environment (no live MT5 credentials);
    see docs/ARCHITECTURE_SUMMARY.md before running this against a real
    account.
    """
    logging.basicConfig(level=logging.INFO)
    container = ApplicationContainer.build()

    context = _seed_initial_fsm_context(container)
    baselines: EquityBaselines | None = None
    baseline_epoch: BaselineEpoch | None = None
    previous_short_term_position: BrokerPosition | None = None
    short_term_peak_price: float | None = None
    constraints = SymbolConstraints(
        point=container.gateway.symbol_spec.point,
        volume_min=container.gateway.symbol_spec.volume_min,
        volume_max=container.gateway.symbol_spec.volume_max,
        volume_step=container.gateway.symbol_spec.volume_step,
        magic_number=container.config.strategy_magic_number,
        tick_value=container.gateway.symbol_spec.tick_value,
        tick_size=container.gateway.symbol_spec.tick_size,
    )
    logger.info(
        "Entering bar-close loop: state=%s position=%s drawdown_state=%s",
        context.state.value,
        context.position.ticket if context.position is not None else None,
        context.drawdown_state.value,
    )

    while True:
        if is_weekend_market_closed(datetime.now(timezone.utc)):
            logger.info(
                "Weekend market closure (Fri %02d:00 UTC - Sun %02d:00 UTC): "
                "skipping cycle, rechecking in %.0fs.",
                WEEKEND_CLOSE_HOUR_UTC,
                WEEKEND_REOPEN_HOUR_UTC,
                WEEKEND_RECHECK_SECONDS,
            )
            time.sleep(WEEKEND_RECHECK_SECONDS)
            continue

        wait_seconds = seconds_until_next_bar_close(datetime.now(timezone.utc))
        logger.info("Waiting %.1fs for next M5 bar close.", wait_seconds)
        time.sleep(wait_seconds)

        started = time.perf_counter()
        account_state = container.gateway.get_account_state()
        short_term_position = _fetch_short_term_position(container, previous_short_term_position)
        previous_short_term_position = short_term_position
        server_now = container.clock_provider.get_server_time(container.gateway.symbol_spec.name)
        if baselines is None or baseline_epoch is None:
            # First cycle: seed all three baselines from current equity,
            # stamped against broker server time.
            baselines, baseline_epoch = seed_equity_baselines(account_state.equity, server_now)
        else:
            # Every subsequent cycle: roll forward whichever tier(s) have
            # crossed their UTC-day/ISO-week/calendar-month boundary since
            # they were last set — a no-op unless the period actually
            # changed (docs/ARCHITECTURE_SUMMARY.md §5).
            baselines, baseline_epoch = roll_equity_baselines(
                baselines, baseline_epoch, account_state.equity, server_now
            )

        # Resolve the self-learning optimizer's latest applied values fresh
        # every cycle (not just at boot) — a Saturday shift takes effect
        # starting the very next cycle, no restart needed.
        effective_adx_threshold = get_effective_parameter_value(
            container.state_manager, "ADX_TREND_THRESHOLD", ADX_TREND_THRESHOLD
        )
        effective_trailing_multiplier = get_effective_parameter_value(
            container.state_manager, "TRAILING_ATR_MULTIPLIER", TRAILING_ATR_MULTIPLIER
        )

        snapshot = _fetch_market_snapshot(
            container.gateway,
            constraints.magic_number,
            [],
            adx_trend_threshold=effective_adx_threshold,
        )
        ended = time.perf_counter()

        result = run_bar_close_cycle(
            context,
            snapshot,
            baselines,
            constraints,
            container.feature_flags,
            cycle_duration_seconds=ended - started,
            trading_mode=container.config.trading_mode,
            short_term_position=short_term_position,
            trailing_atr_multiplier=effective_trailing_multiplier,
            short_term_peak_price=short_term_peak_price,
        )
        context = result.context
        short_term_peak_price = result.short_term_peak_price

        for action in result.position_actions:
            action_metadata: dict[str, Any] = {
                "symbol": action.symbol,
                "action": action.action,
                "position_ticket": action.position_ticket,
                "comment": action.comment,
            }
            action_terminal_state = (
                OrderLifecycleState.MODIFIED
                if action.action == "TRADE_ACTION_SLTP"
                else OrderLifecycleState.FILLED
            )

            def _submit_action(
                _client_order_id: str, *, bound_action: OrderActionPayload = action
            ) -> None:
                container.gateway.submit_position_action(bound_action)

            submit_with_pre_flight_ledger(
                container.state_manager,
                _submit_action,
                action_metadata,
                action_terminal_state,
            )

            if action.comment == EMERGENCY_LIQUIDATION_COMMENT:
                # A full-volume liquidation deal, unlike a partial close,
                # leaves nothing open — clear the tracked position rather
                # than waiting for the next cycle's stale management logic
                # to act on a ticket that no longer exists.
                context = FSMContext(
                    state=TradingState.IDLE,
                    position=None,
                    drawdown_state=context.drawdown_state,
                    drawdown_reason=context.drawdown_reason,
                )

        if (
            result.entry_decision is not None
            and result.entry_decision.direction != "NONE"
            and result.entry_stop_loss is not None
            and result.entry_volume is not None
        ):
            # Narrowed into plain locals (rather than read from `result.*`
            # inside `_submit_entry` below) since mypy's Optional/Literal
            # narrowing from the `if` above does not propagate into a
            # nested function body.
            entry_side = result.entry_decision.direction
            entry_stop_loss = result.entry_stop_loss
            entry_volume = result.entry_volume
            entry_metadata: dict[str, Any] = {
                "symbol": container.gateway.symbol_spec.name,
                "side": entry_side,
                "volume": entry_volume,
            }

            def _submit_entry(
                cid: str,
                *,
                side: Literal["BUY", "SELL"] = entry_side,
                volume: float = entry_volume,
                stop_loss: float = entry_stop_loss,
            ) -> BrokerPosition:
                return container.gateway.submit_market_order(
                    side=side,
                    volume=volume,
                    stop_loss=stop_loss,
                    take_profit=None,
                    comment=cid,
                )

            client_order_id, broker_position = submit_with_pre_flight_ledger(
                container.state_manager,
                _submit_entry,
                entry_metadata,
                OrderLifecycleState.FILLED,
            )
            container.state_manager.record_trade(
                TradeLedgerEntry(
                    client_order_id=client_order_id,
                    symbol=broker_position.symbol,
                    side=broker_position.side,
                    volume_lots=broker_position.volume,
                    status="OPEN",
                    opened_at_utc=broker_position.opened_at_utc.isoformat(),
                    open_price=broker_position.price_open,
                    stop_loss_price=broker_position.stop_loss,
                    magic_number=broker_position.magic,
                    broker_ticket=broker_position.ticket,
                )
            )
            context = FSMContext(
                state=TradingState.IN_POSITION,
                position=PositionState(
                    ticket=broker_position.ticket,
                    symbol=broker_position.symbol,
                    side=broker_position.side,  # type: ignore[arg-type]
                    volume=broker_position.volume,
                    entry_price=broker_position.price_open,
                    stop_loss=broker_position.stop_loss,
                    magic_number=broker_position.magic,
                    partial_closed=False,
                    breakeven_set=False,
                ),
                drawdown_state=context.drawdown_state,
                drawdown_reason=context.drawdown_reason,
            )

        _submit_short_term_entry_if_any(container, result)


if __name__ == "__main__":
    main()
