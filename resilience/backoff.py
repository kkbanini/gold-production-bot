"""Exponential backoff with a strict retry budget for network I/O
(`docs/PRODUCTION_SPEC.md` §7): "Network I/O operations must implement
Exponential Backoff (Max 5 attempts: 2s, 4s, 8s, 16s, 32s)."

This is the one canonical, reusable mechanism any *new* network-retry call
site in this codebase should use. It is deliberately generic — no
dependency on `requests`, `MetaTrader5`, or any specific transport — so it
can wrap any operation that raises on failure.

Scope note: `broker/mt5_gateway.py`'s `MT5Gateway.connect()` already had
its own independently-tuned, already-tested exponential backoff since
Phase 3 (RR-002) — a different default cadence (delay doubling from 1.0s,
`max_attempts` counting total tries rather than retries-after-the-first).
It is deliberately left as-is here rather than retrofitted onto this
module: refactoring already-verified reconnect behavior carries real risk
for no behavioral benefit, since it isn't broken. This module is applied
instead to `news/calendar_provider.py`'s `NetworkCalendarProvider` (a new
retry call site this phase adds).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_INITIAL_DELAY_SECONDS = 2.0

T = TypeVar("T")


class RetryBudgetExhaustedError(Exception):
    """Raised when every attempt (the initial call plus every retry in the
    budget) failed. Chains the final attempt's exception so the real
    root cause is never lost."""


def compute_backoff_delays(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS,
) -> tuple[float, ...]:
    """The delay-before-each-retry sequence: `initial_delay_seconds`
    doubling `max_attempts` times. Defaults to the spec's exact
    `(2.0, 4.0, 8.0, 16.0, 32.0)`.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    if initial_delay_seconds <= 0:
        raise ValueError(f"initial_delay_seconds must be > 0, got {initial_delay_seconds}")
    return tuple(initial_delay_seconds * (2**i) for i in range(max_attempts))


def retry_with_backoff(
    operation: Callable[[], T],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call `operation()`, retrying up to `max_attempts` additional times
    (the "retry budget") on any exception matching `retryable_exceptions`,
    sleeping `compute_backoff_delays()[attempt]` between each retry.

    `max_attempts` counts *retries after the initial attempt* — the
    default of 5 therefore permits up to 6 total attempts, consuming all
    5 of the spec's listed delay values (2s, 4s, 8s, 16s, 32s) rather than
    leaving the last one (32s) always unused, as a "5 total tries, 4
    delays" reading would.

    `sleep` is injectable so tests can assert on the delay sequence
    without a real wall-clock wait. Raises `RetryBudgetExhaustedError`
    (chained from the final attempt's exception) once the budget is
    exhausted — never silently swallows a persistent failure.
    """
    delays = compute_backoff_delays(max_attempts, initial_delay_seconds)
    last_exception: Exception | None = None
    for attempt in range(max_attempts + 1):
        try:
            return operation()
        except retryable_exceptions as exc:
            last_exception = exc
            if attempt < max_attempts:
                sleep(delays[attempt])
    raise RetryBudgetExhaustedError(
        f"operation failed after {max_attempts + 1} attempts (retry budget exhausted)"
    ) from last_exception
