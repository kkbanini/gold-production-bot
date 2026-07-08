"""Atomic persistence of FSM state, trade ledger entries, and (Phase 11c)
the order-lifecycle event store.

Wraps storage/db_engine.py's connection with the read/write operations
needed for crash-safe FSM state recovery (ADR-0003, docs/RUNBOOK.md §1) and
trade ledger bookkeeping. Every write below is a single SQLite transaction;
the system_state row is a pinned singleton (id = 1) updated via UPSERT so a
crash mid-write can never leave two conflicting state rows, and
trade_ledger rows are keyed by client_order_id so a retried write cannot
create a duplicate ledger entry (RR-007).

Phase 11c added `record_order_event()`/`get_order_events()`/
`get_latest_order_event()`/`get_order_ledger_state()`: the append-only
Event Store and its derived `order_ledger` projection
(`docs/PRODUCTION_SPEC.md` §4/§5), applied via `storage.migrations` on
every `StateManager` construction.

Phase 11e added `record_audit_event()`/`get_audit_trail()`: the immutable
persistent Audit Trail (`docs/PRODUCTION_SPEC.md` §7) for state-altering
administrative commands (circuit-breaker resets, manual overrides,
feature-flag alterations).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any

from storage.db_engine import DEFAULT_DB_PATH, connect, initialize_schema
from storage.migrations import apply_pending_migrations


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class OrderLifecycleState(str, Enum):
    """The 11 institutional order-lifecycle states `docs/PRODUCTION_SPEC.md`
    §5 names verbatim. `str` subclass so `.value` round-trips directly
    against the `order_events.event_type`/`order_ledger.state` TEXT columns
    (and their matching CHECK constraints, `storage/migrations.py`)."""

    REQUESTED = "REQUESTED"
    VALIDATED = "VALIDATED"
    SENT = "SENT"
    PENDING = "PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    MODIFIED = "MODIFIED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class OrderEvent:
    """One immutable row of the append-only `order_events` table — the
    Event Store `docs/PRODUCTION_SPEC.md` §5 requires. `order_ledger`'s
    `state` column is the derived/projected read model folded from this
    stream, never a second independent source of truth.

    `sequence_id` is the table's own `AUTOINCREMENT` row id: a strictly
    monotonic total order across every client_order_id, immune to
    same-millisecond timestamp collisions under fast/concurrent writes.
    """

    sequence_id: int
    client_order_id: str
    event_type: OrderLifecycleState
    metadata: dict[str, Any]
    created_at_utc: str


class AuditActionType(str, Enum):
    """Convenience constants for the 3 illustrative categories
    `docs/PRODUCTION_SPEC.md` §7 names ("Circuit breaker reset, Manual
    overrides, Flag alterations") — *not* an exhaustive, DB-enforced set
    like `OrderLifecycleState`: the spec doesn't claim these are the only
    state-altering commands that can occur, so `audit_trail.action_type`
    has no CHECK constraint and accepts any string."""

    CIRCUIT_BREAKER_RESET = "CIRCUIT_BREAKER_RESET"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"
    FLAG_ALTERATION = "FLAG_ALTERATION"
    DISASTER_RECOVERY_RECONCILIATION = "DISASTER_RECOVERY_RECONCILIATION"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One immutable row of the append-only `audit_trail` table
    (`docs/PRODUCTION_SPEC.md` §7). `actor_signature` is a hash, never the
    raw actor identifier — `record_audit_event()` hashes it."""

    sequence_id: int
    timestamp_utc: str
    actor_signature: str
    action_type: str
    parameter_name: str
    old_value: str | None
    new_value: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TradeLedgerEntry:
    """A single row of the trade_ledger table."""

    client_order_id: str
    symbol: str
    side: str
    volume_lots: float
    status: str
    opened_at_utc: str
    open_price: float | None = None
    close_price: float | None = None
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    profit: float | None = None
    strategy_id: str | None = None
    magic_number: int | None = None
    broker_ticket: int | None = None
    closed_at_utc: str | None = None


class StateManager:
    """Atomic reads/writes for FSM state snapshots and trade ledger rows.

    Opening a StateManager against an existing database file and calling
    load_fsm_state() immediately after is the crash-recovery path: it
    returns exactly the last snapshot saved before an unexpected shutdown,
    with no partial/torn state possible (docs/RUNBOOK.md §1 step 4).
    """

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self._connection: sqlite3.Connection = connect(db_path)
        initialize_schema(self._connection)
        apply_pending_migrations(self._connection)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "StateManager":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # --- FSM state (system_state) ---

    def save_fsm_state(self, state: dict[str, Any], *, last_sequence_id: int) -> None:
        """Persist the current FSM state as a single atomic UPSERT transaction."""
        payload = json.dumps(state, sort_keys=True)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO system_state (id, fsm_state_json, last_sequence_id, updated_at_utc)
                VALUES (1, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    fsm_state_json = excluded.fsm_state_json,
                    last_sequence_id = excluded.last_sequence_id,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (payload, last_sequence_id, _utc_now_iso()),
            )

    def load_fsm_state(self) -> tuple[dict[str, Any], int] | None:
        """Return (fsm_state, last_sequence_id) from the last snapshot, or None if never saved."""
        cursor = self._connection.execute(
            "SELECT fsm_state_json, last_sequence_id FROM system_state WHERE id = 1"
        )
        row = cursor.fetchone()
        if row is None:
            return None
        state: dict[str, Any] = json.loads(row["fsm_state_json"])
        return state, int(row["last_sequence_id"])

    # --- Trade ledger ---

    def record_trade(self, entry: TradeLedgerEntry) -> None:
        """Insert or update a trade_ledger row keyed by client_order_id.

        Idempotent on client_order_id: replaying the same entry after a
        crash-recovery retry updates the existing row instead of creating
        a duplicate (RR-007).
        """
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO trade_ledger (
                    client_order_id, symbol, side, volume_lots, open_price,
                    close_price, stop_loss_price, take_profit_price, profit,
                    status, strategy_id, magic_number, broker_ticket,
                    opened_at_utc, closed_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (client_order_id) DO UPDATE SET
                    close_price = excluded.close_price,
                    stop_loss_price = excluded.stop_loss_price,
                    take_profit_price = excluded.take_profit_price,
                    profit = excluded.profit,
                    status = excluded.status,
                    broker_ticket = excluded.broker_ticket,
                    closed_at_utc = excluded.closed_at_utc
                """,
                (
                    entry.client_order_id,
                    entry.symbol,
                    entry.side,
                    entry.volume_lots,
                    entry.open_price,
                    entry.close_price,
                    entry.stop_loss_price,
                    entry.take_profit_price,
                    entry.profit,
                    entry.status,
                    entry.strategy_id,
                    entry.magic_number,
                    entry.broker_ticket,
                    entry.opened_at_utc,
                    entry.closed_at_utc,
                ),
            )

    def get_open_trades(self) -> list[TradeLedgerEntry]:
        """Return all trade_ledger rows that have not yet been closed."""
        cursor = self._connection.execute(
            "SELECT * FROM trade_ledger WHERE closed_at_utc IS NULL ORDER BY opened_at_utc"
        )
        return [_row_to_entry(row) for row in cursor.fetchall()]

    def get_open_trade_by_ticket(self, broker_ticket: int) -> TradeLedgerEntry | None:
        """The still-open ledger row for `broker_ticket`, or `None`.

        `main.py` uses this to find the `client_order_id` a short-term
        entry was recorded under, once it detects (via a fresh broker
        query) that the position is no longer open — the ledger's own
        `broker_ticket` column is the only link back to that id, since
        the closed-position event itself carries only the ticket.
        """
        row = self._connection.execute(
            "SELECT * FROM trade_ledger WHERE broker_ticket = ? AND closed_at_utc IS NULL",
            (broker_ticket,),
        ).fetchone()
        return _row_to_entry(row) if row is not None else None

    def get_closed_trades(self) -> list[TradeLedgerEntry]:
        """Return all trade_ledger rows that have already been closed.

        Read-only against historical performance data — the only
        trade_ledger read `optimizer/self_learning.py` is permitted to
        perform (isolation guarantee: it never reads or writes an open
        position).
        """
        cursor = self._connection.execute(
            "SELECT * FROM trade_ledger WHERE closed_at_utc IS NOT NULL ORDER BY opened_at_utc"
        )
        return [_row_to_entry(row) for row in cursor.fetchall()]

    # --- Parameter history (optimizer/) ---

    def record_parameter_change(
        self, parameter_name: str, old_value: float, new_value: float, reason: str
    ) -> None:
        """Append a row to the isolated parameter_history table.

        This is the only write `optimizer/self_learning.py` is permitted
        to perform — it never touches `system_state` or an open
        `trade_ledger` row (isolation guarantee).
        """
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO parameter_history
                    (parameter_name, old_value, new_value, reason, applied_at_utc)
                VALUES (?, ?, ?, ?, ?)
                """,
                (parameter_name, old_value, new_value, reason, _utc_now_iso()),
            )

    def get_latest_parameter_value(self, parameter_name: str) -> float | None:
        """The most recent `new_value` ever recorded for `parameter_name`
        in `parameter_history`, or `None` if it's never been shifted —
        the read half of `record_parameter_change()`'s write, letting a
        live caller (`main.py`) resolve "the currently-effective value"
        instead of a hardcoded constant that never learns anything.
        """
        row = self._connection.execute(
            """
            SELECT new_value FROM parameter_history
            WHERE parameter_name = ?
            ORDER BY applied_at_utc DESC, id DESC
            LIMIT 1
            """,
            (parameter_name,),
        ).fetchone()
        return float(row[0]) if row is not None else None

    # --- Order lifecycle event store (Phase 11c, docs/PRODUCTION_SPEC.md §4/§5) ---

    def record_order_event(
        self,
        client_order_id: str,
        event_type: OrderLifecycleState,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append one immutable lifecycle event and fold it into the
        `order_ledger` projection, atomically.

        The very first call for a given `client_order_id` (normally with
        `event_type=OrderLifecycleState.REQUESTED`) is the pre-flight
        execution log write §4 requires: it must happen, in this same
        atomic transaction, before the corresponding payload is routed to
        the MT5 gateway — the caller is responsible for sequencing that
        (see `main.py`'s `submit_with_pre_flight_ledger()`), since this
        method has no knowledge of the broker call it precedes.
        """
        metadata = metadata if metadata is not None else {}
        metadata_json = json.dumps(metadata, sort_keys=True)
        now = _utc_now_iso()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO order_events
                    (client_order_id, event_type, metadata_json, created_at_utc)
                VALUES (?, ?, ?, ?)
                """,
                (client_order_id, event_type.value, metadata_json, now),
            )
            self._connection.execute(
                """
                INSERT INTO order_ledger (client_order_id, state, timestamp) VALUES (?, ?, ?)
                ON CONFLICT (client_order_id) DO UPDATE SET
                    state = excluded.state,
                    timestamp = excluded.timestamp
                """,
                (client_order_id, event_type.value, now),
            )

    def get_order_ledger_state(self, client_order_id: str) -> str | None:
        """Return the current projected state for `client_order_id`
        (the `order_ledger` row's `state` column), or `None` if no event
        has ever been recorded for it."""
        cursor = self._connection.execute(
            "SELECT state FROM order_ledger WHERE client_order_id = ?", (client_order_id,)
        )
        row = cursor.fetchone()
        return None if row is None else str(row["state"])

    def get_order_events(self, client_order_id: str) -> list[OrderEvent]:
        """Return the full, ordered lifecycle history for `client_order_id`
        from the append-only Event Store, oldest first."""
        cursor = self._connection.execute(
            "SELECT * FROM order_events WHERE client_order_id = ? ORDER BY id",
            (client_order_id,),
        )
        return [_row_to_order_event(row) for row in cursor.fetchall()]

    def get_latest_order_event(self, client_order_id: str) -> OrderEvent | None:
        """Return the most recently recorded event for `client_order_id`,
        or `None` if it has never been pre-flight-recorded — the input the
        pre-retry duplicate-order gate (`execution.validation`) audits."""
        cursor = self._connection.execute(
            "SELECT * FROM order_events WHERE client_order_id = ? ORDER BY id DESC LIMIT 1",
            (client_order_id,),
        )
        row = cursor.fetchone()
        return None if row is None else _row_to_order_event(row)

    # --- Audit trail (Phase 11e, docs/PRODUCTION_SPEC.md §7) ---

    def record_audit_event(
        self,
        actor: str,
        action_type: str,
        parameter_name: str,
        old_value: object,
        new_value: object,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append one immutable row to the Audit Trail.

        `actor` is hashed (SHA-256) before storage — the spec's literal
        "actor hashes" wording — so the raw identifier (a username,
        hostname, or similar) never lands in the persisted table.
        `old_value`/`new_value` are stringified for the TEXT columns;
        pass `None` for either where there's no meaningful prior/new value
        (e.g. an event with no single scalar parameter).
        """
        metadata = metadata if metadata is not None else {}
        metadata_json = json.dumps(metadata, sort_keys=True)
        actor_signature = hashlib.sha256(actor.encode("utf-8")).hexdigest()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO audit_trail (
                    timestamp_utc, actor_signature, action_type, parameter_name,
                    old_value, new_value, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_now_iso(),
                    actor_signature,
                    action_type,
                    parameter_name,
                    None if old_value is None else str(old_value),
                    None if new_value is None else str(new_value),
                    metadata_json,
                ),
            )

    def get_audit_trail(self, *, parameter_name: str | None = None) -> list[AuditEvent]:
        """Return the full Audit Trail, oldest first, optionally filtered
        to one `parameter_name`."""
        if parameter_name is None:
            cursor = self._connection.execute("SELECT * FROM audit_trail ORDER BY id")
        else:
            cursor = self._connection.execute(
                "SELECT * FROM audit_trail WHERE parameter_name = ? ORDER BY id",
                (parameter_name,),
            )
        return [_row_to_audit_event(row) for row in cursor.fetchall()]


def _row_to_entry(row: sqlite3.Row) -> TradeLedgerEntry:
    return TradeLedgerEntry(
        client_order_id=row["client_order_id"],
        symbol=row["symbol"],
        side=row["side"],
        volume_lots=row["volume_lots"],
        status=row["status"],
        opened_at_utc=row["opened_at_utc"],
        open_price=row["open_price"],
        close_price=row["close_price"],
        stop_loss_price=row["stop_loss_price"],
        take_profit_price=row["take_profit_price"],
        profit=row["profit"],
        strategy_id=row["strategy_id"],
        magic_number=row["magic_number"],
        broker_ticket=row["broker_ticket"],
        closed_at_utc=row["closed_at_utc"],
    )


def _row_to_order_event(row: sqlite3.Row) -> OrderEvent:
    return OrderEvent(
        sequence_id=row["id"],
        client_order_id=row["client_order_id"],
        event_type=OrderLifecycleState(row["event_type"]),
        metadata=json.loads(row["metadata_json"]),
        created_at_utc=row["created_at_utc"],
    )


def _row_to_audit_event(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(
        sequence_id=row["id"],
        timestamp_utc=row["timestamp_utc"],
        actor_signature=row["actor_signature"],
        action_type=row["action_type"],
        parameter_name=row["parameter_name"],
        old_value=row["old_value"],
        new_value=row["new_value"],
        metadata=json.loads(row["metadata_json"]),
    )
