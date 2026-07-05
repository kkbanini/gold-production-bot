# news/

## Responsibility

Connects to an economic calendar feed, classifies core macro events
(NFP/CPI/FOMC), enforces a ±30-minute trade-entry blackout window around
them, and defines the News-API-down defensive fail-safe (half risk size,
double spread tolerance) for when the feed is unreachable.

## Implementation

`news_engine.py`:

- `fetch_calendar_events(base_url, api_key, from_utc, to_utc, ...)` — GETs
  the calendar feed with independent connect/read timeouts (`requests`'
  `(connect, read)` tuple form, defaults 5s/10s), parses the JSON response
  into `EconomicEvent` objects. Raises `NewsFeedConnectionError` on any
  connection failure, timeout, non-2xx response, or invalid JSON — never
  returns a partial/guessed result.
- `EconomicEvent.is_core_macro_event` — `True` if the event's title
  contains `NFP`/`NON-FARM`/`NONFARM`/`CPI`/`FOMC` (case-insensitive).
- `is_trade_entry_locked(now_utc, events)` — `True` if `now_utc` falls
  within ±30 minutes (`MACRO_BLACKOUT_WINDOW`, inclusive on both
  boundaries) of any core macro event in `events`. Non-core events never
  trigger a lock regardless of their provider-reported impact level.
- `apply_news_feed_fail_safe(base_risk_lots, base_spread_limit_points, feed_state)`
  — when `feed_state.is_healthy` is `False`, halves risk size and doubles
  the spread tolerance limit (see "Flagged" below for why doubling, not
  halving, the spread limit is the literal-correct reading here);
  otherwise returns both inputs unchanged.

`calendar_provider.py` (Phase 11b, `docs/PRODUCTION_SPEC.md` §2):

- `CalendarProvider` — a `Protocol` (`name` + `fetch_events(from_utc, to_utc)`)
  every provider in the chain implements.
- `NetworkCalendarProvider` — wraps `news_engine.fetch_calendar_events()`
  with a configured `base_url`/`api_key`; used for `tradingeconomics` and
  `finnhub`. `fetch_events()` retries a `NewsFeedConnectionError` through
  `resilience.backoff.retry_with_backoff()` (Phase 11e,
  `docs/PRODUCTION_SPEC.md` §7) before letting the chain fall through to
  the next provider — deliberately a small budget
  (`retry_max_attempts=1`, one 2s delay), not `resilience.backoff`'s full
  5-retry/2s–32s default, so a transient blip doesn't stall §2's "clean
  transition between providers" guarantee with a ~62s worst case. Catches
  `RetryBudgetExhaustedError` and re-raises as `NewsFeedConnectionError`
  (chained) so `CalendarProviderChain`'s existing fallback contract still
  applies.
- `OfflineSnapshotCalendarProvider` — reads a local JSON snapshot file
  (`news/offline_calendar_snapshot.json` by default, currently an empty
  `[]`), filtering parsed events into `[from_utc, to_utc]`. The
  network-independent final fallback, and the only provider enabled by
  default.
- `RateLimiter` — a non-blocking sliding-window limiter (`allow()` returns
  `False` immediately rather than sleeping once `max_calls_per_minute` is
  reached within the trailing 60 seconds); one instance per network
  provider.
- `CalendarProviderChain.fetch_events()` — tries each provider in priority
  order, falling through to the next on a `NewsFeedConnectionError` or an
  exhausted rate limit; raises `NewsFeedConnectionError` only once every
  provider has failed.
- `build_calendar_provider_chain(config, api_key)` — constructs the chain
  from a `config.calendar_config.CalendarConfig`.

## Provenance note — no calendar provider was ever named

Neither the phase directive, `docs/DEPLOYMENT.md`, nor
`config/.env.template` (which only declares a generic
`ECONOMIC_CALENDAR_API_KEY`) names a specific calendar vendor. This module
assumes a generic REST/JSON shape (`GET {base_url}?from=...&to=...`
returning a JSON list of `{title, country, impact, date}` objects) rather
than targeting one concrete provider's real response format — flagged for
review. `parse_calendar_event()` is the only function that would need to
change to match a specific vendor's actual schema once one is chosen.
`calendar_provider.py`'s `NetworkCalendarProvider` inherits this same
assumption for both `tradingeconomics` and `finnhub`; per-provider
`base_url`s come from `config.calendar_config.CalendarConfig`
(`CALENDAR_<PROVIDER>_BASE_URL` env vars) rather than a hardcoded guess,
since no real endpoint for either vendor has been verified in this
codebase.

