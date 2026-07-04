# broker/

## Responsibility

Sole owner of MetaTrader 5 integration. Implements the `BrokerGateway` `Protocol`
(`docs/API_SPEC.md` §3) against the `MetaTrader5` Python package. Owns connection
lifecycle (connect/reconnect/backoff), server-time-to-UTC normalization
(`broker_utc_offset`), and translation of MT5's `None`/`False`/`last_error()`
failure convention into typed exceptions. No other module (except `backtester/`'s
historical test double, which implements the same Protocol) may import
`MetaTrader5`.

## Depends On

`config/` (credentials, account allowlist). External: `MetaTrader5` package,
locally-running MT5 terminal process.

## Depended On By

`core` (tick/bar producer), `strategy/` (bar history via `get_bars`),
`execution/` (order submission, position queries), `analytics/` (account state).

## Governing Docs

ADR-0002 (MT5 broker gateway abstraction). `docs/RISK_REGISTER.md` RR-002
(disconnects), RR-003 (time skew), RR-012 (wrong-account connection).
`docs/RESEARCH.md` §2 (Time & Session Normalization).

## Non-Goals (This Phase)

No code exists yet. `BrokerGateway` MT5 adapter implementation, reconnect/backoff
logic, and `tests/broker/test_import_boundary.py` /
`tests/broker/test_time_normalization.py` (RQ-001, RQ-002) land in Phase 2.
