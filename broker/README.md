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
    - `resolve_position_audit(report)` (Phase 11e, `docs/PRODUCTION_SPEC.md`
      §7's Disaster Recovery bullet) — turns a `PositionAuditReport` into a
      concrete `DisasterRecoveryPlan`: broker-only positions are
      reconstructed into a new `OPEN` `trade_ledger` row from the broker's
      own fields (a deterministic `f"disaster-recovery-{ticket}"`
      `client_order_id`, idempotent across repeated reconciliation runs);
      ledger-only entries are settled by marking them `CLOSED_RECONCILED`
      at the reconciliation moment. Any divergence at all sets
      `requires_manual_review=True` — `docs/RUNBOOK.md`'s own established
      policy already treats a position-audit mismatch as `HIGH`-severity,
      blocking automated trading pending manual review (RR-008); the
      caller (`container.py`) honors that by starting the FSM's
      `drawdown_state` at `MANUAL_RESET_REQUIRED` rather than `ACTIVE`
      whenever this is `True`. Pure — no I/O of its own; `container.py`
      applies the plan's `ledger_upserts` via `StateManager.record_trade()`.

- `submit_position_action(payload)` (Phase 6) — translates an
  `execution.position_manager.OrderActionPayload` "intent" into a real
  `mt5.order_send()` request:
  - `TRADE_ACTION_DEAL` (partial close): looks up the live position via
    `mt5.positions_get(ticket=...)` to determine its side, computes the
    opposite closing order type, reads the current bid/ask via
    `mt5.symbol_info_tick()` for the closing price, and submits with a
    `CLOSE_DEVIATION_POINTS` (20) slippage tolerance — a placeholder
    default, not a policy decision (the full slippage guard is RQ-010,
    a later phase).
  - `TRADE_ACTION_SLTP` (modify stop-loss/take-profit): a simpler request
    carrying only `position`/`symbol`/`magic`/`sl`/`tp`.
  - Raises `BrokerOrderRejectedError` if `order_send()` returns anything
    other than `TRADE_RETCODE_DONE`, or if the referenced position/tick
    can't be found. This keeps the actual `MetaTrader5` payload
    construction inside `broker/` — `execution/position_manager.py`
    never imports `MetaTrader5` itself (ADR-0002/RQ-001).

- `clock_provider.py` (Phase 11b, `docs/PRODUCTION_SPEC.md` §3):
  - `ClockProvider` — a `Protocol` (`get_server_time(symbol) -> AwareDatetime`)
    so session-boundary logic never touches the host machine's local clock
    or a hardcoded DST table directly.
  - `MT5ClockProvider` — the production implementation: adds the connected
    `MT5Gateway`'s `broker_utc_offset` (itself derived from the resolved
    Gold symbol's latest tick, never a hardcoded value) to the current
    host UTC time to reconstruct current broker server time. Raises
    `ValueError` if called with a symbol other than the one the gateway is
    bound to (this system trades a single Gold symbol; multi-symbol clocks
    are out of scope).
- `is_ticket_still_open(ticket)` (Phase 11c, `docs/PRODUCTION_SPEC.md` §4) —
  queries `mt5.positions_get(ticket=...)`, returning whether it's still
  open. The "query the server cache" half of the pre-flight idempotency
  audit before an automated retry; the "audit the local transaction
  engine" half is `storage.state_manager.get_latest_order_event()`. Both
  feed `execution.validation.check_duplicate_order_before_retry()`.
- `get_account_state()` (Phase 10) — snapshots `mt5.account_info()`
  (balance/equity/margin) into an `AccountState`, the input `main.py`'s
  drawdown-breaker checks are computed from. Raises `BrokerConnectionError`
  if unavailable.
- `get_bars(timeframe, count)` (Phase 10) — fetches the last `count` closed
  bars via `mt5.copy_rates_from_pos()`, returning a `BarSeries` of numpy
  arrays ready for `indicators/`/`strategy/` consumption directly (no
  per-bar object overhead). `TIMEFRAME_D1`/`TIMEFRAME_H4`/`TIMEFRAME_H1`/
  `TIMEFRAME_M5` are re-exported from this module so callers (`main.py`)
  never need to import `MetaTrader5` themselves. Raises
  `BrokerConnectionError` if no data is returned.
- `submit_market_order(side, volume, stop_loss, take_profit, comment)`
  (Phase 10) — opens a *new* position: reads the current bid/ask, submits
  a `TRADE_ACTION_DEAL` market order, and returns a `BrokerPosition`. This
  does **not** perform a pre-trade risk gate, slippage guard, or
  duplicate-submission idempotency check (RQ-009/RQ-010, RR-007) — those
  remain the caller's (`main.py`'s) responsibility and are still an open
  gap, see `docs/ARCHITECTURE_SUMMARY.md` §5.

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
success and failure paths for each function. Phase 6's
`submit_position_action()` request-building (BUY-position-close vs.
SELL-position-close order-type/price mapping, the modify-SLTP request
shape, and both the missing-position and non-DONE-retcode rejection
paths) was verified the same way. Phase 10's `get_account_state()`,
`get_bars()`, and `submit_market_order()` were verified the same way too,
now as formal `tests/integration/test_integration.py::TestBrokerAccountAndBars`
cases. Phase 11e's `resolve_position_audit()` is fully unit-tested
(`tests/unit/test_unit.py::TestResolvePositionAudit`) since it's pure —
no fake `MetaTrader5` needed.