## Flagged — "double spread limits" interpreted literally

"Spread limit" is implemented as the maximum spread (points) tolerated
before a trade entry would be rejected. Doubling it, taken literally, makes
the filter *more* permissive, not less — which reads as counterintuitive
for a "defensive circuit breaker." The alternative reading (halving the
limit, i.e. becoming stricter) is arguably more conventionally "defensive,"
but contradicts the directive's literal word "double." This implementation
follows the literal instruction and documents the resulting model
explicitly: reduced position size (halved) compensates for a wider
accepted spread band, keeping the system in a smaller, more tolerant
degraded operating mode rather than halting trading outright (no hard
halt was requested). Flagged for correction if the intended direction was
actually to tighten the spread tolerance.

## Depends On

`config/` (calendar provider API key, `ECONOMIC_CALENDAR_API_KEY`;
`config/calendar_config.py`'s `CalendarConfig` for
`calendar_provider.py`'s provider priority/timeouts/rate limit/base URLs),
`resilience/` (Phase 11e: `calendar_provider.py`'s
`retry_with_backoff()`/`RetryBudgetExhaustedError`). External: `requests`
(HTTP client — the only network-calling dependency in this module).

## Depended On By

`container.py`'s `ApplicationContainer` (Phase 11b: constructs the
`CalendarProviderChain` via `build_calendar_provider_chain()` and holds it
as `calendar_provider`). `risk/` and `execution/` (future integration point
for `apply_news_feed_fail_safe()`'s adjusted risk/spread values), the
eventual FSM orchestration loop (`main.py`, blackout check before every
new-trade decision — **not yet wired**, see `docs/ARCHITECTURE_SUMMARY.md`
§5: the container holds a working `calendar_provider`, but `main.py`'s
loop still calls `_fetch_market_snapshot()` with a hardcoded empty event
list).

## Governing Docs

`docs/API_SPEC.md` §2 (`NewsWindow` shape — this phase's `EconomicEvent` /
blackout-window pair covers the same intent with a concretely implemented,
narrower shape). `docs/PRODUCTION_SPEC.md` §2 (`calendar_provider.py`'s
`CalendarProvider`/`CalendarProviderChain`, Phase 11b). `docs/RISK_REGISTER.md`
RR-009 (news-event trading risk).

## Non-Goals (This Phase)

No automated `tests/news/` suite yet — verification this phase was ad hoc
against a faked `requests.get` (no live calendar provider credentials
exist in this environment; see `CHANGELOG.md` §0.8.0), consistent with the
project's plan to introduce the full automated test harness in a
dedicated later phase. Wiring `apply_news_feed_fail_safe()`'s output into
`risk/risk_manager.py`'s actual lot-sizing call and `execution/`'s actual
spread guard is deferred — this phase defines the fail-safe function
itself, not its integration into the live decision path. (Phase 9 later
added formal `tests/test_unit.py::TestNewsEnginePureLogic` and
`tests/test_integration.py::TestNewsFeedSocketDisconnections` coverage,
superseding this note for `news_engine.py` itself.)

Phase 11b's `calendar_provider.py` is formally unit-tested
(`tests/test_unit.py::TestRateLimiter`, `TestOfflineSnapshotCalendarProvider`,
`TestCalendarProviderChain`, `TestBuildCalendarProviderChain`) and its
container wiring is integration-tested
(`tests/test_integration.py::TestApplicationContainer::test_build_wires_calendar_and_clock_providers_with_defaults`/
`test_build_propagates_calendar_config_error_uncaught`).
`NetworkCalendarProvider`'s actual HTTP path is not separately re-tested —
it's a thin wrapper delegating directly to `news_engine.fetch_calendar_events()`,
already covered by `TestNewsFeedSocketDisconnections`.
