"""Economic calendar client: connects to a news calendar feed with custom
connect/read timeouts, classifies core macro events (NFP/CPI/FOMC), and
enforces a trade-entry blackout window around them. Also defines the
News-API-down fail-safe: automatically halves risk size and doubles the
spread tolerance limit when the calendar feed becomes unreachable.

Provenance note: no specific calendar provider is named anywhere in this
project (docs/DEPLOYMENT.md and config/.env.template only declare a
generic ECONOMIC_CALENDAR_API_KEY). This module assumes a generic REST/JSON
calendar API shape (GET {base_url}?from=...&to=... returning a JSON list of
{title, country, impact, date} objects) rather than targeting one specific
named vendor — flagged for review; adapt `parse_calendar_event()` to a
concrete provider's real response shape once one is chosen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 10.0

MACRO_EVENT_KEYWORDS: tuple[str, ...] = ("NFP", "NON-FARM", "NONFARM", "CPI", "FOMC")
MACRO_BLACKOUT_WINDOW = timedelta(minutes=30)

NEWS_FEED_DOWN_RISK_MULTIPLIER = 0.5
NEWS_FEED_DOWN_SPREAD_MULTIPLIER = 2.0


class NewsFeedConnectionError(Exception):
    """Raised when the economic calendar feed cannot be reached: connection
    failure, timeout, non-2xx response, or an unparseable body. Never
    returns a partial/guessed event list on failure."""


@dataclass(frozen=True, slots=True)
class EconomicEvent:
    """A single economic calendar entry."""

    title: str
    country: str
    impact: str
    scheduled_at_utc: datetime

    @property
    def is_core_macro_event(self) -> bool:
        """True if this event's title matches one of the core macro
        indicators this system locks trading around (NFP, CPI, FOMC)."""
        upper_title = self.title.upper()
        return any(keyword in upper_title for keyword in MACRO_EVENT_KEYWORDS)


@dataclass(frozen=True, slots=True)
class NewsFeedHealthState:
    """Tracks whether the economic calendar feed is currently reachable."""

    is_healthy: bool
    last_successful_fetch_utc: datetime | None
    last_error: str | None


def fetch_calendar_events(
    base_url: str,
    api_key: str,
    from_utc: datetime,
    to_utc: datetime,
    *,
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
) -> list[EconomicEvent]:
    """Fetch economic calendar events in `[from_utc, to_utc]`.

    Uses separate connect/read timeouts (`requests`' `(connect, read)`
    tuple form) rather than a single blanket timeout, so a slow-to-respond
    provider (read phase) and an unreachable host (connect phase) are each
    bounded independently. Raises `NewsFeedConnectionError` on any
    connection failure, timeout, non-2xx response, or invalid JSON body —
    never returns a partial/guessed result.
    """
    if from_utc.tzinfo is None or to_utc.tzinfo is None:
        raise ValueError("from_utc and to_utc must be timezone-aware")

    try:
        response = requests.get(
            base_url,
            params={
                "api_key": api_key,
                "from": from_utc.isoformat(),
                "to": to_utc.isoformat(),
            },
            timeout=(connect_timeout_seconds, read_timeout_seconds),
        )
    except requests.exceptions.RequestException as exc:
        raise NewsFeedConnectionError(f"economic calendar feed unreachable: {exc}") from exc

    if response.status_code != 200:
        raise NewsFeedConnectionError(
            f"economic calendar feed returned HTTP {response.status_code}: "
            f"{response.text[:200]!r}"
        )

    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise NewsFeedConnectionError(
            f"economic calendar feed returned invalid JSON: {exc}"
        ) from exc

    return [parse_calendar_event(item) for item in payload]


def parse_calendar_event(item: dict[str, Any]) -> EconomicEvent:
    """Parse one raw calendar-feed JSON object into an `EconomicEvent`.

    Public (not module-private) because `news/calendar_provider.py`'s
    offline-snapshot fallback parses the same `{title, country, impact,
    date}` shape from a local file rather than a live HTTP response.
    """
    scheduled_at = datetime.fromisoformat(item["date"])
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    return EconomicEvent(
        title=str(item["title"]),
        country=str(item.get("country", "")),
        impact=str(item.get("impact", "")),
        scheduled_at_utc=scheduled_at.astimezone(timezone.utc),
    )


def is_trade_entry_locked(
    now_utc: datetime,
    events: list[EconomicEvent],
    *,
    blackout_window: timedelta = MACRO_BLACKOUT_WINDOW,
) -> bool:
    """True if `now_utc` falls within ±`blackout_window` (default 30
    minutes) of any core macro event (NFP/CPI/FOMC) in `events`. Non-core
    events never trigger a lock, regardless of their provider-reported
    impact level.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    for event in events:
        if not event.is_core_macro_event:
            continue
        window_start = event.scheduled_at_utc - blackout_window
        window_end = event.scheduled_at_utc + blackout_window
        if window_start <= now_utc <= window_end:
            return True
    return False


def apply_news_feed_fail_safe(
    base_risk_lots: float,
    base_spread_limit_points: float,
    feed_state: NewsFeedHealthState,
    *,
    risk_multiplier_on_failure: float = NEWS_FEED_DOWN_RISK_MULTIPLIER,
    spread_multiplier_on_failure: float = NEWS_FEED_DOWN_SPREAD_MULTIPLIER,
) -> tuple[float, float]:
    """Defensive circuit breaker for a down/unreachable news calendar feed.

    Without calendar visibility the system cannot anticipate upcoming
    high-impact events, so per the phase directive it (1) halves the risk
    size and (2) doubles the spread tolerance limit — the system keeps
    operating in a smaller, more spread-tolerant degraded mode rather than
    halting trading outright, since no hard trading halt was requested.
    Returns `(base_risk_lots, base_spread_limit_points)` unchanged when
    `feed_state.is_healthy` is `True`.
    """
    if feed_state.is_healthy:
        return base_risk_lots, base_spread_limit_points
    return (
        base_risk_lots * risk_multiplier_on_failure,
        base_spread_limit_points * spread_multiplier_on_failure,
    )