## Depends On

`config/` (credentials, `ENVIRONMENT_MODE`), `execution/`
(`OrderActionPayload`, the "intent" type `submit_position_action()`
translates into a real MT5 request), `storage/` (`TradeLedgerEntry` for
position-audit reconciliation). External: `MetaTrader5` package,
locally-running MT5 terminal process. `clock_provider.py` additionally
depends on `mt5_gateway.py`'s own `MT5Gateway`/`broker_utc_offset` — it
adds no new external dependency.

## Depended On By

`main.py` (Phase 10): connection lifecycle, startup position
reconciliation, bar/account fetching, and order submission for the master
FSM orchestration loop. `container.py`'s `ApplicationContainer` (Phase
11b): constructs `MT5ClockProvider(gateway=gateway)` and holds it as
`clock_provider` — **not yet consumed by `main.py`'s live loop**, which
still calls `datetime.now(timezone.utc)` directly for `run_bar_close_cycle()`'s
`now_utc` and the next-bar-close sleep, rather than through
`ClockProvider.get_server_time()`; see `docs/ARCHITECTURE_SUMMARY.md` §5.
`execution/validation.py` (Phase 11c: `is_ticket_still_open()` is the
input a future automated retry loop would pass to
`check_duplicate_order_before_retry()` — no such loop exists in `main.py`
yet, see `docs/ARCHITECTURE_SUMMARY.md` §5). `container.py`'s
`ApplicationContainer` (Phase 11e: calls `resolve_position_audit()` right
after `audit_open_positions()`, applies the resulting
`DisasterRecoveryPlan.ledger_upserts`, and computes
`initial_drawdown_state`); `main.py` (Phase 11e:
`_seed_initial_fsm_context()` calls `get_open_positions_by_magic()`
directly to resume `IN_POSITION` from the broker's live truth on boot).

## Governing Docs

ADR-0002 (MT5 broker gateway abstraction). `docs/RISK_REGISTER.md` RR-002
(disconnects), RR-003 (time skew), RR-007 (duplicate orders — the ticket-based
audit is part of this defense), RR-008 (event log / derived-state divergence
— the position audit is the concrete mitigation), RR-012 (wrong-account
connection). `docs/RESEARCH.md` §2 (Time & Session Normalization).

## Non-Goals (This Phase)

`cancel_order` (canceling a pending, not-yet-filled order — this system
only ever uses market orders, so no pending-order type exists to cancel)
and the account allowlist cross-check referenced in
`docs/RISK_REGISTER.md` RR-012 (`ENVIRONMENT_MODE` is validated by
`config/` but never cross-checked against the actually-connected account's
real demo/live status) are still not implemented — see
`docs/ARCHITECTURE_SUMMARY.md` §5 for the full list of gaps to close
before live use. `submit_market_order()`/`submit_position_action()` still
fold "MT5 explicitly rejected this" and "MT5 returned nothing (ambiguous
— possibly a transient timeout)" into the same `BrokerOrderRejectedError`
(Phase 11e, `resilience/README.md`'s Non-Goals) — distinguishing them is
a prerequisite for safely wrapping either call in an automated retry
loop, and this phase does not do it.
