# ADR-0003: SQLite (WAL Mode) as the Embedded Transactional State Ledger

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-07-04 |
| Deciders | Principal Quantitative Engineering |
| Supersedes | — |
| Superseded by | — |
| Related | ADR-0001 |

## Context

The core loop (ADR-0001) needs a durable, transactional record of every event and
every derived state transition (orders, fills, positions, equity marks, parameter
changes) that:

1. Survives process restart with zero ambiguity about the last confirmed state
   (crash-safe, ACID).
2. Supports point-in-time and full-history queries for `analytics/` (Sharpe, Sortino,
   MAR, drawdown) and for audit/compliance (`docs/RISK_REGISTER.md`,
   `docs/RUNBOOK.md`).
3. Requires no external service dependency, network round-trip, or credential
   surface, given the target deployment is a single Windows VPS running one MT5
   terminal and one bot instance (`docs/DEPLOYMENT.md`).
4. Must not become a concurrency hazard given the single-writer core loop
   (ADR-0001) — the storage layer's job is durability, not arbitration.

## Decision

Use **SQLite in WAL (Write-Ahead Logging) journal mode** as the sole persistence
engine, accessed exclusively through the `storage/` module's repository interfaces
(`EventLogRepository`, `OrderRepository`, `PositionRepository`,
`EquityCurveRepository`, `ParameterHistoryRepository` — see `docs/API_SPEC.md`).

Concretely:

1. **One database file, WAL mode, single writer connection.** `storage/` opens the
   database with `PRAGMA journal_mode=WAL;` and `PRAGMA synchronous=FULL;`
   (durability over the tiny perf gain of `NORMAL`, since transaction volume for a
   single-instrument swing/intraday system is low — tens to low hundreds of writes
   per day, not a throughput-bound workload). Exactly one connection in the process
   holds write access, owned by the core loop's persistence step, consistent with
   the single-writer principle in ADR-0001. Read-only connections (for
   `analytics/`, dashboards, or ad hoc inspection) open with
   `PRAGMA query_only=ON;` and can run concurrently with the writer under WAL
   without blocking it.
2. **Append-only event log is the source of truth.** The `events` table stores
   every `EventBus` event (ADR-0001) as an immutable row
   (`sequence_id INTEGER PRIMARY KEY, event_type TEXT, payload_json TEXT,
   source_timestamp_utc TEXT, received_timestamp_utc TEXT`). Derived tables
   (`orders`, `positions`, `fills`, `equity_curve`, `parameter_history`) are
   materialized views over this log, rebuildable by replay — satisfying the
   crash-recovery consequence claimed in ADR-0001.
3. **Every state-changing write is one transaction.** A single `BEGIN IMMEDIATE …
   COMMIT` wraps "append event row" + "update derived table row(s)" so that a crash
   mid-write can never leave the event log and the derived tables disagreeing.
4. **Schema migrations are explicit and versioned.** `storage/` embeds a
   `schema_version` table and a linear migration script list (`storage/migrations/`,
   later phase); the application refuses to start against a database whose
   `schema_version` it does not recognize, rather than guessing.
5. **Backups are file-level, not logical-dump, and scheduled.** Because SQLite is a
   single file (plus WAL/SHM sidecars), backup is `VACUUM INTO` to a timestamped
   snapshot on a fixed cadence (see `docs/RUNBOOK.md` §Backups), consistent even
   while the WAL is active.

## Consequences

### Positive

- Zero external infrastructure dependency (no DB server, no network credential
  surface) — directly serves the single-VPS deployment target in
  `docs/DEPLOYMENT.md` and shrinks the attack surface in `docs/RISK_REGISTER.md`.
- ACID transactions give crash-safety guarantees "for free," which is the load-bearing
  requirement for a system that must never lose or double-count a fill.
- WAL mode allows concurrent readers (`analytics/`, manual inspection, a future
  read-only dashboard) without blocking the single writer, and without blocking
  reads — a property the default rollback-journal mode does not provide.
- Full historical event log doubles as the deterministic replay input for
  `backtester/`'s event-driven mode and for post-incident forensics
  (`docs/RUNBOOK.md`).

### Negative / Accepted Trade-offs

- SQLite is not horizontally scalable and not safely writable from multiple host
  processes; explicitly acceptable because the architecture mandates exactly one
  writer process per account (ADR-0001) and there is no multi-instance requirement
  in scope.
- WAL mode requires the deployment filesystem to support proper `fsync`/locking
  semantics; network filesystems (SMB/NFS-mounted DB paths) are explicitly
  unsupported and documented as such in `docs/DEPLOYMENT.md`.
- Long-running WAL files require periodic checkpointing
  (`PRAGMA wal_checkpoint(TRUNCATE)`), scheduled in `docs/RUNBOOK.md`'s maintenance
  cadence, or the WAL file grows unbounded.

## Alternatives Considered

| Alternative | Rejected Because |
|---|---|
| PostgreSQL / MySQL server | Introduces an external service dependency, network credential surface, and operational overhead (patching, backup orchestration) disproportionate to a single-account, low-write-volume system. |
| Flat-file JSON/CSV ledger | No transactional guarantees; a crash mid-write can corrupt or truncate the file with no recovery path — unacceptable for an order/position ledger. |
| In-memory state with periodic snapshot-only persistence (no event log) | Loses all events since the last snapshot on crash; violates the crash-recovery requirement in ADR-0001. |
| Redis / key-value store | Optimized for a different access pattern (cache, not durable relational ledger); would require bolting on a separate durability/query layer for `analytics/`, duplicating effort SQLite provides natively. |

## Compliance / Verification

- Enforced structurally: only `storage/` may open a connection to the database
  file; no other module holds a `sqlite3.Connection` (checked at code review and,
  from Phase 3 onward, via a targeted `tests/` import-boundary test).
- Crash-recovery behavior (replay from last snapshot + unacknowledged event tail) is
  a mandatory FSM simulation test scenario once `storage/` and `execution/` land
  (see `docs/TRACEABILITY_MATRIX.md`).
- Backup/restore procedure specified operationally in `docs/RUNBOOK.md` §Backups
  and §Disaster Recovery.
