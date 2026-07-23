"""Performance metric formulas, exactly per `docs/RESEARCH.md` §7 — Sharpe,
Sortino, MAR, max drawdown/duration, profit factor, win rate.

Pure functions operating on a plain equity curve (`Sequence[tuple[datetime,
float]]`, ascending by time) and a plain sequence of closed-trade P&L
values — no `storage/` dependency. This keeps "one implementation" (RQ-016's
actual concern): whether the equity curve comes from `storage/`'s
`EquityCurveRepository` (a live account, future work) or
`backtester/simulator.py`'s in-memory curve, the same formula code computes
the same numbers either way.

Uses `float` throughout, not `Decimal` — matching `broker.mt5_gateway.AccountState`
and every other numeric type already in this codebase; `docs/API_SPEC.md`
§5's `Decimal`-typed `PerformanceReport` was never actually followed
elsewhere either (flagged deviation, not a new one).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import numpy.typing as npt
from scipy.stats import norm

FloatArray = npt.NDArray[np.float64]

EquityCurve = Sequence[tuple[datetime, float]]

# Euler-Mascheroni constant, per docs/RESEARCH.md §3's SR0 formula.
EULER_MASCHERONI_CONSTANT = 0.5772156649015329


def compute_returns(equity_curve: EquityCurve) -> FloatArray:
    """Period-over-period simple returns: `(equity[i] - equity[i-1]) /
    equity[i-1]`. Length `len(equity_curve) - 1`; empty/single-point curves
    return an empty array."""
    if len(equity_curve) < 2:
        return np.array([], dtype=np.float64)
    equities = np.array([e for _, e in equity_curve], dtype=np.float64)
    previous = equities[:-1]
    current = equities[1:]
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.where(previous != 0.0, (current - previous) / previous, 0.0)
    return returns.astype(np.float64)


def infer_periods_per_year(equity_curve: EquityCurve) -> float:
    """Empirically infer the annualization factor from the equity curve's
    own observed sampling rate (`docs/RESEARCH.md` §7: "annualized using
    the bar frequency the returns series is sampled at") — the number of
    points per calendar year actually observed, rather than assuming a
    fixed bars-per-year constant that may not match the step size the
    caller sampled the curve at (e.g. gold trades ~5.7 days/week, so a
    naive 24/365 assumption overstates annualization for H1-sampled data).
    Returns `0.0` (annualization formulas then read as `0.0`, not a
    division error) if the curve spans less than one day.
    """
    if len(equity_curve) < 2:
        return 0.0
    span_days = (equity_curve[-1][0] - equity_curve[0][0]).total_seconds() / 86400.0
    if span_days < 1.0:
        return 0.0
    return len(equity_curve) / (span_days / 365.25)


def sharpe_ratio(
    returns: FloatArray, *, periods_per_year: float, risk_free_rate: float = 0.0
) -> float:
    """`(mean(returns) - risk_free_rate) / std(returns) * sqrt(periods_per_year)`
    (`docs/RESEARCH.md` §7). Population stddev (`ddof=0`), matching this
    codebase's existing convention (`indicators.math_engine.bollinger_bands()`).
    Returns `0.0` on a zero-variance or empty return series rather than
    raising a division error — a flat/degenerate equity curve has no
    meaningful Sharpe, not an infinite one.
    """
    if len(returns) == 0:
        return 0.0
    std = float(returns.std(ddof=0))
    if std == 0.0:
        return 0.0
    return float((returns.mean() - risk_free_rate) / std * np.sqrt(periods_per_year))


def sortino_ratio(
    returns: FloatArray, *, periods_per_year: float, risk_free_rate: float = 0.0
) -> float:
    """Identical to `sharpe_ratio()` but the denominator uses downside
    deviation only — `std` of `min(return, 0)` returns (`docs/RESEARCH.md`
    §7)."""
    if len(returns) == 0:
        return 0.0
    downside = np.minimum(returns, 0.0)
    downside_std = float(downside.std(ddof=0))
    if downside_std == 0.0:
        return 0.0
    return float((returns.mean() - risk_free_rate) / downside_std * np.sqrt(periods_per_year))


def skewness(returns: FloatArray) -> float:
    """Population (`ddof=0`) sample skewness — `γ₃` in `docs/RESEARCH.md`
    §3's Deflated Sharpe Ratio formula. `0.0` for a zero-variance or
    empty/near-empty return series."""
    if len(returns) < 2:
        return 0.0
    std = float(returns.std(ddof=0))
    if std == 0.0:
        return 0.0
    return float(np.mean(((returns - returns.mean()) / std) ** 3))


def kurtosis(returns: FloatArray) -> float:
    """Population (`ddof=0`), raw (non-excess — a normal distribution
    reads as `3.0`, not `0.0`) sample kurtosis — `γ₄` in `docs/RESEARCH.md`
    §3's Deflated Sharpe Ratio formula, which itself uses `(γ₄ - 1)/4`
    (consistent with the raw convention: a normal distribution gives
    `(3-1)/4 = 0.5`, matching the published Bailey & López de Prado
    formula this section cites). `3.0` (the normal-distribution value, a
    neutral default) for a zero-variance or empty/near-empty series."""
    if len(returns) < 2:
        return 3.0
    std = float(returns.std(ddof=0))
    if std == 0.0:
        return 3.0
    return float(np.mean(((returns - returns.mean()) / std) ** 4))


def deflated_sharpe_ratio(
    sharpe: float,
    *,
    n_observations: int,
    skewness: float,
    kurtosis: float,
    n_trials: int,
    sharpe_variance_across_trials: float,
) -> float:
    """`docs/RESEARCH.md` §3's Deflated Sharpe Ratio — corrects a candidate
    parameter set's realized Sharpe for the fact that the best of `n_trials`
    trials is expected to look good by chance alone:

    ```
    DSR = Φ( ((SR - SR0) * sqrt(n_observations - 1))
             / sqrt(1 - γ3*SR + ((γ4 - 1)/4)*SR**2) )
    SR0 ≈ sqrt(Var[SR]) * ((1-γE)*Φ⁻¹(1-1/N) + γE*Φ⁻¹(1-1/(N*e)))
    ```

    `Φ`/`Φ⁻¹` via `scipy.stats.norm.cdf`/`.ppf`. Returns `0.0` (no
    deflated-skill evidence) on any degenerate input — `n_observations <=
    1`, `n_trials <= 1`, non-positive `sharpe_variance_across_trials`, or a
    non-positive value under the formula's inner square root — rather than
    raising or returning `nan`.
    """
    if n_observations <= 1 or n_trials <= 1 or sharpe_variance_across_trials <= 0.0:
        return 0.0

    sr0 = math.sqrt(sharpe_variance_across_trials) * (
        (1.0 - EULER_MASCHERONI_CONSTANT) * float(norm.ppf(1.0 - 1.0 / n_trials))
        + EULER_MASCHERONI_CONSTANT * float(norm.ppf(1.0 - 1.0 / (n_trials * math.e)))
    )

    denominator_squared = 1.0 - skewness * sharpe + ((kurtosis - 1.0) / 4.0) * sharpe**2
    if denominator_squared <= 0.0:
        return 0.0
    denominator = math.sqrt(denominator_squared)

    z = (sharpe - sr0) * math.sqrt(n_observations - 1) / denominator
    return float(norm.cdf(z))


def cagr(equity_curve: EquityCurve) -> float:
    """Compound annual growth rate over the full curve span. `0.0` if the
    curve spans less than a day or starting equity isn't positive (no
    meaningful annualized growth rate in either case)."""
    if len(equity_curve) < 2:
        return 0.0
    start_time, start_equity = equity_curve[0]
    end_time, end_equity = equity_curve[-1]
    span_days = (end_time - start_time).total_seconds() / 86400.0
    if span_days < 1.0 or start_equity <= 0.0:
        return 0.0
    years = span_days / 365.25
    return float((end_equity / start_equity) ** (1.0 / years) - 1.0)


def max_drawdown(equity_curve: EquityCurve) -> float:
    """`max over t of (running_peak_equity(t) - equity(t)) /
    running_peak_equity(t)` (`docs/RESEARCH.md` §7). `0.0` for an empty
    curve or one that never has a positive peak."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0][1]
    worst = 0.0
    for _, equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0.0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def max_drawdown_duration(equity_curve: EquityCurve) -> timedelta:
    """Longest contiguous span where `equity(t) < running_peak_equity(t)`
    (`docs/RESEARCH.md` §7) — the time from a peak until equity recovers to
    a *new* peak (the standard peak-to-recovery drawdown-duration
    convention), maximized over every such span. A span still underwater
    at the curve's end is measured through the curve's last point."""
    if not equity_curve:
        return timedelta(0)
    peak_value = equity_curve[0][1]
    peak_time = equity_curve[0][0]
    longest = timedelta(0)
    underwater = False
    for time, equity in equity_curve:
        if equity >= peak_value:
            if underwater:
                longest = max(longest, time - peak_time)
                underwater = False
            peak_value = equity
            peak_time = time
        else:
            underwater = True
    if underwater:
        longest = max(longest, equity_curve[-1][0] - peak_time)
    return longest


