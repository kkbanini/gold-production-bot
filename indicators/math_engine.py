"""Pure numpy technical indicator functions: EMA, Wilder ATR, Wilder ADX.

Standalone vector math over OHLC price arrays — no I/O, no broker
dependency, no MetaTrader5 import. Direct MT5-computed indicator values are
banned by directive; every value here is derived from raw OHLC arrays
(RQ-007).

EMA/ATR/ADX are IIR (recursive) filters by definition: each smoothed value
depends on the previous smoothed value, which cannot be expressed as a
single element-wise vectorized numpy operation. The recursive step below
uses one explicit Python loop writing into a preallocated numpy array;
every other computation (true range, directional movement) is fully
vectorized.
"""

from __future__ import annotations

from typing import TypeAlias

import numpy as np
import numpy.typing as npt

FloatArray: TypeAlias = npt.NDArray[np.float64]


def ema(values: FloatArray, period: int) -> FloatArray:
    """Exponential moving average, seeded with the simple average of the
    first `period` values (the standard EMA seeding convention).

    Returns an array the same length as `values`; the first `period - 1`
    entries are NaN (undefined — insufficient warm-up data).
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    values = np.asarray(values, dtype=np.float64)
    n = values.shape[0]
    if n < period:
        raise ValueError(f"need at least {period} values, got {n}")

    result: FloatArray = np.full(n, np.nan, dtype=np.float64)
    alpha = 2.0 / (period + 1.0)
    result[period - 1] = values[:period].mean()
    for i in range(period, n):
        result[i] = alpha * values[i] + (1.0 - alpha) * result[i - 1]
    return result


def _wilder_smooth(values: FloatArray, period: int) -> FloatArray:
    """Wilder's smoothing (a.k.a. RMA): alpha = 1/period, seeded with the
    simple sum of the first `period` valid (non-NaN) values, per Welles
    Wilder's original ATR/ADX formulation. Used internally by atr() and
    adx(). Auto-detects the first non-NaN index so it can be safely
    re-applied to an array that already carries a leading NaN warm-up
    region from a prior smoothing pass (as adx() does for DX -> ADX).
    """
    n = values.shape[0]
    result: FloatArray = np.full(n, np.nan, dtype=np.float64)
    finite_mask: npt.NDArray[np.bool_] = ~np.isnan(values)
    if not finite_mask.any():
        return result
    start = int(np.argmax(finite_mask))
    if n - start < period:
        return result

    seed_index = start + period - 1
    result[seed_index] = values[start : seed_index + 1].sum()
    for i in range(seed_index + 1, n):
        result[i] = result[i - 1] - (result[i - 1] / period) + values[i]
    return result


def _true_range(high: FloatArray, low: FloatArray, close: FloatArray) -> FloatArray:
    n = high.shape[0]
    prev_close: FloatArray = np.empty(n, dtype=np.float64)
    prev_close[0] = close[0]
    prev_close[1:] = close[:-1]
    true_range: FloatArray = np.maximum(
        high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close))
    )
    true_range[0] = high[0] - low[0]
    return true_range


def _validate_ohlc(
    high: FloatArray, low: FloatArray, close: FloatArray
) -> tuple[FloatArray, FloatArray, FloatArray]:
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    close = np.asarray(close, dtype=np.float64)
    n = high.shape[0]
    if not (low.shape[0] == n and close.shape[0] == n):
        raise ValueError("high, low, close must be the same length")
    return high, low, close


def atr(high: FloatArray, low: FloatArray, close: FloatArray, period: int = 14) -> FloatArray:
    """Average True Range using Wilder's smoothing method.

    True Range[i] = max(high[i]-low[i], |high[i]-close[i-1]|,
    |low[i]-close[i-1]|), with True Range[0] = high[0]-low[0] (no prior
    close). Returns an array the same length as the inputs; the first
    `period - 1` entries are NaN.
    """
    high, low, close = _validate_ohlc(high, low, close)
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    n = high.shape[0]
    if n < period:
        raise ValueError(f"need at least {period} values, got {n}")

    true_range = _true_range(high, low, close)
    smoothed = _wilder_smooth(true_range, period)
    return smoothed / period


def adx(high: FloatArray, low: FloatArray, close: FloatArray, period: int = 14) -> FloatArray:
    """Average Directional Index (Wilder), the trend-strength line.

    Computes +DM/-DM, Wilder-smooths them alongside True Range to get
    +DI/-DI, derives DX = 100*|+DI - -DI|/(+DI + -DI) (0 where +DI and -DI
    are both 0), then Wilder-smooths DX to get ADX. Returns an array the
    same length as the inputs; the first `2 * (period - 1)` entries are
    NaN (two rounds of Wilder smoothing, each needing `period` warm-up
    values).
    """
    high, low, close = _validate_ohlc(high, low, close)
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    n = high.shape[0]
    if n < 2 * period - 1:
        raise ValueError(f"need at least {2 * period - 1} values, got {n}")

    up_move: FloatArray = np.zeros(n, dtype=np.float64)
    down_move: FloatArray = np.zeros(n, dtype=np.float64)
    up_move[1:] = high[1:] - high[:-1]
    down_move[1:] = low[:-1] - low[1:]

    plus_dm: FloatArray = np.where((up_move > down_move) & (up_move > 0.0), up_move, 0.0)
    minus_dm: FloatArray = np.where((down_move > up_move) & (down_move > 0.0), down_move, 0.0)
    true_range = _true_range(high, low, close)

    smoothed_tr = _wilder_smooth(true_range, period)
    smoothed_plus_dm = _wilder_smooth(plus_dm, period)
    smoothed_minus_dm = _wilder_smooth(minus_dm, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di: FloatArray = 100.0 * smoothed_plus_dm / smoothed_tr
        minus_di: FloatArray = 100.0 * smoothed_minus_dm / smoothed_tr
        denom: FloatArray = plus_di + minus_di

    dx: FloatArray = np.full(n, np.nan, dtype=np.float64)
    valid: npt.NDArray[np.bool_] = ~np.isnan(denom)
    zero_denom = valid & (denom == 0.0)
    nonzero_denom = valid & ~zero_denom
    dx[zero_denom] = 0.0
    dx[nonzero_denom] = (
        100.0 * np.abs(plus_di[nonzero_denom] - minus_di[nonzero_denom]) / denom[nonzero_denom]
    )

    return _wilder_smooth(dx, period) / period
