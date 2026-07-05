"""`CalendarProvider` chain: decouples the economic-calendar feed behind a
unified interface with configurable provider priority, a per-provider
rate limiter, and clean fallback to the next provider on any failure
(`docs/PRODUCTION_SPEC.md` §2).

Provenance note: like `news_engine.fetch_calendar_events()`, the network-
backed providers here (`tradingeconomics`, `finnhub`) assume the same
generic REST/JSON calendar shape — no verified real API contract exists
with either vendor in this codebase (see `news/README.md`). Base URLs are
never hardcoded; they come from `config.calendar_config.CalendarConfig`,
which raises a fatal `ConfigurationError` at boot if a network provider is
listed in the priority chain without its base URL configured.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from config.calendar_config import OFFLINE_PROVIDER_NAME, CalendarConfig
from news.news_engine import (
    EconomicEvent,
    NewsFeedConnectionError,
    fetch_calendar_events,
    parse_calendar_event,
)
from resilience.backoff import RetryBudgetExhaustedError, retry_with_backoff

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0

# A deliberately small retry budget for NetworkCalendarProvider, not
# resilience.backoff's full 5-retry/2s-32s default: this chain's fallback
# (docs/PRODUCTION_SPEC.md §2) must "cleanly transition between active
# providers" quickly, and stacking the full ~62s worst-case backoff budget
# in front of every provider attempt would directly undermine that fast-
# failover guarantee. One quick retry absorbs a single transient blip
# without meaningfully delaying the fall-through to the next provider.
DEFAULT_RETRY_MAX_ATTEMPTS = 1
DEFAULT_RETRY_INITIAL_DELAY_SECONDS = 2.0


@runtime_checkable
class CalendarProvider(Protocol):
    """A single named source of economic calendar events.

    `name` is a read-only `@property` (not a plain attribute) so that
    frozen dataclass implementations (`NetworkCalendarProvider`,
    `OfflineSnapshotCalendarProvider`) satisfy this Protocol structurally —
    mypy treats a plain `name: str` as requiring a settable attribute,
    which a frozen dataclass field is not.
    """

    @property
    def name(self) -> str: ...

    def fetch_events(self, from_utc: datetime, to_utc: datetime) -> list[EconomicEvent]: ...


class RateLimiter:
    """Sliding-window rate limiter: at most `max_calls_per_minute` calls in
    any trailing 60-second window.

    Non-blocking — `allow()` returns `False` immediately rather than
    sleeping when the limit is reached. Callers in this system (the
    `main.py` bar-close loop, indirectly, once wired) operate under a
    200ms processing cap and must never block waiting on a rate limit
    (`docs/PRODUCTION_SPEC.md` §2 / RQ-021).
    """

    WINDOW_SECONDS = 60.0

    def __init__(self, max_calls_per_minute: int) -> None:
        if max_calls_per_minute < 1:
            raise ValueError(f"max_calls_per_minute must be >= 1, got {max_calls_per_minute}")
        self.max_calls_per_minute = max_calls_per_minute
        self._call_times: deque[float] = deque()

    def allow(self, now: float | None = None) -> bool:
        """Records and permits a call if under the limit; otherwise refuses
        it (and does not record it) without blocking."""
        current = now if now is not None else time.monotonic()
        window_start = current - self.WINDOW_SECONDS
        while self._call_times and self._call_times[0] < window_start:
            self._call_times.popleft()
        if len(self._call_times) >= self.max_calls_per_minute:
            return False
        self._call_times.append(current)
        return True


@dataclass(frozen=True, slots=True)
class NetworkCalendarProvider:
    """A `CalendarProvider` backed by a generic REST/JSON HTTP endpoint.
    `base_url` must come from configuration — see this module's
    provenance note.

    `fetch_events()` retries through `resilience.backoff.retry_with_backoff()`
    on a `NewsFeedConnectionError` (`docs/PRODUCTION_SPEC.md` §7) before
    letting the chain fall through to the next provider — a small,
    deliberately-tuned-down budget (see `DEFAULT_RETRY_MAX_ATTEMPTS`'s
    module comment) so a transient blip doesn't stall §2's fast-failover
    guarantee.
    """

    name: str
    base_url: str
    api_key: str
    connect_timeout_seconds: float
    read_timeout_seconds: float
    retry_max_attempts: int = DEFAULT_RETRY_MAX_ATTEMPTS
    retry_initial_delay_seconds: float = DEFAULT_RETRY_INITIAL_DELAY_SECONDS

    def fetch_events(self, from_utc: datetime, to_utc: datetime) -> list[EconomicEvent]:
        try:
            return retry_with_backoff(
                lambda: fetch_calendar_events(
                    self.base_url,
                    self.api_key,
                    from_utc,
                    to_utc,
                    connect_timeout_seconds=self.connect_timeout_seconds,
                    read_timeout_seconds=self.read_timeout_seconds,
                ),
                max_attempts=self.retry_max_attempts,
                initial_delay_seconds=self.retry_initial_delay_seconds,
                retryable_exceptions=(NewsFeedConnectionError,),
            )
        except RetryBudgetExhaustedError as exc:
            # Re-raised as NewsFeedConnectionError (chained) rather than
            # left as RetryBudgetExhaustedError, so CalendarProviderChain's
            # existing except NewsFeedConnectionError fallback contract
            # (docs/PRODUCTION_SPEC.md §2) still catches it and falls
            # through to the next provider.
            raise NewsFeedConnectionError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class OfflineSnapshotCalendarProvider:
    """Reads a local JSON snapshot file — the network-independent final
    fallback in the provider chain. The snapshot is a plain JSON array of
    `{title, country, impact, date}` objects, the same shape
    `news_engine.fetch_calendar_events()` parses from a live feed."""

    snapshot_path: Path
    name: str = OFFLINE_PROVIDER_NAME

    def fetch_events(self, from_utc: datetime, to_utc: datetime) -> list[EconomicEvent]:
        if not self.snapshot_path.exists():
            raise NewsFeedConnectionError(
                f"offline calendar snapshot not found at {self.snapshot_path}"
            )
        try:
            payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise NewsFeedConnectionError(f"offline calendar snapshot unreadable: {exc}") from exc

        events = [parse_calendar_event(item) for item in payload]
        return [event for event in events if from_utc <= event.scheduled_at_utc <= to_utc]


@dataclass
class CalendarProviderChain:
    """Tries each configured `CalendarProvider` in priority order,
    transitioning cleanly to the next on any failure (network fault,
    timeout, or an exhausted per-provider rate limit). Raises
    `NewsFeedConnectionError` only if every provider in the chain fails.
    """

    providers: list[CalendarProvider]
    rate_limiters: dict[str, RateLimiter] = field(default_factory=dict)

    def fetch_events(self, from_utc: datetime, to_utc: datetime) -> list[EconomicEvent]:
        failures: list[str] = []
        for provider in self.providers:
            limiter = self.rate_limiters.get(provider.name)
            if limiter is not None and not limiter.allow():
                failures.append(f"{provider.name}: rate limit exceeded")
                continue
            try:
                return provider.fetch_events(from_utc, to_utc)
            except NewsFeedConnectionError as exc:
                failures.append(f"{provider.name}: {exc}")
                continue
        raise NewsFeedConnectionError(f"all calendar providers exhausted: {'; '.join(failures)}")


def build_calendar_provider_chain(config: CalendarConfig, api_key: str) -> CalendarProviderChain:
    """Builds the provider chain from `config.provider_priority`, applying
    `config.timeout_ms` uniformly to both the connect and read phase of
    each network provider's HTTP request (the spec gives one unified
    timeout, not a separate connect/read pair) and a fresh per-provider
    `RateLimiter(config.rate_limit_per_min)` to every network provider.
    """
    providers: list[CalendarProvider] = []
    rate_limiters: dict[str, RateLimiter] = {}
    read_timeout_seconds = config.timeout_ms / 1000.0
    connect_timeout_seconds = min(read_timeout_seconds, DEFAULT_CONNECT_TIMEOUT_SECONDS)

    for name in config.provider_priority:
        if name == OFFLINE_PROVIDER_NAME:
            providers.append(
                OfflineSnapshotCalendarProvider(snapshot_path=config.offline_snapshot_path)
            )
        else:
            providers.append(
                NetworkCalendarProvider(
                    name=name,
                    base_url=config.provider_base_urls[name],
                    api_key=api_key,
                    connect_timeout_seconds=connect_timeout_seconds,
                    read_timeout_seconds=read_timeout_seconds,
                )
            )
            rate_limiters[name] = RateLimiter(config.rate_limit_per_min)

    return CalendarProviderChain(providers=providers, rate_limiters=rate_limiters)
