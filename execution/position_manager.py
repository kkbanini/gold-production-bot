"""Position management: 50%-at-target partial close, breakeven, and a
dynamic ATR trailing stop.

Pure decision functions over position/market state — produces
`OrderActionPayload` "intents" mirroring the shape of a MetaTrader5
`order_send()` request dict, but never imports `MetaTrader5` itself
(ADR-0002/RQ-001: only `broker/` may). `broker/mt5_gateway.py` is
responsible for translating an `OrderActionPayload` into a real
`mt5.order_send()` call (see its `submit_position_action()`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from risk.risk_manager import clamp_lot_size

PositionSide = Literal["BUY", "SELL"]
OrderActionType = Literal["TRADE_ACTION_DEAL", "TRADE_ACTION_SLTP"]

BASE_TP_ATR_MULTIPLIER = 2.0
PARTIAL_CLOSE_FRACTION = 0.5
TRAILING_ATR_MULTIPLIER = 1.5

# Trailing multiplier used BEFORE breakeven has been set — deliberately
# wider than TRAILING_ATR_MULTIPLIER (looser, so it doesn't compete with
# Base_TP's own 2x-ATR target) but still finite, unlike the previous
# behavior of no trailing at all in this window (a fixed initial SL that
# never moved between entry and Base_TP). Found live: a position swung
# from roughly +$17,000 of unrealized profit — just short of Base_TP —
# all the way back to an $8,000+ loss with zero protection along the
# way, since nothing tightened the stop until the exact Base_TP price
# was reached. No spec reference — a made-up-but-documented default.
PRE_BREAKEVEN_TRAILING_ATR_MULTIPLIER = 2.5


@dataclass(frozen=True, slots=True)
class PositionState:
    """Local view of an open position's management state.

    `partial_closed`/`breakeven_set` are persisted across a process
    restart via `main._fsm_context_to_dict()`/`storage.state_manager.StateManager.save_fsm_state()`
    (saved every cycle) and restored via `main._persisted_position_flags()`
    (`main._seed_initial_fsm_context()`, ticket-matched against the
    broker's real open position) — without this, every restart mid-
    position re-armed an already-completed partial-close/breakeven step,
    repeatedly halving whatever volume remained (found live: a position
    partial-closed itself down from 5.11 lots to 0.01 across repeated
    restarts). `ticket`/`symbol`/`side`/`volume`/`entry_price`/`stop_loss`/
    `magic_number` are never taken from the persisted snapshot, only from
    the broker's live position — it remains the sole source of truth for
    anything it can report directly.
    """

    ticket: int
    symbol: str
    side: PositionSide
    volume: float
    entry_price: float
    stop_loss: float
    magic_number: int
    partial_closed: bool
    breakeven_set: bool


@dataclass(frozen=True, slots=True)
class OrderActionPayload:
    """Mirrors the shape of a MetaTrader5 `order_send()` request dict
    (action/position/symbol/volume/sl/tp/magic/comment). `broker/mt5_gateway.py`
    — the sole module permitted to import `MetaTrader5` (ADR-0002/RQ-001)
    — is responsible for translating this into a real `mt5.order_send()`
    call."""

    action: OrderActionType
    position_ticket: int
    symbol: str
    magic: int
    comment: str
    volume: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None


def calculate_base_take_profit(
    entry_price: float,
    atr_value: float,
    side: PositionSide,
    *,
    atr_multiplier: float = BASE_TP_ATR_MULTIPLIER,
) -> float:
    """Base_TP = entry ± ATR × atr_multiplier (default 2.0), per the phase
    directive's `Base_TP = ATR * 2`."""
    if atr_value <= 0:
        raise ValueError(f"atr_value must be > 0, got {atr_value}")
    distance = atr_value * atr_multiplier
    return entry_price + distance if side == "BUY" else entry_price - distance


def evaluate_partial_close_and_breakeven(
    position: PositionState,
    current_price: float,
    atr_value: float,
    volume_min: float,
    volume_max: float,
    volume_step: float,
    *,
    partial_close_fraction: float = PARTIAL_CLOSE_FRACTION,
    atr_multiplier: float = BASE_TP_ATR_MULTIPLIER,
) -> list[OrderActionPayload]:
    """Evaluate whether price has reached Base_TP (ATR × 2 from entry) and,
    if so and the position hasn't already been partial-closed, return the
    two sequential actions this triggers: (1) close `partial_close_fraction`
    (default 50%) of the position, then (2) move the remaining volume's
    stop-loss to breakeven (the entry price).

    Returns an empty list if already partial-closed or Base_TP hasn't been
    reached yet. The breakeven level is set to the exact entry price with
    no spread/commission buffer, so a runner later stopped at breakeven
    may realize a small net loss after costs — a known simplification, not
    an oversight.
    """
    if position.partial_closed:
        return []

    base_tp = calculate_base_take_profit(
        position.entry_price, atr_value, position.side, atr_multiplier=atr_multiplier
    )
    reached = current_price >= base_tp if position.side == "BUY" else current_price <= base_tp
    if not reached:
        return []

    volume_to_close = clamp_lot_size(
        position.volume * partial_close_fraction, volume_min, volume_max, volume_step
    )
    volume_to_close = min(volume_to_close, position.volume)

    close_payload = OrderActionPayload(
        action="TRADE_ACTION_DEAL",
        position_ticket=position.ticket,
        symbol=position.symbol,
        magic=position.magic_number,
        comment="partial_close_base_tp",
        volume=volume_to_close,
    )
    breakeven_payload = OrderActionPayload(
        action="TRADE_ACTION_SLTP",
        position_ticket=position.ticket,
        symbol=position.symbol,
        magic=position.magic_number,
        comment="move_sl_breakeven",
        stop_loss=position.entry_price,
    )
    return [close_payload, breakeven_payload]


def calculate_trailing_stop(
    position: PositionState,
    current_price: float,
    atr_value: float,
    *,
    trailing_atr_multiplier: float = TRAILING_ATR_MULTIPLIER,
    pre_breakeven_trailing_atr_multiplier: float = PRE_BREAKEVEN_TRAILING_ATR_MULTIPLIER,
) -> OrderActionPayload | None:
    """Dynamic ATR trailing stop, active for the position's *entire*
    life — not just after breakeven. Uses the wider (looser)
    `pre_breakeven_trailing_atr_multiplier` before
    `evaluate_partial_close_and_breakeven()` has set breakeven, then the
    tighter `trailing_atr_multiplier` after. Previously this returned
    `None` unconditionally before breakeven, meaning the fixed initial SL
    never moved no matter how far price ran in favor — found live: a
    position swung from roughly +$17,000 unrealized (just short of
    Base_TP) to an $8,000+ loss with zero protection the entire way,
    since nothing tightened the stop until the exact Base_TP price.
    Using a wider pre-breakeven multiplier (rather than the same one
    Base_TP itself uses) keeps this from firing on every ordinary
    fluctuation and racing/duplicating Base_TP's own job — it only ever
    starts improving on the initial stop once price has moved
    meaningfully in favor, well before the full Base_TP distance.

    Either way the candidate stop only ever tightens in the trend's
    favor — a candidate level that would loosen the existing stop is
    rejected, protecting an already-locked-in gain (or partial
    retracement buffer) from a transient adverse price move.
    """
    if atr_value <= 0:
        raise ValueError(f"atr_value must be > 0, got {atr_value}")
    multiplier = (
        trailing_atr_multiplier if position.breakeven_set else pre_breakeven_trailing_atr_multiplier
    )

    if position.side == "BUY":
        candidate_sl = current_price - multiplier * atr_value
        if candidate_sl <= position.stop_loss:
            return None
    else:
        candidate_sl = current_price + multiplier * atr_value
        if candidate_sl >= position.stop_loss:
            return None

    return OrderActionPayload(
        action="TRADE_ACTION_SLTP",
        position_ticket=position.ticket,
        symbol=position.symbol,
        magic=position.magic_number,
        comment="atr_trailing_stop",
        stop_loss=candidate_sl,
    )


EMERGENCY_LIQUIDATION_COMMENT = "emergency_liquidation_hard_lock"


def build_emergency_liquidation_action(position: PositionState) -> OrderActionPayload:
    """Full-volume market close — the `FeatureFlagManager`-driven
    emergency liquidation payload `docs/PRODUCTION_SPEC.md` §6 requires
    when `liquidate_on_hard_lock` is `True`. Unlike
    `evaluate_partial_close_and_breakeven`'s 50% partial close, this closes
    the position's *entire* remaining volume in a single
    `TRADE_ACTION_DEAL` — `broker.mt5_gateway.MT5Gateway.submit_position_action()`
    already translates a full-volume `TRADE_ACTION_DEAL` into a real
    `mt5.order_send()` close, so no new broker-side method is needed."""
    return OrderActionPayload(
        action="TRADE_ACTION_DEAL",
        position_ticket=position.ticket,
        symbol=position.symbol,
        magic=position.magic_number,
        comment=EMERGENCY_LIQUIDATION_COMMENT,
        volume=position.volume,
    )


def build_short_term_liquidation_action(
    *,
    ticket: int,
    symbol: str,
    magic_number: int,
    volume: float,
    comment: str = EMERGENCY_LIQUIDATION_COMMENT,
) -> OrderActionPayload:
    """Full-volume market close for a short-term (scalp) position — same
    shape as `build_emergency_liquidation_action()` but takes plain scalar
    fields instead of a `PositionState`. Short-term positions are never
    tracked as `PositionState` (they're stateless, fire-and-forget via a
    fixed SL/TP MT5 manages itself; `docs/ARCHITECTURE_SUMMARY.md`) — the
    caller (`main.py`) already holds the broker-reported ticket/symbol/
    magic/volume directly, and this module deliberately does not import
    `broker.mt5_gateway`'s `BrokerPosition` to avoid a circular import
    (`broker/mt5_gateway.py` already imports `OrderActionPayload` from
    this module).

    `comment` defaults to `EMERGENCY_LIQUIDATION_COMMENT` (the existing
    `HARD_LOCK` call site) but the profit-peak lock passes a distinct
    comment instead — `main()`'s existing `if action.comment ==
    EMERGENCY_LIQUIDATION_COMMENT` check (which clears the *regular*
    position's `FSMContext`) must never fire for a short-term-only
    close unrelated to a real `HARD_LOCK`.
    """
    return OrderActionPayload(
        action="TRADE_ACTION_DEAL",
        position_ticket=ticket,
        symbol=symbol,
        magic=magic_number,
        comment=comment,
        volume=volume,
    )
