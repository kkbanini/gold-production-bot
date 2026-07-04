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

## Provenance note — no calendar provider was ever named

Neither the phase directive, `docs/DEPLOYMENT.md`, nor
`config/.env.template` (which only declares a generic
`ECONOMIC_CALENDAR_API_KEY`) names a specific calendar vendor. This module
assumes a generic REST/JSON shape (`GET {base_url}?from=...&to=...`
returning a JSON list of `{title, country, impact, date}` objects) rather
than targeting one concrete provider's real response format — flagged for
review. `_parse_event()` is the only function that would need to change
to match a specific vendor's actual schema once one is chosen.

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

`config/` (calendar provider API key, `ECONOMIC_CALENDAR_API_KEY`).
External: `requests` (HTTP client — the only network-calling dependency in
this module).

## Depended On By

`risk/` and `execution/` (future integration point for
`apply_news_feed_fail_safe()`'s adjusted risk/spread values), the eventual
FSM orchestration loop (`main.py`, blackout check before every new-trade
decision).

## Governing Docs

`docs/API_SPEC.md` §2 (`NewsWindow` shape — this phase's `EconomicEvent` /
blackout-window pair covers the same intent with a concretely implemented,
narrower shape). `docs/RISK_REGISTER.md` RR-009 (news-event trading risk).

## Non-Goals (This Phase)

No automated `tests/news/` suite yet — verification this phase was ad hoc
against a faked `requests.get` (no live calendar provider credentials
exist in this environment; see `CHANGELOG.md` §0.8.0), consistent with the
project's plan to introduce the full automated test harness in a
dedicated later phase. Wiring `apply_news_feed_fail_safe()`'s output into
`risk/risk_manager.py`'s actual lot-sizing call and `execution/`'s actual
spread guard is deferred — this phase defines the fail-safe function
itself, not its integration into the live decision path.
