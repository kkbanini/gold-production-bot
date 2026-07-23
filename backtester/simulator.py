"""Event-driven backtest loop: drives `main.py`'s actual, unmodified live
decision code (`_fetch_market_snapshot()` + `run_bar_close_cycle()`)
against historical bars one H1-aligned bar-close at a time via
`HistoricalReplayGateway`, simulating fills for whatever it decides.

Regular (`WAIT_FOR_CONDITIONS`) strategy only — short-term mode is out of
scope for this phase. `manual_reset_confirmed` is always `False` (matches
live: `main.py` has no control channel to clear a `HARD_LOCK` either), so
a real `MANUAL_RESET_REQUIRED` freeze anywhere in the historical run
correctly halts all further trading for the rest of that run, exactly as
it would live — this is a real finding to report, not a simulator bug to
work around.

Two simplifications, both documented here since they're this phase's
accepted scope (`docs/RESEARCH.md`'s tick-level cost model is a later-phase
concern): (1) every fill — entry, partial close, breakeven-triggered
close, liquidation — executes at the deciding bar's own close price, no
spread/slippage/commission; (2) a stop-loss can still be hit *between*
decision points, so every bar's high/low range is checked against the
open position's current stop-loss *before* that bar's own decision cycle
runs — this models the real broker's own automatic stop-loss execution,
which `run_bar_close_cycle()` itself never simulates (it only decides
*our* bot's actions).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backtester.replay_gateway import HistoricalReplayGateway
from broker.mt5_gateway import TIMEFRAME_D1, TIMEFRAME_H1, TIMEFRAME_H4, AccountState, BarSeries
from config.feature_flags import FeatureFlagManager, FeatureFlags
from execution.position_manager import (
    EMERGENCY_LIQUIDATION_COMMENT,
    TRAILING_ATR_MULTIPLIER,
    PositionState,
)
from main import (
    D1_BAR_COUNT,
    H1_BAR_COUNT,
    H4_BAR_COUNT,
    TRADING_MODE_WAIT_FOR_CONDITIONS,
    FSMContext,
    SymbolConstraints,
    TradingState,
    _advance_equity_baselines,
    _fetch_market_snapshot,
    run_bar_close_cycle,
)
from risk.drawdown_fsm import BaselineEpoch, DrawdownState, EquityBaselines
from strategy.trend_filter import ADX_TREND_THRESHOLD

# Real IC Markets XAUUSD contract values, confirmed live
# (`broker.mt5_gateway.MT5Gateway.symbol_spec` on the connected account) —
# see `backtester/replay_gateway.py`'s `DEFAULT_XAUUSD_SYMBOL_SPEC` for the
# matching `SymbolSpec`.
TICK_VALUE = 1.0
TICK_SIZE = 0.01

# A round validation-scale starting balance, deliberately NOT the live
# account's actual ~$10 balance: this phase tests the strategy's own
# behavior under RQ-022's originally-designed drawdown thresholds (this
# module never overrides `run_bar_close_cycle()`'s `*_lock_limit` kwargs,
# so its RQ-022 defaults apply), not the emergency widened thresholds
# `.env` currently carries for the tiny live account (see
# `risk/drawdown_fsm.py`'s module docstring / `config/config_manager.py`'s
# override mechanism) — those two questions are deliberately kept separate.
DEFAULT_STARTING_EQUITY = 10_000.0
DEFAULT_MAGIC_NUMBER = 123456


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """One closing fill — a full position close, a partial (Base_TP) close,
    or a stop-loss/liquidation close all produce one of these each.
    Multiple rows can share the same `entry_time`/`entry_price` if a
    position was partially closed before its remainder closed later."""

    entry_time: datetime
    exit_time: datetime
    side: str
    volume: float
    entry_price: float
    exit_price: float
    profit: float
    reason: str


@dataclass(frozen=True, slots=True)
class BacktestResult:
    trades: tuple[ClosedTrade, ...]
    equity_curve: tuple[tuple[datetime, float], ...]


def _position_profit(position: PositionState, exit_price: float, volume: float) -> float:
    """`(exit_price - entry_price) * volume * (tick_value / tick_size)`,
    signed for side — the same P&L formula real XAUUSD economics use
    (confirmed against `TICK_VALUE`/`TICK_SIZE`: $1 of price movement is
    worth $100/lot, matching a 100oz contract)."""
    sign = 1.0 if position.side == "BUY" else -1.0
    return (exit_price - position.entry_price) * sign * volume * (TICK_VALUE / TICK_SIZE)


def _stop_loss_hit(position: PositionState, bar_low: float, bar_high: float) -> bool:
    if position.side == "BUY":
        return bar_low <= position.stop_loss
    return bar_high >= position.stop_loss


def _account_state(balance: float, equity: float, now_utc: datetime) -> AccountState:
    return AccountState(
        balance=balance,
        equity=equity,
        margin_used=0.0,
        margin_free=equity,
        as_of_utc=now_utc,
    )


def run_backtest(
    d1_bars: BarSeries,
    h4_bars: BarSeries,
    h1_bars: BarSeries,
    *,
    starting_equity: float = DEFAULT_STARTING_EQUITY,
    magic_number: int = DEFAULT_MAGIC_NUMBER,
    adx_trend_threshold: float = ADX_TREND_THRESHOLD,
    trailing_atr_multiplier: float = TRAILING_ATR_MULTIPLIER,
) -> BacktestResult:
    """Step forward one H1 bar-close at a time over `h1_bars`, evaluating
    `main.py`'s real decision code each step and simulating fills. See
    module docstring for the two accepted simplifications.

    `adx_trend_threshold`/`trailing_atr_multiplier` default to the same
    constants live trading uses; `backtester/walk_forward.py` (Phase 2)
    overrides them per its parameter sweep — the identical override
    pattern `main.py`'s own live loop already uses for the self-learning
    optimizer's applied shifts.
    """
    replay_gateway = HistoricalReplayGateway(
        d1_bars=d1_bars,
        h4_bars=h4_bars,
        h1_bars=h1_bars,
        starting_account_state=_account_state(
            starting_equity, starting_equity, h1_bars.time_utc[0]
        ),
    )
    constraints = SymbolConstraints(
        point=replay_gateway.symbol_spec.point,
        volume_min=replay_gateway.symbol_spec.volume_min,
        volume_max=replay_gateway.symbol_spec.volume_max,
        volume_step=replay_gateway.symbol_spec.volume_step,
        magic_number=magic_number,
        tick_value=TICK_VALUE,
        tick_size=TICK_SIZE,
    )
    feature_flags = FeatureFlagManager(FeatureFlags(liquidate_on_hard_lock=False))

    context = FSMContext(
        state=TradingState.IDLE,
        position=None,
        drawdown_state=DrawdownState.ACTIVE,
        drawdown_reason=None,
    )
    baselines: EquityBaselines | None = None
    baseline_epoch: BaselineEpoch | None = None
    balance = starting_equity
    position_entry_time: datetime | None = None
    next_ticket = 1

    trades: list[ClosedTrade] = []
    equity_curve: list[tuple[datetime, float]] = []

    for cursor_time in h1_bars.time_utc:
        replay_gateway.advance_to(cursor_time)
        bar_index = replay_gateway.get_bars(TIMEFRAME_H1, 1)
        if len(bar_index.close) == 0:
            continue
        bar_low = float(bar_index.low[-1])
        bar_high = float(bar_index.high[-1])
        bar_close = float(bar_index.close[-1])

        # Broker-side stop-loss execution, checked every bar regardless of
        # this cycle's decision — see module docstring.
        if context.position is not None and _stop_loss_hit(context.position, bar_low, bar_high):
            fill_price = context.position.stop_loss
            profit = _position_profit(context.position, fill_price, context.position.volume)
            balance += profit
            trades.append(
                ClosedTrade(
                    entry_time=position_entry_time or cursor_time,
                    exit_time=cursor_time,
                    side=context.position.side,
                    volume=context.position.volume,
                    entry_price=context.position.entry_price,
                    exit_price=fill_price,
                    profit=profit,
                    reason="stop_loss",
                )
            )
            context = FSMContext(
                state=TradingState.IDLE,
                position=None,
                drawdown_state=context.drawdown_state,
                drawdown_reason=context.drawdown_reason,
            )
            position_entry_time = None

        unrealized = (
            _position_profit(context.position, bar_close, context.position.volume)
            if context.position is not None
            else 0.0
        )
        equity = balance + unrealized
        replay_gateway.account_state = _account_state(balance, equity, cursor_time)

        d1_available = len(replay_gateway.get_bars(TIMEFRAME_D1, D1_BAR_COUNT).close)
        h4_available = len(replay_gateway.get_bars(TIMEFRAME_H4, H4_BAR_COUNT).close)
        h1_available = len(replay_gateway.get_bars(TIMEFRAME_H1, H1_BAR_COUNT).close)
        if (
            d1_available < D1_BAR_COUNT
            or h4_available < H4_BAR_COUNT
            or h1_available < H1_BAR_COUNT
        ):
            equity_curve.append((cursor_time, equity))
            continue

        snapshot = _fetch_market_snapshot(
            replay_gateway, magic_number, [], adx_trend_threshold=adx_trend_threshold
        )
        baselines, baseline_epoch = _advance_equity_baselines(
            baselines, baseline_epoch, snapshot.account_state.equity, cursor_time
        )
        previous_position = context.position

        result = run_bar_close_cycle(
            context,
            snapshot,
            baselines,
            constraints,
            feature_flags,
            cycle_duration_seconds=0.0,
            trading_mode=TRADING_MODE_WAIT_FOR_CONDITIONS,
            trailing_atr_multiplier=trailing_atr_multiplier,
        )
        context = result.context

        for action in result.position_actions:
            if action.action != "TRADE_ACTION_DEAL" or action.volume is None:
                continue
            if previous_position is None:
                continue
            fill_price = bar_close
            profit = _position_profit(previous_position, fill_price, action.volume)
            balance += profit
            trades.append(
                ClosedTrade(
                    entry_time=position_entry_time or cursor_time,
                    exit_time=cursor_time,
                    side=previous_position.side,
                    volume=action.volume,
                    entry_price=previous_position.entry_price,
                    exit_price=fill_price,
                    profit=profit,
                    reason=action.comment,
                )
            )
            if action.comment == EMERGENCY_LIQUIDATION_COMMENT:
                context = FSMContext(
                    state=TradingState.IDLE,
                    position=None,
                    drawdown_state=context.drawdown_state,
                    drawdown_reason=context.drawdown_reason,
                )
                position_entry_time = None

        if (
            result.entry_decision is not None
            and result.entry_decision.direction != "NONE"
            and result.entry_stop_loss is not None
            and result.entry_volume is not None
        ):
            fill_price = bar_close
            new_position = PositionState(
                ticket=next_ticket,
                symbol=replay_gateway.symbol_spec.name,
                side=result.entry_decision.direction,
                volume=result.entry_volume,
                entry_price=fill_price,
                stop_loss=result.entry_stop_loss,
                magic_number=magic_number,
                partial_closed=False,
                breakeven_set=False,
            )
            next_ticket += 1
            context = FSMContext(
                state=TradingState.IN_POSITION,
                position=new_position,
                drawdown_state=result.context.drawdown_state,
                drawdown_reason=result.context.drawdown_reason,
            )
            position_entry_time = cursor_time

        unrealized = (
            _position_profit(context.position, bar_close, context.position.volume)
            if context.position is not None
            else 0.0
        )
        equity_curve.append((cursor_time, balance + unrealized))

    return BacktestResult(trades=tuple(trades), equity_curve=tuple(equity_curve))
