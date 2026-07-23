"""Historical bar fetching + gap/quality audit for the backtester
(`docs/RESEARCH.md` §1's mandatory data-quality gate).

`audit_bar_series()` is pure (no I/O); `fetch_audited_history()` is the
thin impure wrapper that calls `MT5Gateway.get_bars_range()` then audits
the result before handing it to the simulator — a WFO/backtest report
built on unaudited data is invalid by definition per RESEARCH.md §1, so
this module never returns unaudited bars silently.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from broker.mt5_gateway import (
    WEEKEND_CLOSE_HOUR_UTC,
    WEEKEND_CLOSE_WEEKDAY,
    WEEKEND_REOPEN_HOUR_UTC,
    WEEKEND_REOPEN_WEEKDAY,
    BarSeries,
    MT5Gateway,
)

logger = logging.getLogger(__name__)

# RESEARCH.md §1: "gaps exceeding 2 x timeframe duration outside known
# weekend/holiday closures" fail the audit.
MAX_GAP_MULTIPLE = 2

# No per-country market holiday calendar exists in this codebase (only
# the weekly weekend closure is modeled precisely — `_overlaps_weekend_closure()`).
# A real gap that misses the weekend window is still very plausibly an
# ordinary single-day/multi-day holiday closure (confirmed live: a real
# 2023-2026 XAUUSD H4 fetch has exactly this shape around Christmas 2024
# — Tuesday 2024-12-24 20:00 UTC to Thursday 2024-12-26 00:00 UTC, neither
# a weekend nor a data error). Gaps under this bound that miss the
# weekend window are logged, not treated as an audit failure; only a gap
# at or beyond this scale halts with an error, since that can no longer
# plausibly be an ordinary holiday and more likely indicates a real
# broker-side data gap.
MAX_PLAUSIBLE_HOLIDAY_GAP = timedelta(days=4)


def _overlaps_weekend_closure(previous: datetime, current: datetime) -> bool:
    """True iff the open interval `(previous, current)` overlaps *any*
    Friday-22:00-UTC-through-Sunday-22:00-UTC closure window.

    Checking `is_weekend_market_closed()` against `previous`/`current`
    themselves (the bars bracketing the gap) is NOT equivalent to this:
    the bracketing bars typically sit just *outside* the closure window
    (the last pre-close bar, the first post-reopen bar), so neither one
    individually reads as "closed." What actually needs checking is
    whether the *gap itself* overlaps the window — computed here by
    building the candidate closure window anchored to each of
    `previous`/`current`'s own week and testing standard interval overlap.
    """
    for anchor in (previous, current):
        days_since_friday = (anchor.weekday() - WEEKEND_CLOSE_WEEKDAY) % 7
        friday_close = (anchor - timedelta(days=days_since_friday)).replace(
            hour=WEEKEND_CLOSE_HOUR_UTC, minute=0, second=0, microsecond=0
        )
        reopen_offset_days = (WEEKEND_REOPEN_WEEKDAY - WEEKEND_CLOSE_WEEKDAY) % 7
        sunday_reopen = (friday_close + timedelta(days=reopen_offset_days)).replace(
            hour=WEEKEND_REOPEN_HOUR_UTC
        )
        if previous < sunday_reopen and current > friday_close:
            return True
    return False


def audit_bar_series(bars: BarSeries, timeframe_minutes: int) -> None:
    """Raises `ValueError` on the first violation found (RESEARCH.md §1):
    (a) non-monotonic timestamps, (b) duplicate bars, (c) a gap at or
    beyond `MAX_PLAUSIBLE_HOLIDAY_GAP`. A gap exceeding
    `MAX_GAP_MULTIPLE x timeframe duration` that isn't explained by the
    weekly weekend closure (`_overlaps_weekend_closure()`) but stays under
    `MAX_PLAUSIBLE_HOLIDAY_GAP` is logged as a probable holiday closure,
    not raised — see `MAX_PLAUSIBLE_HOLIDAY_GAP`'s module comment.
    """
    times = bars.time_utc
    if len(times) < 2:
        return

    timeframe_duration = timedelta(minutes=timeframe_minutes)
    max_gap = timeframe_duration * MAX_GAP_MULTIPLE

    for i in range(1, len(times)):
        previous, current = times[i - 1], times[i]
        if current <= previous:
            raise ValueError(
                f"non-monotonic or duplicate bar timestamps at index {i}: "
                f"{previous} then {current}"
            )
        gap = current - previous
        if gap <= max_gap or _overlaps_weekend_closure(previous, current):
            continue
        if gap >= MAX_PLAUSIBLE_HOLIDAY_GAP:
            raise ValueError(
                f"unexplained gap of {gap} between bars at index {i - 1} ({previous}) "
                f"and {i} ({current}); exceeds the {MAX_PLAUSIBLE_HOLIDAY_GAP} plausible-"
                "holiday bound and doesn't overlap the weekend closure window"
            )
        logger.warning(
            "Bar gap of %s between %s and %s doesn't overlap the weekly weekend "
            "closure window — treating as a probable holiday closure (no holiday "
            "calendar in this codebase to confirm precisely); proceeding since it's "
            "under the %s plausibility bound.",
            gap,
            previous,
            current,
            MAX_PLAUSIBLE_HOLIDAY_GAP,
        )


def fetch_audited_history(
    gateway: MT5Gateway,
    timeframe: int,
    timeframe_minutes: int,
    start_utc: datetime,
    end_utc: datetime,
) -> BarSeries:
    """Fetch `[start_utc, end_utc]` via `gateway.get_bars_range()` then
    audit it. Always a real, connected `MT5Gateway` — this module fetches
    *from* the broker's history server; `HistoricalReplayGateway`
    (`backtester/replay_gateway.py`) is the *consumer* of the audited
    result, never the source.
    """
    bars = gateway.get_bars_range(timeframe, start_utc, end_utc)
    audit_bar_series(bars, timeframe_minutes)
    return bars
