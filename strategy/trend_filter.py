"""Master trend alignment filter across D1/H4/H1 EMAs with an ADX(14)
strength confirmation gate.

Pure functions over numpy OHLC arrays (RQ-008 determinism) — no I/O, no
broker dependency. Consumes indicators/math_engine.py; never recomputes
indicator math inline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from indicators.math_engine import FloatArray, adx, ema

D1_EMA_PERIOD = 200
H4_EMA_PERIOD = 50
H1_EMA_PERIOD = 40
ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 25.0

TrendDirection = Literal["BULLISH", "BEARISH", "NONE"]


@dataclass(frozen=True, slots=True)
class TrendAlignment:
    """Result of evaluating master trend alignment across D1/H4/H1 EMAs
    plus the H1 ADX(14) trend-strength gate."""

    direction: TrendDirection
    d1_bullish: bool
    h4_bullish: bool
    h1_bullish: bool
    adx_value: float
    adx_confirmed: bool

    @property
    def is_valid(self) -> bool:
        """True iff all three timeframes agree on direction AND the H1
        ADX(14) confirms sufficient trend strength."""
        return self.direction != "NONE" and self.adx_confirmed


def evaluate_master_trend(
    d1_close: FloatArray,
    h4_high: FloatArray,
    h4_low: FloatArray,
    h4_close: FloatArray,
    h1_high: FloatArray,
    h1_low: FloatArray,
    h1_close: FloatArray,
) -> TrendAlignment:
    """Validate master trend alignment: the latest close on D1, H4, and H1
    must all sit on the same side of that timeframe's own EMA (D1 EMA(200),
    H4 EMA(50), H1 EMA(40)), and the H1 ADX(14) must exceed
    ADX_TREND_THRESHOLD, or the alignment is not tradeable.

    Each timeframe's close is compared against its own EMA rather than a
    single cross-timeframe price, since each timeframe's close is only
    available at its own bar-close cadence. Requires at least D1_EMA_PERIOD
    D1 bars, H4_EMA_PERIOD H4 bars, and 2*(ADX_PERIOD-1)+H1_EMA_PERIOD-worth
    of H1 bars (whichever indicator needs the most warm-up); insufficient
    history raises ValueError via indicators.math_engine rather than
    silently producing a NaN-derived result.
    """
    d1_ema = ema(d1_close, D1_EMA_PERIOD)
    h4_ema = ema(h4_close, H4_EMA_PERIOD)
    h1_ema = ema(h1_close, H1_EMA_PERIOD)
    h1_adx = adx(h1_high, h1_low, h1_close, ADX_PERIOD)

    d1_last_close, d1_last_ema = float(d1_close[-1]), float(d1_ema[-1])
    h4_last_close, h4_last_ema = float(h4_close[-1]), float(h4_ema[-1])
    h1_last_close, h1_last_ema = float(h1_close[-1]), float(h1_ema[-1])
    adx_value = float(h1_adx[-1])

    d1_bullish = d1_last_close > d1_last_ema
    h4_bullish = h4_last_close > h4_last_ema
    h1_bullish = h1_last_close > h1_last_ema

    d1_bearish = d1_last_close < d1_last_ema
    h4_bearish = h4_last_close < h4_last_ema
    h1_bearish = h1_last_close < h1_last_ema

    direction: TrendDirection
    if d1_bullish and h4_bullish and h1_bullish:
        direction = "BULLISH"
    elif d1_bearish and h4_bearish and h1_bearish:
        direction = "BEARISH"
    else:
        direction = "NONE"

    return TrendAlignment(
        direction=direction,
        d1_bullish=d1_bullish,
        h4_bullish=h4_bullish,
        h1_bullish=h1_bullish,
        adx_value=adx_value,
        adx_confirmed=adx_value > ADX_TREND_THRESHOLD,
    )
