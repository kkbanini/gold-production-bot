"""Equity-based position-size compounding.

Pure functions over plain floats — no I/O, no broker dependency, and no
dependency on `broker/`'s `SymbolSpec` (broker-specific volume constraints
are passed in as raw floats, mirroring the pattern already used by
`strategy/execution_triggers.py`'s `point` parameter).
"""

from __future__ import annotations

import math

BASE_LOT_SIZE = 0.01
EQUITY_PER_LOT_INCREMENT = 1000.0
LOT_INCREMENT = 0.01


def _decimals_from_step(step: float) -> int:
    """Number of decimal places implied by a broker volume_step (e.g. 0.01 -> 2)."""
    step_str = f"{step:.10f}".rstrip("0")
    if "." in step_str:
        return len(step_str.split(".")[1])
    return 0


def clamp_lot_size(
    raw_lots: float, volume_min: float, volume_max: float, volume_step: float
) -> float:
    """Round `raw_lots` down to the nearest broker `volume_step` multiple
    and clamp it to `[volume_min, volume_max]`.

    A non-positive `raw_lots` clamps up to `volume_min` rather than
    producing a zero/negative order size.
    """
    if volume_min <= 0 or volume_step <= 0 or volume_max < volume_min:
        raise ValueError(
            f"invalid broker volume constraints: min={volume_min}, "
            f"max={volume_max}, step={volume_step}"
        )
    if raw_lots <= 0:
        return volume_min

    steps = math.floor(raw_lots / volume_step)
    clamped = steps * volume_step
    clamped = max(clamped, volume_min)
    clamped = min(clamped, volume_max)
    return round(clamped, _decimals_from_step(volume_step))


def calculate_compounded_lot_size(
    equity: float,
    volume_min: float,
    volume_max: float,
    volume_step: float,
    *,
    base_lot_size: float = BASE_LOT_SIZE,
    equity_per_lot_increment: float = EQUITY_PER_LOT_INCREMENT,
    lot_increment: float = LOT_INCREMENT,
) -> float:
    """Scale position size directly with account equity in discrete tiers:
    for every `equity_per_lot_increment` of equity, add `lot_increment`
    lots on top of `base_lot_size`. This is what makes the sizing
    "compounding" — profits that grow equity automatically grow the next
    trade's size, and drawdowns automatically shrink it, with no separate
    manual resizing step.

    The default tier parameters (`base_lot_size=0.01`,
    `equity_per_lot_increment=1000.0`, `lot_increment=0.01` — i.e. one
    micro-lot added per full $1000 of equity) were not specified by the
    phase directive, which asked for equity-based compounding without
    giving exact tier numbers; they are this implementation's choice,
    flagged for review, not a pre-existing spec.
    """
    if equity <= 0:
        raise ValueError(f"equity must be > 0, got {equity}")
    if equity_per_lot_increment <= 0:
        raise ValueError(f"equity_per_lot_increment must be > 0, got {equity_per_lot_increment}")

    tiers = math.floor(equity / equity_per_lot_increment)
    raw_lots = base_lot_size + tiers * lot_increment
    return clamp_lot_size(raw_lots, volume_min, volume_max, volume_step)
