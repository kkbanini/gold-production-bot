# monitoring/

## Responsibility

External liveness/status reporting and remote control for the running
bot — a concern that doesn't belong to any single existing package,
carved out the same way `resilience/` was for cross-cutting network
resiliency. Not part of the original Phase 0 module scaffold.

Two directions, two modules:

- **Pull** (`telegram_bot.py`, its own standalone process): is `main.py`'s
  bar-close loop still alive, and under which `TRADING_MODE` (`/check`)?
  What did the last few orders do (`/checkhisorder`)? For a genuine
  emergency, terminate `main.py`'s OS process outright (`/killbot`).
- **Push** (`notifier.py`, a tiny utility `main.py`'s own process calls):
  fire-and-forget Telegram alerts the moment something noteworthy happens
  — bot started, entry filled, short-term close reconciled (with real
  profit), `HARD_LOCK` emergency liquidation, MT5 reconnect, crash — so
  the operator hears about it immediately instead of only when they think
  to poll. `build_notifier_from_env()` returns `None` when `TELEGRAM_*`
  isn't configured (an optional add-on, never a boot requirement), and
  `TelegramNotifier.send()` swallows every exception after logging it —
  a notification failure can never take down or delay the bar-close loop
  it reports on. Held on the container as `ApplicationContainer.notifier`;
  `main._notify()` is the None-safe call-site wrapper.

## Implementation

`telegram_bot.py`:

- `format_status_message(heartbeat, *, now, process_alive=None, stale_after=HEARTBEAT_STALE_AFTER)`
  — pure: renders the `/check` reply from a `storage.state_manager.HeartbeatInfo`
  (or `None` if `main.py` has never run against this database file) and
  (when known) whether `main.py`'s OS process is actually running.
  Liveness priority: `process_alive is False` reports "หยุดทำงาน"
  (stopped) immediately, even under a still-fresh heartbeat — the exact
  `/killbot` scenario, where the PID is confirmed gone within seconds, far
  sooner than the heartbeat itself would ever go stale. Otherwise falls
  back to heartbeat staleness: older than `stale_after` also reads as
  stopped (a hang/crash that didn't exit the process). `HEARTBEAT_STALE_AFTER`
  (20 minutes, made-up-but-documented) is larger than both cadences
  `main.py`'s loop ever heartbeats at — the normal ~5-minute M5 bar-close
  wait, and the 900s `WEEKEND_RECHECK_SECONDS` weekend-skip loop.
- `_is_main_process_alive()` — the same PID-file-then-`Get-CimInstance`
  check `_kill_main_process()` verifies against before killing, reused
  here read-only for `/check`. Returns `None` (can't determine) on
  non-Windows or when no PID file has ever been written.
- `format_order_history_message(trades)` — pure: renders
  `/checkhisorder`'s reply from a list of `storage.state_manager.TradeLedgerEntry`
  (already most-recent-first, per `get_recent_trades()`) — side, volume,
  symbol, `OPEN`/`CLOSED` status, profit (when closed), and both
  opened/closed timestamps. Never shows `client_order_id` (internal-only).
- `_handle_command(config, state_manager, *, command, chat_id)` —
  dispatches one recognized command: `/check` reads
  `state_manager.get_heartbeat()` and `_is_main_process_alive()` fresh;
  `/checkhisorder` reads `state_manager.get_recent_trades(limit=ORDER_HISTORY_LIMIT)`;
  `/killbot` calls `_kill_main_process()`. Only `/killbot` records to the
  Audit Trail (`AuditActionType.MANUAL_OVERRIDE`, `docs/PRODUCTION_SPEC.md`
  §7 — the same mechanism any other state-altering administrative command
  uses); `/check` and `/checkhisorder` are read-only and record nothing.
- `_kill_main_process()` — Windows-only (`platform.system() != "Windows"`
  refuses outright): reads `main.py`'s PID from `MAIN_PID_PATH`
  (`storage/db_engine.py`), re-queries the OS for that PID's actual
  command line (`Get-CimInstance Win32_Process` via `subprocess`) to
  confirm it still contains `main.py` before doing anything — a stale PID
  whose process has since exited (or, worse, been reused by an unrelated
  process — `main.py` has no graceful-shutdown path today, so `main.pid`
  can outlive the process that wrote it) is refused rather than acted on
  — then `taskkill /PID <pid> /F`.
- `run_telegram_bot(config, state_manager)` — the impure long-polling
  loop: calls Telegram's `getUpdates` (30s server-side long-poll timeout),
  dispatches any of `RECOGNIZED_COMMANDS` from a `config.allowed_chat_ids`
  chat via `_handle_command()`, and replies via `sendMessage`. A
  recognized command from an unrecognized chat is silently ignored
  (logged, not replied to) — this is a personal monitoring/control bot,
  not a public one.
- `main()` — the standalone entry point: `TelegramConfig.from_env()` +
  `StateManager()`, then `run_telegram_bot()` forever.

## Liveness signal (`storage/`)

`storage/state_manager.py`'s `record_heartbeat(trading_mode)` /
`get_heartbeat()` own the single-row `bot_heartbeat` table (same
pinned-singleton pattern as `system_state`). `main.py`'s bar-close loop
calls `record_heartbeat()` once per iteration — including its
weekend-market-closed skip branch, so a closed weekend market is never
mistaken for a stopped process.

## Order history (`storage/`)

`storage/state_manager.py`'s `get_recent_trades(limit=7)` returns the
`limit` most recently opened `trade_ledger` rows — both still-`OPEN` and
`CLOSED` — most recent first, ordered by the table's own `AUTOINCREMENT`
id rather than `opened_at_utc` (a stored string, not guaranteed
collision-free). Read-only; `/checkhisorder` is its only caller.
`ORDER_HISTORY_LIMIT` (7) is exactly what the user asked for, not derived
from anything else.

## Process handle (`storage/`)

`storage/db_engine.py`'s `MAIN_PID_PATH` (`storage/main.pid`, gitignored)
is the one thing `main.py` writes and `monitoring/telegram_bot.py` reads
that isn't a DB table — colocated with `DEFAULT_DB_PATH` so neither
module imports the other directly, both just depend on `storage/`.
`main.py` writes its own `os.getpid()` there once at boot (overwritten
fresh on every restart). `/killbot` is the only reader.

## Running it

A separate, standalone process from `main.py` — start it independently:

```
python -m monitoring.telegram_bot
```

Requires `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_CHAT_ID` in `.env`
(see `.env.template`). This process opens a normal (writable)
`StateManager()` — `/killbot` needs to record its action to the Audit
Trail. That's its only write; it never touches `system_state` or an open
`trade_ledger` row, the same isolation guarantee `parameter_history`
gives `optimizer/self_learning.py`. SQLite's WAL mode
(`storage/db_engine.py`) is explicitly designed to support more than one
writer process, serialized via its own file locking plus `busy_timeout`;
writes here are rare (only on an explicit `/killbot`), so contention with
`main.py`'s own frequent heartbeat writes is not a practical concern. If
`storage/gold_bot.db` doesn't exist yet, this process creates the schema
itself (same `initialize_schema()` / `apply_pending_migrations()` every
`StateManager()` already runs) rather than requiring `main.py` to have
run first.

`/killbot` is not reversible from Telegram: once `main.py`'s process is
gone, restarting requires manually running `python main.py` again on the
host — the same manual step needed today after any other reason `main.py`
might stop (a crash, a host reboot, an operator's own Ctrl+C).

## Depends On

`storage/state_manager.py` (`HeartbeatInfo`, `TradeLedgerEntry`,
`AuditActionType`, `get_heartbeat`/`record_heartbeat`,
`get_recent_trades`, `record_audit_event`), `storage/db_engine.py`
(`MAIN_PID_PATH`), `config/telegram_config.py`, `requests` (already a
project dependency — no `python-telegram-bot` package added; Telegram's
Bot API is plain HTTPS, and this bot only needs
`getUpdates`/`sendMessage`), and the standard library's `subprocess`/
`platform` (`/killbot`'s Windows `taskkill`/`Get-CimInstance` calls).

## Depended On By

- `container.py` builds `notifier.py`'s `TelegramNotifier` (via
  `build_notifier_from_env()`) into `ApplicationContainer.notifier`, and
  `main.py`'s `_notify()` calls it at every noteworthy trading event.
- `telegram_bot.py` remains a leaf — its own standalone process, never
  imported by `main.py`/`container.py`.
