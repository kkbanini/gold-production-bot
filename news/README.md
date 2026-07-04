# news/

## Responsibility

Polls an economic calendar provider and emits `NewsWindow` events
(`docs/API_SPEC.md` §2) for high-impact releases (e.g. FOMC, NFP, CPI) relevant
to XAUUSD (primarily USD- and real-rate-driven releases). Defines
`blackout_before`/`blackout_after` windows consumed by `execution/`'s pre-trade
risk gate to block or derisk around the release (RQ-017, RR-009). All
`scheduled_at_utc` timestamps are UTC per the same normalization discipline as
`broker/` (ADR-0002) — this module does not perform its own independent
timezone arithmetic against provider-local time without going through the same
UTC-normalization discipline.

## Depends On

`config/` (calendar provider API key). External: third-party economic calendar
API (provider TBD, selected in the phase this module is implemented).

## Depended On By

`execution/` (consumes active `NewsWindow` events for the blackout check).

## Governing Docs

`docs/API_SPEC.md` §2 (`NewsWindow` shape). `docs/RISK_REGISTER.md` RR-009
(news-event trading risk).

## Non-Goals (This Phase)

No code exists yet. Calendar polling implementation, provider selection, and
`tests/news/test_blackout_enforcement.py` (RQ-017) land in Phase 9.
