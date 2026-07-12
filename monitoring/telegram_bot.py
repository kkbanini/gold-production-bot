"""Telegram status/control bot: `/check` reports whether `main.py`'s
bar-close loop is alive and which `TRADING_MODE` it's running under;
`/killbot` terminates the `main.py` OS process outright;
`/checkhisorder` reports the most recent trade_ledger rows.

Runs as its own standalone, long-polling process — never imported by
`main.py`/`container.py`. `/killbot` needs to send a real OS signal and
record the action to the Audit Trail, so this process opens a normal
(writable) `StateManager` rather than a read-only one. Its only DB write
is `record_audit_event()` — the same isolation guarantee
`parameter_history` gives `optimizer/self_learning.py` — so it never
touches `system_state` or an open `trade_ledger` row. SQLite's WAL mode
(`storage/db_engine.py`) is explicitly designed to support more than one
writer process, serialized via its own file locking plus `busy_timeout`;
this process's writes are rare (only on an explicit `/killbot`), so
contention with `main.py`'s own frequent heartbeat writes is not a
practical concern.

Liveness is derived from `bot_heartbeat` (`storage/state_manager.py`'s
`record_heartbeat()`/`get_heartbeat()`), written by `main.py` every loop
iteration — including its weekend-market-closed skip branch, so a closed
weekend market is never mistaken for a stopped process.

`/killbot` is Windows-only (`taskkill`, matching this project's
Windows-deployed environment) and is not reversible from Telegram: it
ends `main.py`'s process entirely, so restarting requires manually
running `python main.py` again on the host.
"""

from __future__ import annotations

import logging
import platform
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from config.telegram_config import TelegramConfig
from storage.db_engine import MAIN_PID_PATH
from storage.state_manager import AuditActionType, HeartbeatInfo, StateManager, TradeLedgerEntry

logger = logging.getLogger(__name__)

CHECK_COMMAND = "/check"
KILL_COMMAND = "/killbot"
CHECK_HISTORY_COMMAND = "/checkhisorder"
RECOGNIZED_COMMANDS = (CHECK_COMMAND, KILL_COMMAND, CHECK_HISTORY_COMMAND)

# How many of the most recent trade_ledger rows /checkhisorder reports —
# the number the user asked for, not derived from anything else.
ORDER_HISTORY_LIMIT = 7

# Timeout for the OS-level subprocess calls /killbot shells out to
# (querying then terminating a process) — generous for a purely local
# operation, just bounding it against an unexpected hang.
_PROCESS_CONTROL_TIMEOUT_SECONDS = 10.0

# Larger than both cadences main.py's loop ever heartbeats at (the normal
# ~5-minute M5 bar-close wait, and the 900s WEEKEND_RECHECK_SECONDS skip
# loop), so neither ever false-positives as "stopped" under this
# threshold. Made-up-but-documented, same as this project's other
# invented-but-flagged numeric defaults.
HEARTBEAT_STALE_AFTER = timedelta(minutes=20)

# Telegram's own long-poll timeout for getUpdates: the HTTP request hangs
# open server-side for up to this many seconds waiting for a new update
# before returning empty, instead of the caller busy-polling.
POLL_TIMEOUT_SECONDS = 30
_HTTP_TIMEOUT_SECONDS = POLL_TIMEOUT_SECONDS + 10

# Fixed retry delay after a transient network failure talking to
# Telegram's API. A monitoring poller that runs forever has no notion of
# a bounded "retry budget" (unlike resilience/backoff.py's use in
# news/calendar_provider.py, which wraps one bounded attempt before
# falling through to the next provider) — a failed poll simply retries
# after this pause, forever.
RETRY_DELAY_SECONDS = 5.0