def mar_ratio(equity_curve: EquityCurve) -> float:
    """`CAGR / abs(Max Drawdown)` (`docs/RESEARCH.md` §7). `0.0` if the
    curve never drew down at all (undefined ratio, not infinite)."""
    drawdown = max_drawdown(equity_curve)
    if drawdown == 0.0:
        return 0.0
    return cagr(equity_curve) / abs(drawdown)


def profit_factor(trade_profits: Sequence[float]) -> float:
    """`sum(winning trade P&L) / abs(sum(losing trade P&L))`
    (`docs/RESEARCH.md` §7). `0.0` for no trades or no losing trades with
    no winners either; `float("inf")` for winners with zero losers (a
    genuinely undefined-denominator case, not modeled as `0.0` since that
    would misrepresent a flawless run as having no edge)."""
    gains = sum(p for p in trade_profits if p > 0.0)
    losses = sum(p for p in trade_profits if p < 0.0)
    if losses == 0.0:
        return float("inf") if gains > 0.0 else 0.0
    return gains / abs(losses)


def win_rate(trade_profits: Sequence[float]) -> float:
    """Fraction of trades with positive P&L, in `[0.0, 1.0]`. `0.0` for no
    trades."""
    if not trade_profits:
        return 0.0
    wins = sum(1 for p in trade_profits if p > 0.0)
    return wins / len(trade_profits)


