# storage/

## Responsibility

Sole owner of the SQLite (WAL mode) transactional ledger. Exposes
`EventLogRepository`, `OrderRepository`, `PositionRepository`,
`EquityCurveRepository`, and `ParameterHistoryRepository` (see
`docs/API_SPEC.md` §4). Owns schema migrations, crash-recovery replay, and
backup/checkpoint operations. No other module opens a `sqlite3.Connection`.

## Depends On

Nothing internal (leaf module for persistence). External: `sqlite3` (standard
library).

## Depended On By

`core` (event log append + derived-table upsert on every state transition),
`execution/` (order/position idempotency checks), `analytics/` (equity curve
reads), `optimizer/` (parameter history reads/writes).

## Governing Docs

ADR-0003 (SQLite WAL as sole persistence engine). `docs/RISK_REGISTER.md`
RR-008 (event log / derived-state divergence) and RR-013 (DB corruption).
`docs/RUNBOOK.md` §4 (backups & disaster recovery).

## Non-Goals (This Phase)

No code exists yet. Schema DDL, migrations, and crash-recovery tests
(RQ-005, RQ-006) land in Phase 3.
