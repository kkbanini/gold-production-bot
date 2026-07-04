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


@dataclass(frozen=True, slots=True)
class PositionState:
    """Local view of an open position's management state.

    Persisting this across restarts (e.g. via a future storage/ column)
    is a later-phase concern; this phase only defines the decision logic
    that consumes it.
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
) -> OrderActionPayload | None:
    """Dynamic ATR trailing stop, active only once breakeven has been set
    (`evaluate_partial_close_and_breakeven` covers the entry-to-breakeven
    leg; this covers the runner beyond that point). The candidate stop
    only ever tightens in the trend's favor — a candidate level that would
    loosen the existing stop is rejected, protecting an already-locked-in
    gain from a transient adverse price move.
    """
    if not position.breakeven_set:
        return None
    if atr_value <= 0:
        raise ValueError(f"atr_value must be > 0, got {atr_value}")

    if position.side == "BUY":
        candidate_sl = current_price - trailing_atr_multiplier * atr_value
        if candidate_sl <= position.stop_loss:
            return None
    else:
        candidate_sl = current_price + trailing_atr_multiplier * atr_value
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
