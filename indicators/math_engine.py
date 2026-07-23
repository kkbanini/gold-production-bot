"""Pure numpy technical indicator functions: SMA, EMA, Wilder ATR, Wilder
ADX, Wilder RSI, MACD, Bollinger Bands.

Standalone vector math over OHLC price arrays — no I/O, no broker
dependency, no MetaTrader5 import. Direct MT5-computed indicator values are
banned by directive; every value here is derived from raw OHLC arrays
(RQ-007).

EMA/ATR/ADX/RSI/MACD's signal line are IIR (recursive) filters by
definition: each smoothed value depends on the previous smoothed value,
which cannot be expressed as a single element-wise vectorized numpy
operation. The recursive step below uses one explicit Python loop writing
into a preallocated numpy array; every other computation (true range,
directional movement, SMA's cumulative-sum window, Bollinger's rolling
stddev) is fully vectorized.

`rsi()`/`macd()`/`bollinger_bands()` are monitoring-only today — reported
by `monitoring/telegram_bot.py`'s `/condition` command alongside the
trend/ADX/trigger checklist, but not consulted by `main.py`'s actual
entry/position-management decisions (a deliberate scope choice, not an
oversight: adding them as real trading gates is a separate, larger change
this session did not request).
"""

from __future__ import annotations

from typing import TypeAlias

import numpy as np
import numpy.typing as npt

FloatArray: TypeAlias = npt.NDArray[np.float64]


def sma(values: FloatArray, period: int) -> FloatArray:
    """Simple moving average via a vectorized cumulative-sum window (SMA is
    a plain windowed average, not an IIR filter, so no recursive loop is
    needed here unlike ema()/_wilder_smooth()).

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
    cumulative_sum = np.cumsum(values, dtype=np.float64)
    result[period - 1] = cumulative_sum[period - 1] / period
    result[period:] = (cumulative_sum[period:] - cumulative_sum[:-period]) / period
    return result


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


def rsi(values: FloatArray, period: int = 14) -> FloatArray:
    """Relative Strength Index (Wilder's original smoothing of average
    gain/loss, not a plain SMA-based variant).

    Needs `period` price changes (i.e. `period + 1` closes) to seed the
    first value via `_wilder_smooth()` — mirrors `atr()`/`adx()`'s own
    Wilder-smoothing pattern. A zero average loss (an unbroken run of
    gains) reads as RSI=100 rather than raising on the 0/0 division.
    Returns an array the same length as `values`; the first `period`
    entries are NaN.
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    values = np.asarray(values, dtype=np.float64)
    n = values.shape[0]
    if n < period + 1:
        raise ValueError(f"need at least {period + 1} values, got {n}")

    delta = np.diff(values)
    gains: FloatArray = np.concatenate(([np.nan], np.where(delta > 0.0, delta, 0.0)))
    losses: FloatArray = np.concatenate(([np.nan], np.where(delta < 0.0, -delta, 0.0)))
    avg_gain = _wilder_smooth(gains, period) / period
    avg_loss = _wilder_smooth(losses, period) / period

    result: FloatArray = np.full(n, np.nan, dtype=np.float64)
    valid: npt.NDArray[np.bool_] = ~np.isnan(avg_gain) & ~np.isnan(avg_loss)
    zero_loss = valid & (avg_loss == 0.0)
    nonzero_loss = valid & ~zero_loss
    result[zero_loss] = 100.0
    with np.errstate(divide="ignore", invalid="ignore"):
        rs: FloatArray = avg_gain / avg_loss
    result[nonzero_loss] = 100.0 - (100.0 / (1.0 + rs[nonzero_loss]))
    return result


def macd(
    values: FloatArray,
    *,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Moving Average Convergence/Divergence: `macd_line = EMA(fast) -
    EMA(slow)`, `signal_line = EMA(signal_period)` of `macd_line`,
    `histogram = macd_line - signal_line`.

    `signal_line`'s EMA is seeded from `macd_line`'s own first valid
    (non-NaN) index — `slow_period - 1` — rather than index 0, mirroring
    `ema()`'s seeding convention applied to the valid tail instead of
    reimplementing NaN-skipping logic. Returns three arrays the same
    length as `values`.
    """
    for name, value in (
        ("fast_period", fast_period),
        ("slow_period", slow_period),
        ("signal_period", signal_period),
    ):
        if value < 1:
            raise ValueError(f"{name} must be >= 1, got {value}")
    if fast_period >= slow_period:
        raise ValueError(f"fast_period must be < slow_period, got {fast_period} >= {slow_period}")

    values = np.asarray(values, dtype=np.float64)
    n = values.shape[0]
    min_required = slow_period + signal_period - 1
    if n < min_required:
        raise ValueError(f"need at least {min_required} values, got {n}")

    fast_ema = ema(values, fast_period)
    slow_ema = ema(values, slow_period)
    macd_line: FloatArray = fast_ema - slow_ema

    signal_line: FloatArray = np.full(n, np.nan, dtype=np.float64)
    start = slow_period - 1
    alpha = 2.0 / (signal_period + 1.0)
    seed_index = start + signal_period - 1
    signal_line[seed_index] = macd_line[start : seed_index + 1].mean()
    for i in range(seed_index + 1, n):
        signal_line[i] = alpha * macd_line[i] + (1.0 - alpha) * signal_line[i - 1]

    histogram: FloatArray = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(
    values: FloatArray, period: int = 20, num_std: float = 2.0
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Bollinger Bands: `middle = SMA(period)`, `upper`/`lower = middle +/-
    num_std * rolling population stddev(period)`.

    Rolling stddev is computed via a vectorized sliding window (`ddof=0`,
    population stddev — the conventional Bollinger Bands definition)
    rather than a Python loop, consistent with this module's "fully
    vectorized except IIR filters" design. Returns three arrays the same
    length as `values`; the first `period - 1` entries of each are NaN.
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    values = np.asarray(values, dtype=np.float64)
    n = values.shape[0]
    if n < period:
        raise ValueError(f"need at least {period} values, got {n}")

    middle = sma(values, period)
    windows = np.lib.stride_tricks.sliding_window_view(values, period)
    rolling_std: FloatArray = np.full(n, np.nan, dtype=np.float64)
    rolling_std[period - 1 :] = windows.std(axis=1, ddof=0)

    upper: FloatArray = middle + num_std * rolling_std
    lower: FloatArray = middle - num_std * rolling_std
    return upper, middle, lower


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