def format_status_message(
    heartbeat: HeartbeatInfo | None,
    *,
    now: datetime,
    process_alive: bool | None = None,
    stale_after: timedelta = HEARTBEAT_STALE_AFTER,
) -> str:
    """Pure: render the `/check` reply text from the latest heartbeat.

    Liveness is decided two ways, in priority order:
    1. `process_alive` (from `_is_main_process_alive()`, an OS-level check
       against `main.py`'s PID file) is authoritative when it's `False` —
       "หยุดทำงาน" (stopped) immediately, even if the last heartbeat is
       still within `stale_after` (e.g. right after `/killbot`, whose PID
       is confirmed gone within seconds — waiting out the full
       `stale_after` window would misreport a dead process as running).
    2. Otherwise (process confirmed alive, or `process_alive is None`
       because the check can't be made — non-Windows, or no PID file has
       ever been written), fall back to heartbeat staleness: older than
       `stale_after` also reads as "หยุดทำงาน" (the loop has hung or
       crashed without the process itself exiting).

    No heartbeat row has ever been written (main.py has never run against
    this database file) is reported separately.
    """
    if heartbeat is None:
        return "⚠️ ยังไม่เคยพบข้อมูลการทำงานของบอท (main.py อาจยังไม่เคยรันสำเร็จ)"

    last_seen = datetime.fromisoformat(heartbeat.last_heartbeat_utc)
    age = now - last_seen
    age_minutes = int(age.total_seconds() // 60)
    heartbeat_fresh = age <= stale_after

    if process_alive is False:
        status_line = "🛑 หยุดทำงาน (main.py ถูกปิดไปแล้ว)"
    elif heartbeat_fresh:
        status_line = "✅ กำลังทำงาน"
    else:
        status_line = "🛑 หยุดทำงาน (ไม่มีการอัปเดตนานเกินไป)"

    return (
        f"สถานะบอท: {status_line}\n"
        f"โหมด: {heartbeat.trading_mode}\n"
        f"อัปเดตล่าสุด: "
        f"{last_seen.strftime('%Y-%m-%d %H:%M:%S')} UTC "
        f"({age_minutes} นาทีที่แล้ว)"
    )


def _format_order_timestamp(raw: str | None) -> str | None:
    """`raw` is one of trade_ledger's own ISO-string timestamp columns
    (`TradeLedgerEntry.opened_at_utc`/`closed_at_utc`) — reformatted for
    display, or passed through unchanged if it doesn't parse (defensive;
    every real row is written by `main.py`'s own ISO-producing code
    paths, so this should never actually trigger)."""
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw).strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return raw


def format_order_history_message(trades: list[TradeLedgerEntry]) -> str:
    """Pure: render `/checkhisorder`'s reply from the most recent
    trade_ledger rows (`StateManager.get_recent_trades()`'s output,
    already most-recent-first) — both still-`OPEN` and `CLOSED` orders,
    exactly as stored, no re-sorting or filtering here."""
    if not trades:
        return "📋 ยังไม่มีประวัติออเดอร์"

    lines = [f"📋 ประวัติ {len(trades)} ออเดอร์ล่าสุด:", ""]
    for i, trade in enumerate(trades, start=1):
        status_line = "🟢 OPEN" if trade.status == "OPEN" else "⚪ CLOSED"
        if trade.profit is not None:
            sign = "+" if trade.profit >= 0 else ""
            status_line += f" | กำไร/ขาดทุน: {sign}{trade.profit:.2f}"

        opened = _format_order_timestamp(trade.opened_at_utc)
        closed = _format_order_timestamp(trade.closed_at_utc)
        timing_line = f"เปิด: {opened}"
        if closed is not None:
            timing_line += f" | ปิด: {closed}"

        lines.append(f"{i}. {trade.side} {trade.volume_lots} {trade.symbol} — {status_line}")
        lines.append(f"   {timing_line}")

    return "\n".join(lines)


def _get_updates(bot_token: str, offset: int | None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"timeout": POLL_TIMEOUT_SECONDS}
    if offset is not None:
        params["offset"] = offset
    response = requests.get(
        f"https://api.telegram.org/bot{bot_token}/getUpdates",
        params=params,
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    result: list[dict[str, Any]] = response.json()["result"]
    return result


def _send_message(bot_token: str, chat_id: int, text: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def _read_main_pid() -> int | None:
    """The PID `main.py` wrote at its own boot, or `None` if the file
    doesn't exist (main.py has never run, or was started before this
    feature existed)."""
    if not MAIN_PID_PATH.exists():
        return None
    try:
        return int(MAIN_PID_PATH.read_text().strip())
    except ValueError:
        return None


def _process_command_line(pid: int) -> str | None:
    """Windows-only: the running process's command line for `pid`, or
    `None` if no such process exists. Used to confirm a PID read from
    `MAIN_PID_PATH` still actually belongs to a running `main.py` before
    killing it — guards against a stale file whose PID has since been
    reused by an unrelated process."""
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f'(Get-CimInstance Win32_Process -Filter "ProcessId={pid}").CommandLine',
        ],
        capture_output=True,
        text=True,
        timeout=_PROCESS_CONTROL_TIMEOUT_SECONDS,
    )
    output = result.stdout.strip()
    return output or None


def _is_main_process_alive() -> bool | None:
    """Whether `main.py`'s OS process is actually running right now,
    checked directly against its PID file — the same mechanism
    `_kill_main_process()` verifies against before killing. Returns
    `None` (can't determine, `format_status_message()` then falls back to
    heartbeat staleness alone) when the check isn't possible: non-Windows,
    or no PID file has ever been written (main.py never ran, or ran
    before this feature existed).
    """
    if platform.system() != "Windows":
        return None
    pid = _read_main_pid()
    if pid is None:
        return None
    command_line = _process_command_line(pid)
    return command_line is not None and "main.py" in command_line.lower()


