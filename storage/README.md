# storage/

## Responsibility

Sole owner of the SQLite (WAL mode) transactional ledger. No other module
opens a `sqlite3.Connection` directly.

## Implementation

- `db_engine.py` — low-level connection factory (`connect()`) and schema DDL
  (`initialize_schema()`). Enables `PRAGMA journal_mode=WAL`,
  `PRAGMA synchronous=FULL`, `PRAGMA foreign_keys=ON`, and (Phase 11e,
  `docs/PRODUCTION_SPEC.md` §7) `PRAGMA busy_timeout` (default 5000ms,
  overridable via `connect(busy_timeout_ms=...)`) on every connection —
  SQLite's own native wait-on-lock-contention mechanism, so a transient
  writer/reader lock resolves without any Python-level sleep-and-retry
  loop (the spec's "SQLite operations are barred from sleep-based
  retries" rule; `tests/unit/test_unit.py::TestStorageNeverSleeps`
  statically enforces it via `ast`). Every write already goes through
  `with connection:`, which rolls back immediately and atomically on any
  exception. Defines two tables:
  - `trade_ledger` — one row per trade, keyed by a unique `client_order_id`
    (idempotent upsert target, RR-007). Tracks symbol, side, volume, open/close
    price, stop-loss/take-profit, profit, status, strategy id, magic number,
    and (added Phase 3) `broker_ticket` — the MT5 position ticket, used by
    `broker/mt5_gateway.py`'s `audit_open_positions()` to reconcile the
    ledger against the broker's actual open positions on reconnect.
  - `system_state` — a pinned singleton row (`id = 1`) holding the current
    FSM state as a JSON blob plus the last-processed event `sequence_id`,
    updated via UPSERT so a crash mid-write can never leave two conflicting
    state rows.
  - `parameter_history` (added Phase 8) — append-only log of every
    parameter change `optimizer/self_learning.py` decides to make
    (`parameter_name`, `old_value`, `new_value`, `reason`,
    `applied_at_utc`). This is the *only* table that module ever writes
    to — it is a deliberately isolated write target so the weekend
    optimizer can never touch `system_state` or an open `trade_ledger`
    row (verified in `optimizer/README.md`).
  - Also exposes `checkpoint_wal()` (routine WAL truncation,
    `docs/RUNBOOK.md` §6) and `integrity_check()` (`PRAGMA integrity_check`,
    startup verification per `docs/RUNBOOK.md` §1).
