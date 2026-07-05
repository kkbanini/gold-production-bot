# resilience/

## Responsibility

Cross-cutting network resiliency (Phase 11e, `docs/PRODUCTION_SPEC.md`
§7's "Bifurcated Resiliency" bullet — network I/O gets exponential
backoff with a retry budget; SQLite gets `busy_timeout` + atomic
rollback instead, owned by `storage/`, not here). Not part of the
original Phase 0 module scaffold — carved out the same way `risk/` was
in Phase 6, for a concern that doesn't belong to any single existing
package.

## Implementation

`backoff.py`:

- `compute_backoff_delays(max_attempts=5, initial_delay_seconds=2.0)` —
  pure: returns the doubling delay sequence, defaulting to the spec's
  exact `(2.0, 4.0, 8.0, 16.0, 32.0)`.
- `retry_with_backoff(operation, *, max_attempts=5, initial_delay_seconds=2.0, retryable_exceptions=(Exception,), sleep=time.sleep)`
  — calls `operation()`, retrying up to `max_attempts` additional times
  (a "retry budget") on a matching exception, sleeping the corresponding
  backoff delay between each. `max_attempts` counts *retries after the
  initial attempt* (so the default of 5 permits up to 6 total attempts,
  consuming all 5 listed delay values rather than leaving the last one,
  32s, always unused under a "5 total tries" reading). `sleep` is
  injectable for deterministic tests. Raises `RetryBudgetExhaustedError`
  (chained from the final attempt's real exception) once the budget is
  exhausted.

## Applied to `news/calendar_provider.py`'s `NetworkCalendarProvider`

The one call site this phase wires it into. Deliberately overridden down
to a small budget (`retry_max_attempts=1`, `retry_initial_delay_seconds=2.0`
— one quick retry, not the full 5/2s-32s default): `CalendarProviderChain`
(Phase 11b, `docs/PRODUCTION_SPEC.md` §2) must "cleanly transition between
active providers" on a fault, and stacking a ~62-second worst-case backoff
budget in front of every provider attempt would directly undermine that
fast-failover guarantee. `NetworkCalendarProvider.fetch_events()` catches
`RetryBudgetExhaustedError` and re-raises as `NewsFeedConnectionError`
(chained) so `CalendarProviderChain`'s existing exception contract still
catches it and falls through to the next provider.

## Depends On

Nothing internal beyond the standard library (`time`, `typing`).

## Depended On By

`news/calendar_provider.py`'s `NetworkCalendarProvider` (Phase 11e).
`broker/mt5_gateway.py`'s `MT5Gateway.connect()` deliberately does **not**
depend on this module — see the Flagged note below.

## Flagged — `MT5Gateway.connect()` was not refactored onto this module

`connect()` already had its own independently-tuned, already-tested
exponential backoff since Phase 3 (RR-002) — a different default cadence
(delay doubling from 1.0s, `max_attempts` counting total tries rather
than retries-after-the-first) and an additional `max_delay_seconds` cap
this module doesn't have. Refactoring already-verified reconnect
behavior onto a new shared abstraction, purely for the sake of
de-duplication, would add real risk (two different retry-counting
conventions to reconcile) for no behavioral benefit, since `connect()`
isn't broken. Flagged in case unifying them was actually intended.

## Governing Docs

`docs/PRODUCTION_SPEC.md` §7 (Bifurcated Resiliency). No dedicated ADR
(cross-cutting, like `risk/`).

## Non-Goals (This Phase)

No retry loop exists yet for MT5 order submission — `execution/validation.py`'s
`check_duplicate_order_before_retry()` (Phase 11c) and
`broker.mt5_gateway.MT5Gateway.is_ticket_still_open()` are ready to guard
one, but `broker/mt5_gateway.py`'s `submit_market_order()`/
`submit_position_action()` currently fold both "MT5 explicitly rejected
this" and "MT5 returned nothing (ambiguous — could be a transient
timeout)" into the same `BrokerOrderRejectedError`. Wrapping either call
in `retry_with_backoff()` today would retry indiscriminately, including
genuine, permanent rejections (e.g. invalid stops) that a retry can never
fix — a real correctness risk, not just an unfinished feature. Building
this safely requires first distinguishing "ambiguous/timeout" from
"explicit rejection" in `broker/mt5_gateway.py`'s exception model, which
this phase does not do; see `docs/ARCHITECTURE_SUMMARY.md` §5.
