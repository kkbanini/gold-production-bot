# ADR-0002: MetaTrader5 Python API Wrapped Behind an Internal BrokerGateway Port

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-07-04 |
| Deciders | Principal Quantitative Engineering |
| Supersedes | — |
| Superseded by | — |
| Related | ADR-0001, ADR-0003 |

## Context

The system's only supported broker/venue integration is the MetaTrader 5 terminal via
the official `MetaTrader5` Python package, which is a thin ctypes binding to a
locally-running MT5 terminal process (Windows-only, stateful, singleton-per-process
connection model). This binding has properties that are hostile to naive direct use
throughout the codebase:

1. **Global mutable connection state.** `mt5.initialize()` / `mt5.shutdown()` operate
   on a process-global handle; there is no client object to inject/mock.
2. **Server time vs. local time skew.** MT5 reports timestamps in the broker
   server's timezone, which is *not* UTC and *not* the host machine's local time, and
   which itself shifts under the broker's DST convention (frequently different from
   the host's DST convention). Every strategy/session/news-window calculation in
   `strategy/` and `news/` depends on correct time alignment; getting this wrong is a
   recurring, high-severity class of bug in retail algo systems.
3. **Untyped, mutable `NamedTuple`-like return values.** `mt5.symbol_info_tick()`,
   `mt5.orders_get()`, etc. return loosely-typed structures whose fields vary by
   broker/symbol configuration (e.g. optional fields present only for certain
   instrument types).
4. **Silent failure mode.** Most `mt5.*` calls return `None` or `False` on failure
   and require a separate `mt5.last_error()` call; exceptions are not raised.
5. **Testability.** Direct calls to `mt5.*` throughout strategy/execution code make
   unit testing impossible without a live terminal, and make the event-driven
   backtester (ADR-0001) unable to share code paths with the live system.

## Decision

All MT5 interaction is isolated behind a single internal port interface,
`BrokerGateway` (see `docs/API_SPEC.md`), implemented in `broker/`. No module other
than `broker/` may import the `MetaTrader5` package. This is a hard architectural
boundary, not a convention.

Concretely:

1. **Port/Adapter (hexagonal) boundary.** `BrokerGateway` is defined as a `Protocol`
   in `docs/API_SPEC.md` / (later) `broker/interface.py`. The production adapter
   (`broker/mt5_gateway.py`, later phase) implements it against the real
   `MetaTrader5` package. The backtester (`backtester/`) implements the same
   `Protocol` against historical data, allowing `strategy/` and `execution/` code to
   be written once and run against either.
2. **Server-time normalization at the boundary.** `BrokerGateway` is responsible for
   resolving and exposing `broker_utc_offset: timedelta` (derived from
   `mt5.symbol_info(symbol).time` vs. `datetime.now(timezone.utc)` at connection
   time, re-validated on a fixed interval to catch DST transitions on the broker
   side). Every timestamp crossing the `BrokerGateway` boundary outward
   (`TickEvent.source_timestamp_utc`, `Bar.close_time_utc`) is normalized to UTC
   *inside* `broker/` before it reaches `strategy/`, `news/`, or `execution/`. No
   other module is permitted to perform broker-timezone arithmetic.
3. **Fail-loud translation layer.** Every `BrokerGateway` method translates MT5's
   `None`/`False` + `mt5.last_error()` failure convention into a typed exception
   hierarchy (`BrokerConnectionError`, `BrokerOrderRejectedError`,
   `BrokerTimeoutError`, `BrokerSymbolUnavailableError` — finalized in Phase 2).
   Nothing downstream of `broker/` ever branches on a bare `None`.
4. **Typed DTOs at the boundary.** `BrokerGateway` methods return the frozen
   dataclasses defined in `docs/API_SPEC.md` (`Tick`, `Bar`, `Position`, `Order`,
   `Fill`, `AccountState`), never raw `MetaTrader5` tuples.
5. **Reconnection is `broker/`'s responsibility, not the core loop's.** Terminal
   disconnects (common with MT5) are retried with exponential backoff inside
   `broker/`; the core loop only ever observes a `BrokerConnectionError` if backoff
   is exhausted, at which point it is treated as a `CRITICAL` risk event per
   `docs/RISK_REGISTER.md`.

## Consequences

### Positive

- `strategy/`, `execution/`, `analytics/` are entirely broker-agnostic and testable
  without a running MT5 terminal or a Windows host.
- The event-driven backtester (`backtester/`) is guaranteed behaviorally consistent
  with live execution because both run against the identical `BrokerGateway`
  `Protocol` and the identical `strategy/`/`execution/` code — the single largest
  source of live/backtest divergence ("backtest-live parity risk") is closed by
  construction.
- Server-time bugs (session windows, news blackout windows, daily-reset boundaries)
  are centralized to one module and one code review surface instead of being
  re-implemented (and re-broken) in every strategy/news component.
- Enables future multi-broker support (e.g. a second FIX/REST venue) as a pure
  addition of a new `BrokerGateway` adapter with zero changes to `strategy/`/
  `execution/`.

### Negative / Accepted Trade-offs

- Adds one layer of indirection and DTO-mapping overhead versus calling `mt5.*`
  directly; accepted because correctness and testability dominate the marginal
  translation cost at this trade frequency.
- The `broker/` module carries the most operationally sensitive code in the system
  (connection lifecycle, time normalization) and must be held to the highest test
  and review bar; `docs/TRACEABILITY_MATRIX.md` weights `broker/` requirements
  accordingly.

## Alternatives Considered

| Alternative | Rejected Because |
|---|---|
| Call `MetaTrader5` package directly from `strategy/`/`execution/` | Makes unit testing and backtest/live parity impossible; couples business logic to a Windows-only ctypes binding. |
| Use MT5's built-in MQL5 Strategy Tester instead of a Python broker abstraction | Forfeits the Python quant stack (numpy/pandas/statsmodels), the offline optimizer design (ADR-0004), and this project's language mandate (Python 3.12+). |
| Normalize time at each call-site (strategy, news, execution independently) | Duplicated, drift-prone logic; exactly the bug class this ADR exists to eliminate. |

## Compliance / Verification

- Enforced via CI import-boundary lint rule (to be added in the Phase 2 CI update):
  no file outside `broker/` and `backtester/` (test double) may contain
  `import MetaTrader5` or `import mt5`.
- `BrokerGateway` `Protocol` and all DTOs frozen in `docs/API_SPEC.md`; any change is
  a MAJOR/MINOR version event per `CHANGELOG.md` policy.
- Server-time normalization behavior specified quantitatively in `docs/RESEARCH.md`
  §"Time & Session Normalization".
