"""Technical entry-trigger rules: 2-candle breakout with a point-distance
and tick-volume filter, trend-continuation pullback, and wick-fill
rejection analytics.

Pure functions over numpy OHLC(+tick-volume) arrays (RQ-008 determinism) —
no I/O, no broker dependency. Precise pattern definitions are specified in
docs/RESEARCH.md §8 ("Entry Trigger Specification").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from indicators.math_engine import FloatArray, sma

Direction = Literal["BUY", "SELL", "NONE"]

BREAKOUT_MIN_POINTS = 50
BREAKOUT_VOLUME_SMA_PERIOD = 20
BREAKOUT_VOLUME_MULTIPLIER = 1.5
WICK_FILL_THRESHOLD = 0.60


@dataclass(frozen=True, slots=True)
class BreakoutSignal:
    """Result of evaluating the 2-candle breakout pattern at the latest
    closed bar (docs/RESEARCH.md §8.1)."""

    direction: Direction
    breakout_distance_points: float
    volume_confirmed: bool

    @property
    def is_valid(self) -> bool:
        """True iff a breakout fired AND the tick-volume filter confirms it."""
        return self.direction != "NONE" and self.volume_confirmed


def detect_breakout(
    high: FloatArray,
    low: FloatArray,
    close: FloatArray,
    tick_volume: FloatArray,
    point: float,
    *,
    min_breakout_points: int = BREAKOUT_MIN_POINTS,
    volume_sma_period: int = BREAKOUT_VOLUME_SMA_PERIOD,
    volume_multiplier: float = BREAKOUT_VOLUME_MULTIPLIER,
) -> BreakoutSignal:
    """Evaluate the 2-candle breakout pattern on the latest two closed bars.

    A bullish breakout requires the latest close to clear the *prior* bar's
    high by at least `min_breakout_points` broker points (point size from
    `broker.mt5_gateway.SymbolSpec.point`, resolved per-broker — never
    hardcoded); a bearish breakout is the mirror image against the prior
    bar's low. Volume confirmation requires the latest bar's tick_volume to
    exceed `volume_multiplier` times its own SMA(volume_sma_period).
    """
    if point <= 0:
        raise ValueError(f"point must be > 0, got {point}")
    n = high.shape[0]
    if n < 2 or low.shape[0] < 2 or close.shape[0] < 2:
        raise ValueError("need at least 2 bars to evaluate a 2-candle breakout pattern")

    min_distance = min_breakout_points * point
    prior_high, prior_low = float(high[-2]), float(low[-2])
    last_close = float(close[-1])

    bullish_distance = last_close - prior_high
    bearish_distance = prior_low - last_close

    volume_sma = sma(tick_volume, volume_sma_period)
    volume_threshold = float(volume_sma[-1]) * volume_multiplier
    volume_confirmed = float(tick_volume[-1]) > volume_threshold

    if bullish_distance >= min_distance:
        return BreakoutSignal("BUY", bullish_distance / point, volume_confirmed)
    if bearish_distance >= min_distance:
        return BreakoutSignal("SELL", bearish_distance / point, volume_confirmed)
    return BreakoutSignal("NONE", 0.0, volume_confirmed)


@dataclass(frozen=True, slots=True)
class PullbackSignal:
    """Result of evaluating a trend-continuation pullback at the latest
    closed bar (docs/RESEARCH.md §8.2)."""

    direction: Direction
    reference_level: float

    @property
    def is_valid(self) -> bool:
        return self.direction != "NONE"


def detect_pullback(
    high: FloatArray,
    low: FloatArray,
    close: FloatArray,
    reference_level: FloatArray,
    trend_direction: Direction,
) -> PullbackSignal:
    """Evaluate a trend-continuation pullback against `reference_level`
    (typically the timeframe's own trend EMA) at the latest closed bar.

    `trend_direction` should come from strategy.trend_filter's
    TrendAlignment.direction; "NONE" always yields a "NONE" pullback
    signal, since a pullback is only meaningful within an established
    trend (docs/RESEARCH.md §8.2).
    """
    if high.shape[0] < 1 or low.shape[0] < 1 or close.shape[0] < 1 or reference_level.shape[0] < 1:
        raise ValueError("need at least 1 bar to evaluate a pullback")

    level = float(reference_level[-1])
    last_high, last_low, last_close = float(high[-1]), float(low[-1]), float(close[-1])

    if trend_direction == "BUY" and last_low <= level < last_close:
        return PullbackSignal("BUY", level)
    if trend_direction == "SELL" and last_high >= level > last_close:
        return PullbackSignal("SELL", level)
    return PullbackSignal("NONE", level)


@dataclass(frozen=True, slots=True)
class WickFillResult:
    """Wick/shadow-length rejection analytics for the latest closed bar
    (docs/RESEARCH.md §8.3)."""

    upper_shadow_ratio: float
    lower_shadow_ratio: float
    rejection: Direction

    @property
    def is_significant(self) -> bool:
        return self.rejection != "NONE"


def analyze_wick_fill(
    open_: FloatArray,
    high: FloatArray,
    low: FloatArray,
    close: FloatArray,
    *,
    threshold: float = WICK_FILL_THRESHOLD,
) -> WickFillResult:
    """Classify the latest closed bar's wick/shadow proportions.

    A lower shadow exceeding `threshold` of the bar's full range maps to a
    "BUY"-side rejection signal (demand absorbed a decline); an upper
    shadow exceeding it maps to "SELL" (mirror image). A zero-range bar
    (high == low) yields no signal rather than a division by zero.
    """
    if open_.shape[0] < 1 or high.shape[0] < 1 or low.shape[0] < 1 or close.shape[0] < 1:
        raise ValueError("need at least 1 bar to analyze wick fill")

    o, h, low_price, c = float(open_[-1]), float(high[-1]), float(low[-1]), float(close[-1])
    bar_range = h - low_price
    if bar_range <= 0:
        return WickFillResult(0.0, 0.0, "NONE")

    upper_ratio = (h - max(o, c)) / bar_range
    lower_ratio = (min(o, c) - low_price) / bar_range

    rejection: Direction = "NONE"
    if lower_ratio > threshold:
        rejection = "BUY"
    elif upper_ratio > threshold:
        rejection = "SELL"

    return WickFillResult(upper_ratio, lower_ratio, rejection)