- `state_manager.py` — `StateManager`, the atomic read/write API consumed by
  the rest of the system:
  - `save_fsm_state(state, last_sequence_id)` / `load_fsm_state()` — the
    crash-recovery path. Verified by ad hoc test: state saved by one
    `StateManager` instance is fully recoverable by a freshly constructed
    `StateManager` against the same database file after the first instance
    is dropped without a graceful `close()` (simulating an abrupt process
    death), with the `system_state` table always holding exactly one row.
  - `record_trade(entry)` / `get_open_trades()` / `get_closed_trades()`
    (the latter added Phase 8) — trade ledger bookkeeping. `record_trade`
    is idempotent on `client_order_id`: verified that calling it twice
    with the same entry (simulating a retry after a timeout) leaves
    exactly one `trade_ledger` row (RR-007).
  - `record_parameter_change(...)` (added Phase 8) — appends a row to
    `parameter_history`. See `optimizer/README.md` for the isolation
    guarantee this enables.
  - `record_order_event(client_order_id, event_type, metadata)` /
    `get_order_ledger_state(client_order_id)` / `get_order_events(client_order_id)` /
    `get_latest_order_event(client_order_id)` (Phase 11c,
    `docs/PRODUCTION_SPEC.md` §4/§5) — the order-lifecycle Event Store.
    `record_order_event()` appends one immutable row to `order_events` and
    folds it into the `order_ledger` projection's `state`/`timestamp`
    columns in the same atomic transaction. The very first call for a
    fresh `client_order_id` (`event_type=OrderLifecycleState.REQUESTED`)
    *is* §4's pre-flight execution log write — `main.py`'s
    `submit_with_pre_flight_ledger()` calls it before the corresponding
    payload is routed to the MT5 gateway at either of the two real
    submission call sites (a new market order, or a position action).
  - `record_audit_event(actor, action_type, parameter_name, old_value, new_value, metadata)` /
    `get_audit_trail(parameter_name=None)` (Phase 11e,
    `docs/PRODUCTION_SPEC.md` §7) — the immutable persistent Audit Trail:
    one append-only row per state-altering administrative command
    (circuit-breaker reset, manual override, feature-flag alteration).
    `actor` is SHA-256-hashed before storage (the spec's literal "actor
    hashes" wording) — the raw identifier never lands in the table.
    Currently wired into `container.py`'s Disaster Recovery reconciliation
    (a `DISASTER_RECOVERY_RECONCILIATION` entry whenever a boot-time
    broker/ledger divergence forces `MANUAL_RESET_REQUIRED`).
- `migrations.py` (Phase 11c, extended Phase 11e) — `apply_pending_migrations()` /
  `get_applied_migrations()`: a lightweight schema-migration framework
  tracking applied versions in a `schema_migrations` table. Owns two
  migrations: version 1 (the `order_events`/`order_ledger` DDL, plus two
  SQLite triggers making `order_events` **structurally** append-only) and
  version 2 (Phase 11e: the `audit_trail` DDL, plus
  `trg_audit_trail_no_update`/`_no_delete` making it append-only the same
  way) — an `UPDATE`/`DELETE` against either table raises
  `sqlite3.IntegrityError` at the database engine level, not merely by
  Python-side convention. `StateManager.__init__` calls
  `initialize_schema()` (unchanged) then `apply_pending_migrations()`.
  Phase 2/3/8's original tables remain outside this framework — see
  "Simplification" below.

## Simplification vs. the Phase 0 API contract

`docs/API_SPEC.md` §4 originally specified five separate repository
protocols (`EventLogRepository`, `OrderRepository`, `PositionRepository`,
`EquityCurveRepository`, `ParameterHistoryRepository`) sitting on top of a
full append-only event log, per ADR-0003's original design. Phase 2
implemented a smaller, concrete two-table schema (`trade_ledger`,
`system_state`) sufficient for FSM crash-recovery and trade bookkeeping,
deferring the full event-sourcing model until `core`'s `EventBus` actually
exists to produce events for it (no phase has built `core` yet). The
event-log/derived-table replay guarantee described in ADR-0003 is not yet
implemented — today's guarantee is narrower: the last-saved FSM state
snapshot survives a crash, not a full replay of every intermediate event.
This gap is intentional and tracked, not silently dropped.

Phase 11c's `order_events`/`order_ledger` narrows `EventLogRepository`
similarly: a concrete pair of tables (append-only log + folded projection)
scoped to *order lifecycle* specifically, not `EventEnvelope`'s fully
generic, `core`/`EventBus`-produced event shape (ADR-0001). `OrderEvent`
plays the `EventEnvelope` role and `record_order_event()`/`get_order_events()`
play `EventLogRepository.append()`/`replay_since()`'s role, narrowed to one
concrete domain. This is a real, working slice of the original
event-sourcing vision — not a placeholder — but it does not generalize to
`system_state`/`trade_ledger` or any other table; those remain under their
Phase 2/3/8 UPSERT-based model, unchanged by this phase.

## Depends On

Nothing internal (leaf module for persistence). External: `sqlite3` (standard
library).

## Depended On By

`execution/` (Phase 11c: `execution.validation.check_duplicate_order_before_retry()`
consumes `OrderEvent`/`OrderLifecycleState`; the idempotency ledger writes
themselves are called directly by `main.py`, not by `execution/`),
`broker/` (Phase 11c: `MT5Gateway.is_ticket_still_open()` supplies the
"server cache" half of the same audit; Phase 11e:
`resolve_position_audit()` constructs `TradeLedgerEntry` upserts this
module's `record_trade()` persists), `analytics/` (trade ledger reads,
future phase), `optimizer/` (`get_closed_trades()`/`record_parameter_change()`,
Phase 8), `container.py` (Phase 11e: Disaster Recovery reconciliation
calls `record_trade()` for each settlement upsert and
`record_audit_event()` when a divergence forces `MANUAL_RESET_REQUIRED`),
`main.py` (boot-time state recovery; Phase 11c:
`submit_with_pre_flight_ledger()` calls `record_order_event()` around both
real broker-submission call sites).

## Governing Docs

ADR-0003 (SQLite WAL as sole persistence engine). `docs/RISK_REGISTER.md`
RR-007 (duplicate order/idempotency — Phase 11c's concrete implementation),
RR-008 (event log / derived-state divergence — partially addressed, see
Simplification note above), and RR-013 (DB corruption, addressed by
`integrity_check()`). `docs/PRODUCTION_SPEC.md` §4/§5 (Phase 11c) and §7
(Phase 11e: `busy_timeout`, Audit Trail). `docs/RUNBOOK.md` §1 (startup
state reload) and §4/§6 (backups, WAL checkpointing).

## Non-Goals (This Phase)

No automated `tests/storage/` suite yet — verification this phase was ad hoc
(see `CHANGELOG.md` §0.3.0), consistent with the project's plan to introduce
the full automated test harness in a dedicated later phase. (Phase 9 later
added formal `tests/unit/test_unit.py` coverage, superseding this note;
Phase 11c's migration framework and Event Store additions, and Phase 11e's
Audit Trail and `busy_timeout`, are themselves formally tested — see
`TestSchemaMigrations`/`TestOrderEventStore`/`TestAuditTrail`/`TestBusyTimeout`.)
The full generic event-log/derived-table model and `EventLogRepository`
Protocol remain deferred until `core`'s `EventBus` lands — Phase 11c
narrows this gap for order lifecycles specifically, not generally (see
Simplification above); Phase 11e does the same for administrative
actions via the Audit Trail, not generally.
