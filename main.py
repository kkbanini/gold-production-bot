"""Master FSM orchestration loop: connects every module built in Phases
1-9 into a single event-driven process that acts once per new M5 bar
close (docs/RUNBOOK.md §1's boot sequence, ADR-0001's event-driven
principle applied without a full EventBus/`core`, which no phase built).

This module owns the trading state machine (TradingState) and the two
mechanisms this phase specifically adds: the 200ms processing-cap metric
and the 5%/10%/20% daily/weekly/monthly drawdown hard locks. It does NOT
implement a pre-trade risk gate or slippage guard (RQ-009/RQ-010) — those
remain open gaps, see docs/TRACEABILITY_MATRIX.md and
docs/ARCHITECTURE_SUMMARY.md.

`run_bar_close_cycle()` is a pure decision function: no I/O, fully
deterministic given its inputs, and the unit of testing. `bootstrap_system()`
and `main()` are the impure I/O layer that fetches real data and executes
decisions against the broker — reviewed for correctness but not executed
against a live/demo account in this environment (no real MT5 credentials
exist here; see docs/ARCHITECTURE_SUMMARY.md "Before your first live/demo
run").
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from broker.mt5_gateway import (
    TIMEFRAME_D1,
    TIMEFRAME_H1,
    TIMEFRAME_H4,
    AccountState,
    MT5Gateway,
)
from config.config_manager import ConfigManager
from execution.position_manager import (
    BASE_TP_ATR_MULTIPLIER,
    OrderActionPayload,
    PositionState,
    calculate_trailing_stop,
    evaluate_partial_close_and_breakeven,
)
from indicators.math_engine import atr, ema
from news.news_engine import EconomicEvent, is_trade_entry_locked
from risk.risk_manager import calculate_compounded_lot_size
from storage.state_manager import StateManager, TradeLedgerEntry
from strategy.execution_triggers import (
    BreakoutSignal,
    PullbackSignal,
    WickFillResult,
    analyze_wick_fill,
    detect_breakout,
    detect_pullback,
)
from strategy.trend_filter import H1_EMA_PERIOD, TrendAlignment, evaluate_master_trend

logger = logging.getLogger(__name__)

Direction = Literal["BUY", "SELL", "NONE"]

PROCESSING_CAP_MS = 200.0
BAR_CLOSE_TIMEFRAME_MINUTES = 5

DAILY_DRAWDOWN_LIMIT = 0.05
WEEKLY_DRAWDOWN_LIMIT = 0.10
MONTHLY_DRAWDOWN_LIMIT = 0.20

# Distance (in ATR multiples) from the current price to the initial
# stop-loss on a new entry. Mirrors position_manager's Base_TP multiplier
# for symmetry; not independently specified anywhere, flagged for review.
ENTRY_ATR_STOP_MULTIPLIER = BASE_TP_ATR_MULTIPLIER

D1_BAR_COUNT = 220
H4_BAR_COUNT = 70
H1_BAR_COUNT = 67
M5_BAR_COUNT = 40


class TradingState(str, Enum):
    """Master FSM states for the orchestration loop."""

    INITIALIZING = "INITIALIZING"
    IDLE = "IDLE"
    IN_POSITION = "IN_POSITION"
    HALTED = "HALTED"


class DrawdownBreaker(str, Enum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


@dataclass(frozen=True, slots=True)
class EquityBaselines:
    """Reference equity captured at the start of the current UTC
    day/ISO week/calendar month, against which drawdown is measured."""

    daily_start_equity: float
    weekly_start_equity: float
    monthly_start_equity: float


@dataclass(frozen=True, slots=True)
class DrawdownCheckResult:
    daily_drawdown_pct: float
    weekly_drawdown_pct: float
    monthly_drawdown_pct: float
    breached: tuple[DrawdownBreaker, ...]

    @property
    def is_halted(self) -> bool:
        return len(self.breached) > 0


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
    bar-close cycle."""

    state: TradingState
    position: PositionState | None
    halt_reason: str | None


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Everything about the current market/account `run_bar_close_cycle`
    needs, gathered by the (impure) caller before invoking it."""

    now_utc: datetime
    account_state: AccountState
    current_price: float | None
    atr_value: float
    trend: TrendAlignment
    breakout: BreakoutSignal
    pullback: PullbackSignal
    wick_fill: WickFillResult
    news_events: list[EconomicEvent]


@dataclass(frozen=True, slots=True)
class SymbolConstraints:
    point: float
    volume_min: float
    volume_max: float
    volume_step: float
    magic_number: int


@dataclass(frozen=True, slots=True)
class BarCloseCycleResult:
    """Everything one call to `run_bar_close_cycle` decided. `context` only
    reflects a drawdown-triggered HALT transition — entry/position-
    management transitions happen in `main()` after the broker confirms
    the corresponding action succeeded, which this pure function cannot
    know."""

    context: FSMContext
    drawdown: DrawdownCheckResult
    processing: ProcessingCapResult
    entry_decision: EntryDecision | None
    entry_stop_loss: float | None
    entry_volume: float | None
    position_actions: tuple[OrderActionPayload, ...]


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


def check_drawdown_breach(current_equity: float, baselines: EquityBaselines) -> DrawdownCheckResult:
    """Compute daily/weekly/monthly drawdown against the supplied
    baselines and flag any breaker exceeding its hard-lock threshold
    (5%/10%/20% respectively).

    A breach halts new entries (`FSMContext.state -> HALTED`) but never
    force-closes an existing position, matching `docs/RUNBOOK.md`'s
    established `CRITICAL`-severity posture (autonomous de-risk means no
    new risk added, not an automatic flatten).
    """
    for label, baseline in (
        ("daily", baselines.daily_start_equity),
        ("weekly", baselines.weekly_start_equity),
        ("monthly", baselines.monthly_start_equity),
    ):
        if baseline <= 0:
            raise ValueError(f"{label}_start_equity must be > 0, got {baseline}")
    if current_equity < 0:
        raise ValueError(f"current_equity must be >= 0, got {current_equity}")

    daily_dd = max(
        0.0, (baselines.daily_start_equity - current_equity) / baselines.daily_start_equity
    )
    weekly_dd = max(
        0.0, (baselines.weekly_start_equity - current_equity) / baselines.weekly_start_equity
    )
    monthly_dd = max(
        0.0, (baselines.monthly_start_equity - current_equity) / baselines.monthly_start_equity
    )

    breached: list[DrawdownBreaker] = []
    if daily_dd >= DAILY_DRAWDOWN_LIMIT:
        breached.append(DrawdownBreaker.DAILY)
    if weekly_dd >= WEEKLY_DRAWDOWN_LIMIT:
        breached.append(DrawdownBreaker.WEEKLY)
    if monthly_dd >= MONTHLY_DRAWDOWN_LIMIT:
        breached.append(DrawdownBreaker.MONTHLY)

    return DrawdownCheckResult(
        daily_drawdown_pct=daily_dd,
        weekly_drawdown_pct=weekly_dd,
        monthly_drawdown_pct=monthly_dd,
        breached=tuple(breached),
    )


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

    if breakout.is_valid and breakout.direction == trend_direction:
        return EntryDecision(trend_direction, f"breakout confirms {trend_direction} trend")
    if pullback.is_valid and pullback.direction == trend_direction:
        return EntryDecision(trend_direction, f"pullback confirms {trend_direction} trend")
    if wick_fill.is_significant and wick_fill.rejection == trend_direction:
        return EntryDecision(
            trend_direction, f"wick-fill rejection confirms {trend_direction} trend"
        )
    return EntryDecision("NONE", "no entry trigger agrees with the confirmed trend direction")


def run_bar_close_cycle(
    context: FSMContext,
    snapshot: MarketSnapshot,
    baselines: EquityBaselines,
    constraints: SymbolConstraints,
    *,
    cycle_duration_seconds: float,
) -> BarCloseCycleResult:
    """Pure decision function for a single M5 bar-close cycle: no I/O,
    entirely deterministic given its inputs. `main()` fetches the inputs
    (broker/storage calls) and executes the resulting
    `position_actions`/entry decision against the broker, then persists
    the outcome and constructs the next cycle's `FSMContext`.
    """
    processing = evaluate_processing_time(cycle_duration_seconds)
    if processing.exceeded_cap:
        logger.warning(
            "Bar-close cycle exceeded the 200ms processing cap: %.1fms", processing.duration_ms
        )

    drawdown = check_drawdown_breach(snapshot.account_state.equity, baselines)
    no_action = BarCloseCycleResult(
        context=context,
        drawdown=drawdown,
        processing=processing,
        entry_decision=None,
        entry_stop_loss=None,
        entry_volume=None,
        position_actions=(),
    )

    if drawdown.is_halted:
        halted_context = FSMContext(
            state=TradingState.HALTED,
            position=context.position,
            halt_reason=f"drawdown breaker(s) tripped: {[b.value for b in drawdown.breached]}",
        )
        return BarCloseCycleResult(
            context=halted_context,
            drawdown=drawdown,
            processing=processing,
            entry_decision=None,
            entry_stop_loss=None,
            entry_volume=None,
            position_actions=(),
        )

    if context.state == TradingState.HALTED:
        # Remains halted until a human clears it (docs/RUNBOOK.md
        # CRITICAL posture) — never auto-resumes even if drawdown recovers.
        return no_action

    news_locked = is_trade_entry_locked(snapshot.now_utc, snapshot.news_events)

    if context.position is None:
        entry_decision = decide_entry_signal(
            snapshot.trend, snapshot.breakout, snapshot.pullback, snapshot.wick_fill, news_locked
        )
        if entry_decision.direction == "NONE" or snapshot.current_price is None:
            return BarCloseCycleResult(
                context=context,
                drawdown=drawdown,
                processing=processing,
                entry_decision=entry_decision,
                entry_stop_loss=None,
                entry_volume=None,
                position_actions=(),
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
            context=context,
            drawdown=drawdown,
            processing=processing,
            entry_decision=entry_decision,
            entry_stop_loss=entry_stop_loss,
            entry_volume=entry_volume,
            position_actions=(),
        )

    if snapshot.current_price is None:
        return no_action

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
            context.position, snapshot.current_price, snapshot.atr_value
        )
        if trailing_action is not None:
            actions = [trailing_action]

    return BarCloseCycleResult(
        context=context,
        drawdown=drawdown,
        processing=processing,
        entry_decision=None,
        entry_stop_loss=None,
        entry_volume=None,
        position_actions=tuple(actions),
    )


# ---------------------------------------------------------------------------
# Impure I/O layer: bootstrap and the live loop. Reviewed for correctness
# but not executed against a live/demo account in this environment — see
# docs/ARCHITECTURE_SUMMARY.md "Before your first live/demo run".
# ---------------------------------------------------------------------------


@dataclass
class RuntimeHandles:
    """Live objects the running process holds for its lifetime."""

    config: ConfigManager
    state_manager: StateManager
    gateway: MT5Gateway


def bootstrap_system(env_file: str | None = None) -> RuntimeHandles:
    """Boot sequence per docs/RUNBOOK.md §1: load config, open storage,
    connect the broker (with exponential backoff), and reconcile broker-
    reported positions against the local ledger before returning.

    Raises whatever the underlying step raises (ConfigurationError,
    BrokerConnectionError, etc.) — startup is fail-closed, never
    fail-open into a partially-initialized state.
    """
    config = ConfigManager.load(env_file=env_file)
    state_manager = StateManager()

    gateway = MT5Gateway(
        login=config.mt5_login,
        password=config.mt5_password,
        server=config.mt5_server,
        magic_number=config.strategy_magic_number,
    )
    gateway.connect()

    audit = gateway.audit_open_positions(state_manager.get_open_trades())
    if not audit.is_clean:
        logger.warning(
            "Position audit found divergence on startup: broker_only=%s ledger_only=%s",
            [p.ticket for p in audit.broker_only_positions],
            [e.client_order_id for e in audit.ledger_only_entries],
        )

    return RuntimeHandles(config=config, state_manager=state_manager, gateway=gateway)


def _fetch_market_snapshot(
    gateway: MT5Gateway, magic_number: int, news_events: list[EconomicEvent]
) -> MarketSnapshot:
    """Fetch bars/account state and evaluate the trend filter and entry
    triggers on the H1 timeframe (the finest granularity trend_filter
    already evaluates ADX on)."""
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
    tick_price = float(h1_bars.close[-1])

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
    )


def main() -> None:
    """Entry point: bootstrap, then loop forever, acting once per new M5
    bar close. Not executed in this environment (no live MT5 credentials);
    see docs/ARCHITECTURE_SUMMARY.md before running this against a real
    account.
    """
    logging.basicConfig(level=logging.INFO)
    handles = bootstrap_system()

    context = FSMContext(state=TradingState.IDLE, position=None, halt_reason=None)
    baselines: EquityBaselines | None = None
    constraints = SymbolConstraints(
        point=handles.gateway.symbol_spec.point,
        volume_min=handles.gateway.symbol_spec.volume_min,
        volume_max=handles.gateway.symbol_spec.volume_max,
        volume_step=handles.gateway.symbol_spec.volume_step,
        magic_number=handles.config.strategy_magic_number,
    )

    while True:
        time.sleep(seconds_until_next_bar_close(datetime.now(timezone.utc)))

        started = time.perf_counter()
        account_state = handles.gateway.get_account_state()
        if baselines is None:
            # First cycle: seed all three baselines from current equity.
            # Real daily/weekly/monthly rollover tracking is a further
            # refinement — see docs/ARCHITECTURE_SUMMARY.md.
            baselines = EquityBaselines(
                daily_start_equity=account_state.equity,
                weekly_start_equity=account_state.equity,
                monthly_start_equity=account_state.equity,
            )

        snapshot = _fetch_market_snapshot(handles.gateway, constraints.magic_number, [])
        ended = time.perf_counter()

        result = run_bar_close_cycle(
            context,
            snapshot,
            baselines,
            constraints,
            cycle_duration_seconds=ended - started,
        )
        context = result.context

        for action in result.position_actions:
            handles.gateway.submit_position_action(action)

        if (
            result.entry_decision is not None
            and result.entry_decision.direction != "NONE"
            and result.entry_stop_loss is not None
            and result.entry_volume is not None
        ):
            client_order_id = str(uuid.uuid4())
            broker_position = handles.gateway.submit_market_order(
                side=result.entry_decision.direction,
                volume=result.entry_volume,
                stop_loss=result.entry_stop_loss,
                take_profit=None,
                comment=client_order_id,
            )
            handles.state_manager.record_trade(
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
                halt_reason=None,
            )


if __name__ == "__main__":
    main()
