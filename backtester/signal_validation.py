"""Empirical validation of `monitoring/telegram_bot.py`'s `/condition`
BUY/SELL/HOLD heuristic (`summarize_indicator_signal()`) against real
historical H1 price data: does the signal actually predict the direction
price moves over some forward horizon, better than chance?

Vectorized, not event-driven (unlike `backtester/simulator.py`):
`summarize_indicator_signal()` is monitoring-only, never consulted by
`main.py`'s actual trading decisions (`indicators/math_engine.py`'s module
docstring) — there is no live-decision-code parity requirement to
preserve here, so computing every indicator once over the full bar series
via `indicators/math_engine.py`'s existing array functions is both correct
and far cheaper than an event-driven replay.

A prediction's "correctness" is binary: a `BUY` is correct iff price is
higher `horizon_bars` later than now; a `SELL` is correct iff price is
lower. `HOLD` predicts nothing and is excluded from accuracy — this
mirrors how `/condition`'s own reader would use the signal (only BUY/SELL
are actionable).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from broker.mt5_gateway import BarSeries
from indicators.math_engine import FloatArray, bollinger_bands, macd, rsi, sma
from monitoring.telegram_bot import (
    BOLLINGER_NUM_STD,
    BOLLINGER_PERIOD,
    MA_PERIOD,
    MACD_FAST_PERIOD,
    MACD_SIGNAL_PERIOD,
    MACD_SLOW_PERIOD,
    RSI_PERIOD,
    summarize_indicator_signal,
)

DEFAULT_HORIZON_BARS = 4


@dataclass(frozen=True, slots=True)
class SignalPrediction:
    bar_index: int
    label: str
    score: int
    correct: bool


@dataclass(frozen=True, slots=True)
class SignalValidationResult:
    predictions: tuple[SignalPrediction, ...]

    @property
    def total(self) -> int:
        return len(self.predictions)

    @property
    def accuracy(self) -> float:
        if not self.predictions:
            return 0.0
        return sum(1 for p in self.predictions if p.correct) / len(self.predictions)

    def accuracy_for(self, label: str) -> float:
        subset = [p for p in self.predictions if p.label == label]
        if not subset:
            return 0.0
        return sum(1 for p in subset if p.correct) / len(subset)


def _compute_indicator_arrays(
    closes: FloatArray,
) -> tuple[FloatArray, FloatArray, FloatArray, FloatArray, FloatArray]:
    """`(ma, rsi_values, macd_histogram, bollinger_upper, bollinger_lower)`
    — the exact same formulas/periods `monitoring/telegram_bot.py`'s
    `_fetch_indicator_summary()` uses live, computed once over the whole
    series instead of per-cycle."""
    ma = sma(closes, MA_PERIOD)
    rsi_values = rsi(closes, RSI_PERIOD)
    _, _, macd_histogram = macd(
        closes,
        fast_period=MACD_FAST_PERIOD,
        slow_period=MACD_SLOW_PERIOD,
        signal_period=MACD_SIGNAL_PERIOD,
    )
    upper, _, lower = bollinger_bands(closes, period=BOLLINGER_PERIOD, num_std=BOLLINGER_NUM_STD)
    return ma, rsi_values, macd_histogram, upper, lower


def build_forward_labels(closes: FloatArray, *, horizon_bars: int) -> FloatArray:
    """`1.0` at index `i` iff `closes[i + horizon_bars] > closes[i]`, else
    `0.0`, for every `i` in `[0, len(closes) - horizon_bars)`. The single
    forward-looking binary label definition shared by `validate_signal()`
    (naive-heuristic accuracy) and `backtester/ml_signal_model.py` (the ML
    training target) — identical semantics, so accuracy figures from both
    are directly comparable on the same horizon.
    """
    if horizon_bars <= 0:
        raise ValueError(f"horizon_bars must be > 0, got {horizon_bars}")
    n = len(closes)
    if n <= horizon_bars:
        return np.array([], dtype=np.float64)
    future = closes[horizon_bars:n]
    current = closes[0 : n - horizon_bars]
    return (future > current).astype(np.float64)


def validate_signal(
    h1_bars: BarSeries,
    *,
    horizon_bars: int = DEFAULT_HORIZON_BARS,
    min_abs_score: int = 1,
    start_index: int = 0,
    end_index: int | None = None,
) -> SignalValidationResult:
    """Runs `summarize_indicator_signal()` at every bar in
    `[start_index, end_index)` (defaults to the whole series) with enough
    warm-up data, checking each non-`HOLD` prediction against the actual
    price `horizon_bars` later. `start_index`/`end_index` let a caller
    restrict validation to a chronological sub-range (in-sample/
    out-of-sample split) without re-slicing `h1_bars` itself, since the
    indicator arrays need the full series' lookback regardless of which
    sub-range is being scored.

    Returns an empty result (rather than raising) if `h1_bars` is shorter
    than the longest indicator's own warm-up requirement (`indicators/
    math_engine.py`'s functions raise `ValueError` on insufficient data) —
    "not enough history to say anything yet" is a valid, unremarkable
    outcome for this function, not an error.
    """
    closes = h1_bars.close
    try:
        ma, rsi_values, macd_histogram, upper, lower = _compute_indicator_arrays(closes)
    except ValueError:
        return SignalValidationResult(predictions=())

    labels = build_forward_labels(closes, horizon_bars=horizon_bars)
    n = len(closes)
    stop = n - horizon_bars if end_index is None else min(end_index, n - horizon_bars)

    predictions: list[SignalPrediction] = []
    for i in range(max(start_index, 0), stop):
        if (
            np.isnan(ma[i])
            or np.isnan(rsi_values[i])
            or np.isnan(macd_histogram[i])
            or np.isnan(upper[i])
            or np.isnan(lower[i])
        ):
            continue
        label, score = summarize_indicator_signal(
            current_price=float(closes[i]),
            ma_value=float(ma[i]),
            rsi_value=float(rsi_values[i]),
            macd_histogram=float(macd_histogram[i]),
            bollinger_upper=float(upper[i]),
            bollinger_lower=float(lower[i]),
            min_abs_score=min_abs_score,
        )
        if label == "HOLD":
            continue
        actual_up = bool(labels[i])
        correct = (label == "BUY") == actual_up
        predictions.append(SignalPrediction(bar_index=i, label=label, score=score, correct=correct))

    return SignalValidationResult(predictions=tuple(predictions))