@dataclass(frozen=True, slots=True)
class PerformanceReport:
    """`docs/API_SPEC.md` §5 shape, `float`-typed (see module docstring).
    `deflated_sharpe_ratio` stays `None` unless the caller supplies
    `n_trials`/`sharpe_variance_across_trials` to `build_performance_report()`
    — DSR requires knowing the number of trials in a parameter sweep
    (`docs/RESEARCH.md` §3); a single historical run (Phase 1) has no
    "sweep" to deflate against, but a walk-forward run (Phase 2,
    `backtester/walk_forward.py`) does.
    """

    period_start_utc: datetime
    period_end_utc: datetime
    sharpe_ratio: float
    sortino_ratio: float
    mar_ratio: float
    max_drawdown_pct: float
    max_drawdown_duration: timedelta
    win_rate_pct: float
    profit_factor: float
    total_trades: int
    deflated_sharpe_ratio: float | None = None


def build_performance_report(
    equity_curve: EquityCurve,
    trade_profits: Sequence[float],
    *,
    n_trials: int | None = None,
    sharpe_variance_across_trials: float | None = None,
) -> PerformanceReport:
    """Convenience wrapper: run every formula above over one equity curve
    + trade P&L list and assemble the `PerformanceReport`. Passing both
    `n_trials` and `sharpe_variance_across_trials` also populates
    `deflated_sharpe_ratio` (`skewness()`/`kurtosis()` computed from the
    same `returns` this call already derives internally); omitting either
    leaves it `None`, matching Phase 1's original (no-sweep) behavior.
    """
    if not equity_curve:
        raise ValueError("equity_curve must not be empty")
    returns = compute_returns(equity_curve)
    periods_per_year = infer_periods_per_year(equity_curve)
    sharpe = sharpe_ratio(returns, periods_per_year=periods_per_year)

    dsr: float | None = None
    if n_trials is not None and sharpe_variance_across_trials is not None:
        dsr = deflated_sharpe_ratio(
            sharpe,
            n_observations=len(returns),
            skewness=skewness(returns),
            kurtosis=kurtosis(returns),
            n_trials=n_trials,
            sharpe_variance_across_trials=sharpe_variance_across_trials,
        )

    return PerformanceReport(
        period_start_utc=equity_curve[0][0],
        period_end_utc=equity_curve[-1][0],
        sharpe_ratio=sharpe,
        sortino_ratio=sortino_ratio(returns, periods_per_year=periods_per_year),
        mar_ratio=mar_ratio(equity_curve),
        max_drawdown_pct=max_drawdown(equity_curve),
        max_drawdown_duration=max_drawdown_duration(equity_curve),
        win_rate_pct=win_rate(trade_profits),
        profit_factor=profit_factor(trade_profits),
        total_trades=len(trade_profits),
        deflated_sharpe_ratio=dsr,
    )
