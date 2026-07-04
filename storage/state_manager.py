"""Atomic persistence of FSM state and trade ledger entries.

Wraps storage/db_engine.py's connection with the read/write operations
needed for crash-safe FSM state recovery (ADR-0003, docs/RUNBOOK.md §1) and
trade ledger bookkeeping. Every write below is a single SQLite transaction;
the system_state row is a pinned singleton (id = 1) updated via UPSERT so a
crash mid-write can never leave two conflicting state rows, and
trade_ledger rows are keyed by client_order_id so a retried write cannot
create a duplicate ledger entry (RR-007).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any

from storage.db_engine import DEFAULT_DB_PATH, connect, initialize_schema


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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
                    status, strategy_id, magic_number, opened_at_utc, closed_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (client_order_id) DO UPDATE SET
                    close_price = excluded.close_price,
                    stop_loss_price = excluded.stop_loss_price,
                    take_profit_price = excluded.take_profit_price,
                    profit = excluded.profit,
                    status = excluded.status,
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
        closed_at_utc=row["closed_at_utc"],
    )
