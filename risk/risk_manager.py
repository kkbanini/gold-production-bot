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

# Deposit-currency codes known to be "Cent" account variants (1 real USD =
# 100 units of account currency) rather than a standard 1:1 currency —
# Exness's Cent accounts report "USC" specifically (confirmed against the
# live account this was built for; broker.mt5_gateway.AccountState.currency
# is read straight from mt5.account_info().currency, so re-verify this
# set against that live value if a different broker/account type is ever
# connected and reports something not listed here — silently NOT
# recognizing a cent account is the safe failure mode, since it just
# falls back to today's pre-existing USD-assuming behavior rather than
# guessing wrong in either direction).
CENT_ACCOUNT_CURRENCY_CODES = frozenset({"USC"})
CENT_ACCOUNT_SCALE_FACTOR = 100.0


def normalize_cent_denominated_equity(equity: float, account_currency: str) -> float:
    """Converts `equity` to its real-USD-equivalent value if
    `account_currency` is a known Cent-account code (divides by
    `CENT_ACCOUNT_SCALE_FACTOR`), otherwise returns it unchanged.

    `calculate_compounded_lot_size()`'s `equity_per_lot_increment` is a
    hardcoded absolute amount (`$1000`) compared directly against raw
    `equity` — correct only if `equity` is actually denominated in USD.
    A Cent account (e.g. Exness Cent: 1 USD = 100 USC) reports `equity`
    ~100x larger for the same real capital, which would otherwise trigger
    lot-size tiers 100x too early relative to real money at stake. Ratio-
    based uses of equity (drawdown percentages) and tick_value-normalized
    price-distance formulas (`calculate_price_distance_for_target_profit()`)
    don't need this — only this one hardcoded-absolute-amount comparison
    does.
    """
    if account_currency.upper() in CENT_ACCOUNT_CURRENCY_CODES:
        return equity / CENT_ACCOUNT_SCALE_FACTOR
    return equity


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


def calculate_price_distance_for_target_profit(
    target_profit_usd: float, volume: float, tick_value: float, tick_size: float
) -> float:
    """Convert a target dollar profit into the price distance that
    realizes it at `volume` lots, given the broker's own
    `tick_value`/`tick_size` (`broker.mt5_gateway.SymbolSpec` —
    `tick_value` is the account-currency profit per `tick_size` price
    move, per 1.0 lot; both broker-reported, not fixed constants, since a
    fixed price distance means a different dollar amount at 0.01 lots
    than at 0.1).

    `target_profit_usd / (volume lots * tick_value per tick) * tick_size`
    — e.g. tick_value=$1.00 per 0.01 tick per lot, volume=0.01 lots,
    target=$5: distance = 5 / (0.01 * 1.00) * 0.01 = 5.0 price units.
    """
    if target_profit_usd <= 0:
        raise ValueError(f"target_profit_usd must be > 0, got {target_profit_usd}")
    if volume <= 0:
        raise ValueError(f"volume must be > 0, got {volume}")
    if tick_value <= 0 or tick_size <= 0:
        raise ValueError(
            f"tick_value and tick_size must both be > 0, got tick_value={tick_value}, "
            f"tick_size={tick_size}"
        )
    return target_profit_usd * tick_size / (tick_value * volume)
