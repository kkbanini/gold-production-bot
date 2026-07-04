# broker/

## Responsibility

Sole owner of MetaTrader 5 integration. No other module (except
`backtester/`'s future historical test double) may import `MetaTrader5`.

## Implementation

- `mt5_gateway.py`:
  - `resolve_gold_symbol()` — dynamic Gold symbol matching. Tries
    `GOLD_SYMBOL_CANDIDATES` (`XAUUSD`, `XAUUSD.m`, `XAUUSD.a`, `XAUUSDm`,
    `XAUUSD_i`, `GOLD`, `GOLD.m`, `GOLDm`) in priority order against
    `mt5.symbol_info()`, falling back to `mt5.symbol_select()` if a matched
    symbol exists but isn't visible in Market Watch. Returns a `SymbolSpec`
    with point size, digits, tick value/size, and volume constraints read
    directly from the broker — never hardcoded. Raises
    `BrokerSymbolUnavailableError` if no candidate is found.
  - `is_within_execution_window(now_utc)` — the 07:00–22:00 GMT execution
    filter. Requires a timezone-aware `datetime` (raises `ValueError`
    otherwise); GMT and UTC share the same civil time year-round (GMT
    observes no DST), so no further conversion is applied
    (`docs/RESEARCH.md` §2).
  - `MT5Gateway` — connection lifecycle:
    - `connect()` retries `mt5.initialize()` with exponential backoff
      (delay doubles each attempt, capped at `max_delay_seconds`, up to
      `max_attempts`), raising `BrokerConnectionError` with the last
      `mt5.last_error()` detail if every attempt fails (RR-002). On
      success, resolves `symbol_spec` and `broker_utc_offset` before
      returning, so a caller never observes a half-initialized gateway.
    - `broker_utc_offset` is computed from the resolved Gold symbol's
      latest tick timestamp (`mt5.symbol_info_tick`) minus host UTC time —
      not from `terminal_info()` — since tick timestamps are what
      session/news-window logic actually compares against downstream
      (ADR-0002).
    - `get_open_positions_by_magic()` / `audit_open_positions()` — the
      position-recovery path. Filters `mt5.positions_get()` by this
      gateway's magic number, then reconciles against the locally
      persisted `trade_ledger` (via `storage.state_manager.TradeLedgerEntry`,
      matched on the `broker_ticket` column added this phase). Returns a
      `PositionAuditReport` with `reconciled_tickets`, `broker_only_positions`
      (open on the broker, no matching ledger row — RR-008), and
      `ledger_only_entries` (the ledger thinks it's open, the broker
      disagrees — e.g. closed by SL/TP while the bot was down). This
      function only detects and reports divergence; resolving it is a
      caller responsibility (`docs/RUNBOOK.md` §1 step 5, §3.1).

Order submission/cancellation and the remaining `docs/API_SPEC.md` §3
`BrokerGateway` methods (`submit_order`, `cancel_order`, `close_position`,
`get_account_state`) are not implemented yet — they land alongside
`execution/`.

## Verification note (no live terminal in this environment)

`MetaTrader5` requires a running MT5 terminal and real broker credentials to
actually connect; neither exists in this development environment. Verified
instead: `ruff check`, `ruff format --check`, and `mypy --strict .` all pass
against the installed `MetaTrader5` package (see `CHANGELOG.md` §0.4.0 for
the exact version note — the version pinned in `requirements.txt`,
`5.0.4500`, is not published on PyPI; `5.0.5488`, the oldest available, was
installed locally for import resolution and type-checking only). All
connection-independent logic — symbol resolution priority/fallback, the GMT
window's boundary hours, the backoff delay sequence and its exhaustion path,
and the audit/reconciliation logic — was exercised ad hoc against a fake
`MetaTrader5` module substituted in place of the real one, covering both
success and failure paths for each function.

## Depends On

`config/` (credentials, `ENVIRONMENT_MODE`), `storage/` (`TradeLedgerEntry`
for position-audit reconciliation). External: `MetaTrader5` package,
locally-running MT5 terminal process.

## Depended On By

The eventual FSM orchestration loop (`main.py`, connection lifecycle +
startup position reconciliation), `strategy/` (bar history, future phase),
`execution/` (order submission, position queries, future phase).

## Governing Docs

ADR-0002 (MT5 broker gateway abstraction). `docs/RISK_REGISTER.md` RR-002
(disconnects), RR-003 (time skew), RR-007 (duplicate orders — the ticket-based
audit is part of this defense), RR-008 (event log / derived-state divergence
— the position audit is the concrete mitigation), RR-012 (wrong-account
connection). `docs/RESEARCH.md` §2 (Time & Session Normalization).

## Non-Goals (This Phase)

No automated `tests/broker/` suite yet — verification this phase was ad hoc
against a fake `MetaTrader5` substitute (see `CHANGELOG.md` §0.4.0),
consistent with the project's plan to introduce the full automated test
harness in a dedicated later phase. Order submission/cancellation,
`get_latest_tick`/`get_bars` for `strategy/` consumption, and the account
allowlist cross-check referenced in `docs/RISK_REGISTER.md` RR-012 are not
yet implemented.