def _kill_main_process() -> str:
    """Terminate the OS process `main.py` wrote its PID for, verifying
    first that the PID still actually belongs to a running `main.py` —
    never kills blind on a possibly-stale/reused PID."""
    if platform.system() != "Windows":
        return "❌ /killbot รองรับเฉพาะ Windows เท่านั้น"

    pid = _read_main_pid()
    if pid is None:
        return "⚠️ ไม่พบข้อมูล PID ของ main.py (main.py อาจไม่เคยรันเลย)"

    command_line = _process_command_line(pid)
    if command_line is None or "main.py" not in command_line.lower():
        return (
            f"⚠️ PID {pid} ไม่ใช่ main.py ที่กำลังทำงานอยู่แล้ว (อาจถูกปิดไปก่อนหน้านี้) "
            "— ไม่ได้ทำอะไรเพื่อความปลอดภัย"
        )

    result = subprocess.run(
        ["taskkill", "/PID", str(pid), "/F"],
        capture_output=True,
        text=True,
        timeout=_PROCESS_CONTROL_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return f"❌ พยายามหยุด main.py (PID {pid}) แต่ล้มเหลว: {result.stderr.strip()}"

    return (
        f"🛑 หยุดการทำงานของบอททั้งหมดแล้ว (PID {pid}) — "
        "ต้องไปรัน python main.py ใหม่เองที่เครื่องเพื่อเริ่มทำงานอีกครั้ง"
    )


def _handle_command(
    config: TelegramConfig, state_manager: StateManager, *, command: str, chat_id: int
) -> str:
    """Dispatch one recognized command to its effect + reply text.

    `/killbot` is recorded to the Audit Trail (`AuditActionType.MANUAL_OVERRIDE`)
    the same way any other state-altering administrative command is
    (`docs/PRODUCTION_SPEC.md` §7) — `actor` is the Telegram chat_id,
    hashed before storage by `record_audit_event()` itself. `/check` and
    `/checkhisorder` are read-only and record nothing.
    """
    if command == KILL_COMMAND:
        reply = _kill_main_process()
        state_manager.record_audit_event(
            actor=f"telegram_chat_{chat_id}",
            action_type=AuditActionType.MANUAL_OVERRIDE,
            parameter_name="main_process_killed",
            old_value=None,
            new_value=reply,
        )
        return reply

    if command == CHECK_HISTORY_COMMAND:
        trades = state_manager.get_recent_trades(limit=ORDER_HISTORY_LIMIT)
        return format_order_history_message(trades)

    heartbeat = state_manager.get_heartbeat()
    process_alive = _is_main_process_alive()
    return format_status_message(
        heartbeat,
        now=datetime.now(timezone.utc),
        process_alive=process_alive,
    )


def run_telegram_bot(config: TelegramConfig, state_manager: StateManager) -> None:
    """Long-poll Telegram's `getUpdates` forever, dispatching
    `/check`/`/killbot`/`/checkhisorder` from an allowed chat.

    A message from a chat_id not in `config.allowed_chat_ids` is silently
    ignored (logged, not replied to) rather than told "unauthorized" —
    this is a personal monitoring/control bot, not a public one, so there
    is no reason to confirm its existence/behavior to an unrecognized
    chat.
    """
    offset: int | None = None
    logger.info("Telegram status bot started; allowed chat ids: %s", config.allowed_chat_ids)
    while True:
        try:
            updates = _get_updates(config.bot_token, offset)
        except requests.exceptions.RequestException:
            logger.exception("Telegram getUpdates failed; retrying in %.0fs.", RETRY_DELAY_SECONDS)
            time.sleep(RETRY_DELAY_SECONDS)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message")
            if message is None:
                continue
            text = message.get("text", "").strip()
            if text not in RECOGNIZED_COMMANDS:
                continue
            chat_id = message["chat"]["id"]
            if chat_id not in config.allowed_chat_ids:
                logger.warning("Ignoring %s from unauthorized chat_id=%s", text, chat_id)
                continue

            reply = _handle_command(config, state_manager, command=text, chat_id=chat_id)
            try:
                _send_message(config.bot_token, chat_id, reply)
            except requests.exceptions.RequestException:
                logger.exception("Failed to send %s reply to chat_id=%s", text, chat_id)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = TelegramConfig.from_env()
    state_manager = StateManager()
    run_telegram_bot(config, state_manager)


if __name__ == "__main__":
    main()
