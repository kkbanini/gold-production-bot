"""Lightweight internal schema-migration framework (`docs/PRODUCTION_SPEC.md`
§4's "track db status securely" requirement): tracks which numbered
migrations have already been applied to a given SQLite file via a
`schema_migrations` table, and applies any still-pending ones in ascending
version order.

Governs schema changes from Phase 11c onward only. Phase 2/3/8's original
`trade_ledger`/`system_state`/`parameter_history` tables remain under
`db_engine.initialize_schema()`'s unconditionally-idempotent static DDL —
retrofitting them into this framework as a synthetic "migration zero" would
add churn/risk to already-tested, already-provisioned tables for no
behavioral benefit (every `CREATE TABLE IF NOT EXISTS` there is already
safe to replay). `StateManager.__init__` calls `initialize_schema()` first,
then `apply_pending_migrations()`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

_SCHEMA_MIGRATIONS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version         INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    applied_at_utc  TEXT NOT NULL
);
"""

# The 11 institutional order-lifecycle states docs/PRODUCTION_SPEC.md §5
# names verbatim. Shared between order_events.event_type and
# order_ledger.state's CHECK constraints so a bad value can never be
# written to either table, not just validated at the Python layer.
_ORDER_LIFECYCLE_STATES_SQL_LIST = (
    "'REQUESTED', 'VALIDATED', 'SENT', 'PENDING', 'PARTIALLY_FILLED', "
    "'FILLED', 'MODIFIED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'CLOSED'"
)

_ORDER_LEDGER_AND_EVENT_STORE_DDL = f"""
-- The Event Store (docs/PRODUCTION_SPEC.md §5): structurally append-only
-- (enforced below by trigger, not just convention) — the immutable source
-- of truth for every order's lifecycle. order_ledger, below, is the
-- mutable *projection* derived from this stream; it is never itself a
-- second source of truth.
CREATE TABLE IF NOT EXISTS order_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id     TEXT NOT NULL,
    event_type          TEXT NOT NULL CHECK (event_type IN ({_ORDER_LIFECYCLE_STATES_SQL_LIST})),
    metadata_json       TEXT NOT NULL,
    created_at_utc      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_order_events_client_order_id
    ON order_events (client_order_id);

CREATE TRIGGER IF NOT EXISTS trg_order_events_no_update
BEFORE UPDATE ON order_events
BEGIN
    SELECT RAISE(ABORT, 'order_events is append-only: UPDATE is prohibited');
END;

CREATE TRIGGER IF NOT EXISTS trg_order_events_no_delete
BEFORE DELETE ON order_events
BEGIN
    SELECT RAISE(ABORT, 'order_events is append-only: DELETE is prohibited');
END;

-- The pre-flight idempotency projection (docs/PRODUCTION_SPEC.md §4's
-- literal `INSERT INTO order_ledger (client_order_id, state, timestamp)
-- VALUES (?, 'REQUESTED', ?)`): one row per client_order_id, `state`
-- always reflecting the most recently folded event_type, `timestamp` the
-- UTC time of that fold — updated in place (unlike order_events), since
-- this is the read-model projection, not the log.
CREATE TABLE IF NOT EXISTS order_ledger (
    client_order_id     TEXT PRIMARY KEY,
    state               TEXT NOT NULL CHECK (state IN ({_ORDER_LIFECYCLE_STATES_SQL_LIST})),
    timestamp           TEXT NOT NULL
);
"""

_AUDIT_TRAIL_DDL = """
-- The immutable persistent Audit Trail (docs/PRODUCTION_SPEC.md §7):
-- structurally append-only (enforced below by trigger, same pattern as
-- order_events), one row per state-altering administrative command
-- (circuit-breaker reset, manual override, feature-flag alteration).
-- `actor_signature` is a hash of the caller-supplied actor identifier
-- (storage.state_manager.record_audit_event() hashes it), never the raw
-- identity, per the spec's literal "actor hashes" wording.
CREATE TABLE IF NOT EXISTS audit_trail (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc       TEXT NOT NULL,
    actor_signature     TEXT NOT NULL,
    action_type         TEXT NOT NULL,
    parameter_name      TEXT NOT NULL,
    old_value           TEXT,
    new_value           TEXT,
    metadata_json       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_trail_action_type ON audit_trail (action_type);
CREATE INDEX IF NOT EXISTS idx_audit_trail_parameter_name ON audit_trail (parameter_name);

CREATE TRIGGER IF NOT EXISTS trg_audit_trail_no_update
BEFORE UPDATE ON audit_trail
BEGIN
    SELECT RAISE(ABORT, 'audit_trail is append-only: UPDATE is prohibited');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_trail_no_delete
BEFORE DELETE ON audit_trail
BEGIN
    SELECT RAISE(ABORT, 'audit_trail is append-only: DELETE is prohibited');
END;
"""


@dataclass(frozen=True, slots=True)
class Migration:
    """One numbered, named schema change. `sql` must be composed entirely
    of idempotent statements (`CREATE TABLE/INDEX/TRIGGER IF NOT EXISTS`)
    so replaying it against an already-migrated database is always safe."""

    version: int
    name: str
    sql: str


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="order_ledger_and_event_store",
        sql=_ORDER_LEDGER_AND_EVENT_STORE_DDL,
    ),
    Migration(
        version=2,
        name="audit_trail",
        sql=_AUDIT_TRAIL_DDL,
    ),
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def get_applied_migrations(connection: sqlite3.Connection) -> tuple[int, ...]:
    """Return every migration version already recorded as applied, in
    ascending order. Creates the (empty) schema_migrations table first if
    this is a brand-new database."""
    connection.execute(_SCHEMA_MIGRATIONS_TABLE_DDL)
    cursor = connection.execute("SELECT version FROM schema_migrations ORDER BY version")
    return tuple(int(row[0]) for row in cursor.fetchall())


def apply_pending_migrations(connection: sqlite3.Connection) -> tuple[int, ...]:
    """Apply every `MIGRATIONS` entry not yet recorded in
    `schema_migrations`, in ascending version order.

    Each migration's DDL is applied via `executescript` (itself composed
    only of `IF NOT EXISTS` statements, so it is always safe to replay),
    then its version is recorded in its own atomic transaction. A crash
    between the two leaves the DDL harmlessly re-appliable on the next
    boot rather than a half-recorded version — `executescript` issues its
    own implicit commit ahead of running, so it cannot itself be nested
    inside the version-recording transaction. Returns the newly-applied
    version numbers (empty on a no-op re-run against an already-migrated
    database).
    """
    applied = set(get_applied_migrations(connection))
    newly_applied: list[int] = []
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        connection.executescript(migration.sql)
        with connection:
            connection.execute(
                "INSERT INTO schema_migrations (version, name, applied_at_utc) VALUES (?, ?, ?)",
                (migration.version, migration.name, _utc_now_iso()),
            )
        newly_applied.append(migration.version)
    return tuple(newly_applied)
