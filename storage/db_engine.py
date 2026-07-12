"""SQLite (WAL mode) connection factory and schema DDL.

Sole low-level owner of the SQLite connection lifecycle and schema
definition for the storage/ transactional ledger (ADR-0003). No other
module opens a sqlite3.Connection directly; storage/state_manager.py is
the only consumer of this module's connect()/initialize_schema().

`docs/PRODUCTION_SPEC.md` §7: "SQLite operations are barred from
sleep-based retries and must utilize immediate rollbacks combined with
explicit `busy_timeout` definitions." `connect()` sets `PRAGMA
busy_timeout` (SQLite's own native wait-and-retry on `SQLITE_BUSY`,
implemented in the C library, not a Python `time.sleep()` loop) so a
transient writer/reader lock contention resolves itself without any
application-level retry logic; every write already goes through `with
connection:` (Python's own transaction context manager), which rolls back
immediately and atomically on any exception — no module in this codebase
ever wraps a SQLite call in a sleep-and-retry loop
(`tests/unit/test_unit.py::TestStorageNeverSleeps` statically enforces
this).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH: Path = Path(__file__).resolve().parent / "gold_bot.db"
DEFAULT_BUSY_TIMEOUT_MS = 5000

# main.py writes its own OS process ID here at boot; monitoring/telegram_bot.py's
# /killbot reads it to find the process to terminate. Colocated with
# DEFAULT_DB_PATH (not owned by monitoring/) so both main.py and
# monitoring/telegram_bot.py depend only on storage/ for this path, never
# on each other directly.
MAIN_PID_PATH: Path = Path(__file__).resolve().parent / "main.pid"

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS trade_ledger (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id     TEXT NOT NULL UNIQUE,
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    volume_lots         REAL NOT NULL,
    open_price          REAL,
    close_price         REAL,
    stop_loss_price     REAL,
    take_profit_price   REAL,
    profit              REAL,
    status              TEXT NOT NULL,
    strategy_id         TEXT,
    magic_number        INTEGER,
    broker_ticket       INTEGER,
    opened_at_utc       TEXT NOT NULL,
    closed_at_utc       TEXT,
    created_at_utc      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_trade_ledger_status ON trade_ledger (status);
CREATE INDEX IF NOT EXISTS idx_trade_ledger_symbol ON trade_ledger (symbol);
CREATE INDEX IF NOT EXISTS idx_trade_ledger_magic_number ON trade_ledger (magic_number);
CREATE INDEX IF NOT EXISTS idx_trade_ledger_broker_ticket ON trade_ledger (broker_ticket);

CREATE TABLE IF NOT EXISTS system_state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    fsm_state_json      TEXT NOT NULL,
    last_sequence_id    INTEGER NOT NULL DEFAULT 0,
    updated_at_utc      TEXT NOT NULL
);

-- Append-only. The sole write target for optimizer/self_learning.py
-- (isolation guarantee: that module never writes system_state or an open
-- trade_ledger row).
CREATE TABLE IF NOT EXISTS parameter_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    parameter_name      TEXT NOT NULL,
    old_value           REAL NOT NULL,
    new_value           REAL NOT NULL,
    reason              TEXT NOT NULL,
    applied_at_utc      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_parameter_history_name ON parameter_history (parameter_name);

-- Single-row liveness signal, same pinned-singleton pattern as
-- system_state: main.py's bar-close loop upserts this every iteration
-- (including during the weekend skip branch) so monitoring/telegram_bot.py
-- can tell "process alive and iterating" apart from "stopped/crashed"
-- without touching FSM state.
CREATE TABLE IF NOT EXISTS bot_heartbeat (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    trading_mode        TEXT NOT NULL,
    last_heartbeat_utc  TEXT NOT NULL
);
"""


def connect(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    read_only: bool = False,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Open a connection to the SQLite ledger with WAL mode enabled.

    Exactly one writer connection (read_only=False) is expected per process,
    per ADR-0003's single-writer principle. read_only connections may be
    opened concurrently (e.g. by analytics/ or manual inspection) without
    blocking the writer, since WAL allows concurrent readers.

    `busy_timeout_ms` (default 5000) sets SQLite's native `busy_timeout`:
    if this connection finds the database locked by another connection, it
    waits (internally, without any Python-level sleep loop) up to this many
    milliseconds for the lock to clear before raising `sqlite3.OperationalError`
    (`docs/PRODUCTION_SPEC.md` §7).
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if read_only:
        uri = f"file:{path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    else:
        connection = sqlite3.connect(str(path))

    connection.execute("PRAGMA journal_mode = WAL;")
    connection.execute("PRAGMA synchronous = FULL;")
    connection.execute("PRAGMA foreign_keys = ON;")
    connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)};")
    if read_only:
        connection.execute("PRAGMA query_only = ON;")
    connection.row_factory = sqlite3.Row
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create trade_ledger and system_state tables if they do not already exist."""
    connection.executescript(_SCHEMA_DDL)


def checkpoint_wal(connection: sqlite3.Connection) -> None:
    """Truncate the WAL file into the main database file.

    Scheduled as routine maintenance per docs/RUNBOOK.md §6.
    """
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE);")


def integrity_check(connection: sqlite3.Connection) -> bool:
    """Run SQLite's built-in integrity check. Returns True iff the database is sound."""
    cursor = connection.execute("PRAGMA integrity_check;")
    row = cursor.fetchone()
    return row is not None and str(row[0]) == "ok"
