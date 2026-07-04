# storage/

## Responsibility

Sole owner of the SQLite (WAL mode) transactional ledger. No other module
opens a `sqlite3.Connection` directly.

## Implementation

- `db_engine.py` — low-level connection factory (`connect()`) and schema DDL
  (`initialize_schema()`). Enables `PRAGMA journal_mode=WAL`,
  `PRAGMA synchronous=FULL`, and `PRAGMA foreign_keys=ON` on every connection.
  Defines two tables:
  - `trade_ledger` — one row per trade, keyed by a unique `client_order_id`
    (idempotent upsert target, RR-007). Tracks symbol, side, volume, open/close
    price, stop-loss/take-profit, profit, status, strategy id, and magic
    number.
  - `system_state` — a pinned singleton row (`id = 1`) holding the current
    FSM state as a JSON blob plus the last-processed event `sequence_id`,
    updated via UPSERT so a crash mid-write can never leave two conflicting
    state rows.
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
  - `record_trade(entry)` / `get_open_trades()` — trade ledger bookkeeping.
    `record_trade` is idempotent on `client_order_id`: verified that calling
    it twice with the same entry (simulating a retry after a timeout) leaves
    exactly one `trade_ledger` row (RR-007).

## Simplification vs. the Phase 0 API contract

`docs/API_SPEC.md` §4 originally specified five separate repository
protocols (`EventLogRepository`, `OrderRepository`, `PositionRepository`,
`EquityCurveRepository`, `ParameterHistoryRepository`) sitting on top of a
full append-only event log, per ADR-0003's original design. This phase
implements a smaller, concrete two-table schema (`trade_ledger`,
`system_state`) sufficient for FSM crash-recovery and trade bookkeeping,
deferring the full event-sourcing model until `core`'s `EventBus` actually
exists to produce events for it (no phase has built `core` yet). The
event-log/derived-table replay guarantee described in ADR-0003 is not yet
implemented — today's guarantee is narrower: the last-saved FSM state
snapshot survives a crash, not a full replay of every intermediate event.
This gap is intentional and tracked, not silently dropped.

## Depends On

Nothing internal (leaf module for persistence). External: `sqlite3` (standard
library).

## Depended On By

`execution/` (trade ledger writes, order/position idempotency checks, future
phase), `analytics/` (trade ledger reads, future phase), the eventual FSM
orchestration loop (`main.py`, boot-time state recovery).

## Governing Docs

ADR-0003 (SQLite WAL as sole persistence engine). `docs/RISK_REGISTER.md`
RR-007 (duplicate order/idempotency), RR-008 (event log / derived-state
divergence — partially addressed, see Simplification note above), and RR-013
(DB corruption, addressed by `integrity_check()`). `docs/RUNBOOK.md` §1
(startup state reload) and §4/§6 (backups, WAL checkpointing).

## Non-Goals (This Phase)

No automated `tests/storage/` suite yet — verification this phase was ad hoc
(see `CHANGELOG.md` §0.3.0), consistent with the project's plan to introduce
the full automated test harness in a dedicated later phase. Schema
migrations, the full event-log/derived-table model, and `EventLogRepository`
are deferred until `core`'s `EventBus` lands.
