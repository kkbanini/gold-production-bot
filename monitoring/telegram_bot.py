"""Telegram status/control bot: `/check` reports whether `main.py`'s
bar-close loop is alive and which `TRADING_MODE` it's running under, plus
a live account summary; `/killbot` terminates the `main.py` OS process
outright; `/checkhisorder` reports the most recent trade_ledger rows;
`/condition` reports which of the regular strategy's entry conditions
are currently satisfied.

Runs as its own standalone, long-polling process — never imported by
`main.py`/`container.py` (the reverse import, `_fetch_condition_summary()`
pulling `main._fetch_market_snapshot()`, is fine: `main.py` has no
knowledge of this module and no cycle results). `/killbot` needs to send
a real OS signal and
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

from broker.mt5_gateway import (
    TIMEFRAME_H1,
    AccountState,
    BrokerConnectionError,
    BrokerPosition,
    MT5Gateway,
)
from config.config_manager import ConfigManager, ConfigurationError
from config.telegram_config import TelegramConfig
from indicators.math_engine import bollinger_bands, macd, rsi, sma
from main import H1_BAR_COUNT, _fetch_market_snapshot
from optimizer.self_learning import get_effective_parameter_value
from risk.drawdown_fsm import DrawdownState, blocks_new_entries
from storage.db_engine import MAIN_PID_PATH
from storage.state_manager import AuditActionType, HeartbeatInfo, StateManager, TradeLedgerEntry
from strategy.execution_triggers import BreakoutSignal, PullbackSignal, WickFillResult
from strategy.trend_filter import ADX_TREND_THRESHOLD, TrendAlignment

logger = logging.getLogger(__name__)

CHECK_COMMAND = "/check"
KILL_COMMAND = "/killbot"
CHECK_HISTORY_COMMAND = "/checkhisorder"
CONDITION_COMMAND = "/condition"
RECOGNIZED_COMMANDS = (CHECK_COMMAND, KILL_COMMAND, CHECK_HISTORY_COMMAND, CONDITION_COMMAND)

# How many of the most recent trade_ledger rows /checkhisorder reports —
# the number the user asked for, not derived from anything else.
ORDER_HISTORY_LIMIT = 7

# /condition's monitoring-only MA/RSI/MACD/Bollinger Bands readout
# (indicators/math_engine.py's module docstring): conventional textbook
# defaults for each indicator, made-up-but-documented like this project's
# other numeric defaults — not tuned against this account/instrument, and
# never consulted by any actual trading decision in main.py.
MA_PERIOD = 20
RSI_PERIOD = 14
MACD_FAST_PERIOD = 12
MACD_SLOW_PERIOD = 26
MACD_SIGNAL_PERIOD = 9
BOLLINGER_PERIOD = 20
BOLLINGER_NUM_STD = 2.0
RSI_OVERBOUGHT_THRESHOLD = 70.0
RSI_OVERSOLD_THRESHOLD = 30.0

# `backtester/signal_validation.py`'s measured out-of-sample directional
# accuracy of `summarize_indicator_signal()` against the real 2023-03-20
# to 2026-07 XAUUSD H1 history (70% in-sample / 30% out-of-sample split,
# this module's own default `horizon_bars=4`/`min_abs_score=1`): 49.7%
# overall (BUY 50.8%, SELL 48.5%) — statistically indistinguishable from
# a coin flip. Every other `(min_abs_score, horizon_bars)` combination
# tried (1-2 x 2/4/8/12; 3-4 never even fired a prediction, since all 4
# votes essentially never agree in real data) landed in the same 48-51%
# band — no combination showed a real, non-noise improvement. A point-in-
# time measurement, not a live-updating statistic; flagged for periodic
# re-validation as more history accrues, not treated as permanently fixed.
MEASURED_SIGNAL_OOS_ACCURACY = 0.497

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


def format_account_message(account: AccountState, positions: list[BrokerPosition]) -> str:
    """Pure: render `/check`'s account-summary section from a live
    `AccountState` + every open position on the account (`MT5Gateway.
    get_all_open_positions()` — unfiltered by magic number, so a manually
    opened position is reported here too, matching what the account
    actually looks like in the broker's own terminal/client portal).
    """
    profit_sign = "+" if account.floating_profit >= 0 else "-"
    lines = [
        "📊 ข้อมูลบัญชีล่าสุด:",
        f"Balance: ${account.balance:,.2f}",
        f"Equity: ${account.equity:,.2f}",
        f"Margin ใช้ไป: ${account.margin_used:,.2f}",
        f"Margin ว่าง: ${account.margin_free:,.2f}",
        f"Leverage: 1:{account.leverage}",
        f"กำไร/ขาดทุนลอย: {profit_sign}${abs(account.floating_profit):,.2f}",
    ]
    if not positions:
        lines.append("Position เปิดอยู่: ไม่มี")
    else:
        lines.append(f"Position เปิดอยู่: มี {len(positions)} รายการ")
        for position in positions:
            position_sign = "+" if position.profit >= 0 else "-"
            lines.append(
                f"  • {position.side} {position.volume} {position.symbol} "
                f"@ {position.price_open} (magic {position.magic}) "
                f"— {position_sign}${abs(position.profit):,.2f}"
            )
    return "\n".join(lines)


def _fetch_account_summary(gateway: MT5Gateway | None) -> str:
    """None-safe + connection-safe wrapper: `gateway` is `None` when MT5
    credentials aren't configured for this process (kept optional so a
    missing/invalid `.env` only degrades `/check`'s account section,
    never blocks the rest of the monitoring bot from running). A dropped
    connection is retried once inline — `main.py`'s own loop handles
    sustained reconnection; this is a much lower-frequency, on-demand
    caller, so a single retry is enough rather than that same backoff
    machinery.
    """
    if gateway is None:
        return "⚠️ ไม่ได้ตั้งค่า MT5 สำหรับบอทมอนิเตอร์นี้ — ไม่มีข้อมูลบัญชีให้แสดง"
    try:
        account = gateway.get_account_state()
        positions = gateway.get_all_open_positions()
    except BrokerConnectionError:
        try:
            gateway.connect()
            account = gateway.get_account_state()
            positions = gateway.get_all_open_positions()
        except BrokerConnectionError:
            return "❌ ไม่สามารถดึงข้อมูลบัญชีจาก MT5 ได้ตอนนี้"
    return format_account_message(account, positions)


def format_condition_message(
    *,
    is_locked: bool,
    drawdown_state_label: str,
    has_position: bool,
    trend: TrendAlignment,
    breakout: BreakoutSignal,
    pullback: PullbackSignal,
    wick_fill: WickFillResult,
    adx_trend_threshold: float,
) -> str:
    """Pure: render `/condition`'s reply — every gate the regular
    (`WAIT_FOR_CONDITIONS`) strategy's new-entry decision must clear, each
    marked satisfied/not so a human can tell at a glance which single gate
    is still blocking an entry, mirroring the same conditions
    `run_bar_close_cycle()`'s `context.position is None` branch checks:
    drawdown not locked, no position already open, D1+H4+H1 trend
    alignment, H1 ADX(14) confirmation, and a trigger signal
    (breakout/pullback/wick-fill).
    """

    def mark(ok: bool) -> str:
        return "✅" if ok else "❌"

    has_trigger = (
        breakout.direction != "NONE"
        or pullback.direction != "NONE"
        or wick_fill.rejection != "NONE"
    )

    return "\n".join(
        [
            "🔍 เงื่อนไขการเปิดออเดอร์ (โหมดปกติ):",
            f"{mark(not is_locked)} Drawdown ไม่ติดล็อก (สถานะ: {drawdown_state_label})",
            f"{mark(not has_position)} ไม่มี position เปิดอยู่แล้ว",
            f"{mark(trend.direction != 'NONE')} เทรนด์ D1+H4+H1 ตรงกัน (ปัจจุบัน: {trend.direction})",
            f"{mark(trend.adx_confirmed)} ADX ยืนยันเทรนด์แรงพอ "
            f"({trend.adx_value:.1f} / ต้อง ≥ {adx_trend_threshold:.1f})",
            f"{mark(has_trigger)} มี trigger signal (breakout/pullback/wick-fill)",
        ]
    )


def _fetch_condition_summary(
    gateway: MT5Gateway | None, mt5_config: ConfigManager | None, state_manager: StateManager
) -> str:
    """None-safe + connection-safe wrapper mirroring
    `_fetch_account_summary()`'s degrade-gracefully shape. Drawdown/
    position status comes from the last-persisted FSM snapshot
    (`StateManager.load_fsm_state()` — the same one `main.py`'s own loop
    writes every cycle via `save_fsm_state()`), not a fresh recomputation:
    this command is a read-only observer, never a second decision-maker.
    """
    if gateway is None or mt5_config is None:
        return "⚠️ ไม่ได้ตั้งค่า MT5 สำหรับบอทมอนิเตอร์นี้ — ไม่สามารถเช็คเงื่อนไขได้"

    saved = state_manager.load_fsm_state()
    fsm_state = saved[0] if saved is not None else {}
    drawdown_state = DrawdownState(fsm_state.get("drawdown_state", DrawdownState.ACTIVE.value))
    has_position = fsm_state.get("position") is not None

    effective_adx_threshold = get_effective_parameter_value(
        state_manager, "ADX_TREND_THRESHOLD", ADX_TREND_THRESHOLD
    )
    try:
        snapshot = _fetch_market_snapshot(
            gateway,
            mt5_config.strategy_magic_number,
            [],
            adx_trend_threshold=effective_adx_threshold,
        )
    except BrokerConnectionError:
        try:
            gateway.connect()
            snapshot = _fetch_market_snapshot(
                gateway,
                mt5_config.strategy_magic_number,
                [],
                adx_trend_threshold=effective_adx_threshold,
            )
        except BrokerConnectionError:
            return "❌ ไม่สามารถดึงข้อมูลตลาดจาก MT5 ได้ตอนนี้"

    return format_condition_message(
        is_locked=blocks_new_entries(drawdown_state),
        drawdown_state_label=drawdown_state.value,
        has_position=has_position,
        trend=snapshot.trend,
        breakout=snapshot.breakout,
        pullback=snapshot.pullback,
        wick_fill=snapshot.wick_fill,
        adx_trend_threshold=effective_adx_threshold,
    )


def summarize_indicator_signal(
    *,
    current_price: float,
    ma_value: float,
    rsi_value: float,
    macd_histogram: float,
    bollinger_upper: float,
    bollinger_lower: float,
    min_abs_score: float = 1,
) -> tuple[str, int]:
    """Pure: a simple additive-vote heuristic combining the four
    monitoring-only indicators into one BUY/SELL/HOLD suggestion, purely
    for a human reading `/condition` — this is a separate, much simpler
    rule from (and never fed into) `main.py`'s actual entry decision
    (D1+H4+H1 trend alignment + ADX + trigger signal). Each indicator
    casts one vote in `[-1, 0, 1]`:

    - MA: trend-following — price above MA votes bullish (+1), below
      votes bearish (-1).
    - RSI: mean-reversion — oversold (<= `RSI_OVERSOLD_THRESHOLD`) votes
      bullish (potential bounce, +1), overbought (>=
      `RSI_OVERBOUGHT_THRESHOLD`) votes bearish (-1), otherwise neutral (0).
    - MACD: momentum — a non-negative histogram (MACD line at or above
      its signal line) votes bullish (+1), otherwise bearish (-1).
    - Bollinger Bands: mean-reversion — price at/below the lower band
      votes bullish (+1), at/above the upper band votes bearish (-1),
      otherwise neutral (0).

    Returns `(label, score)` where `score` is the vote sum in `[-4, 4]`;
    `label` is `"BUY"` if `score >= min_abs_score`, `"SELL"` if `score <=
    -min_abs_score`, else `"HOLD"`. `min_abs_score` defaults to `1` (every
    prior behavior preserved exactly, requiring only one net vote to call
    a direction). Exists so `backtester/signal_validation.py` can
    empirically test raising this threshold (requiring 2+ net agreeing
    votes before calling a direction) against real history, rather than
    guessing whether a stricter threshold actually improves accuracy.
    """
    ma_vote = 1 if current_price >= ma_value else -1
    if rsi_value <= RSI_OVERSOLD_THRESHOLD:
        rsi_vote = 1
    elif rsi_value >= RSI_OVERBOUGHT_THRESHOLD:
        rsi_vote = -1
    else:
        rsi_vote = 0
    macd_vote = 1 if macd_histogram >= 0 else -1
    if current_price <= bollinger_lower:
        bollinger_vote = 1
    elif current_price >= bollinger_upper:
        bollinger_vote = -1
    else:
        bollinger_vote = 0

    score = ma_vote + rsi_vote + macd_vote + bollinger_vote
    if score >= min_abs_score:
        label = "BUY"
    elif score <= -min_abs_score:
        label = "SELL"
    else:
        label = "HOLD"
    return label, score


def format_indicator_message(
    *,
    current_price: float,
    ma_period: int,
    ma_value: float,
    rsi_period: int,
    rsi_value: float,
    macd_line: float,
    macd_signal: float,
    macd_histogram: float,
    bollinger_period: int,
    bollinger_upper: float,
    bollinger_middle: float,
    bollinger_lower: float,
) -> str:
    """Pure: render `/condition`'s extra MA/RSI/MACD/Bollinger Bands
    readout plus `summarize_indicator_signal()`'s suggestion — monitoring-
    only (`indicators/math_engine.py`'s module docstring): informational
    context for a human reading `/condition`, never consulted by any
    actual entry/position-management decision in `main.py`.
    """
    if rsi_value >= RSI_OVERBOUGHT_THRESHOLD:
        rsi_label = "overbought"
    elif rsi_value <= RSI_OVERSOLD_THRESHOLD:
        rsi_label = "oversold"
    else:
        rsi_label = "ปกติ"

    macd_label = "โมเมนตัมขาขึ้น" if macd_histogram >= 0 else "โมเมนตัมขาลง"

    if current_price >= bollinger_upper:
        band_label = "ชนแถบบน (overbought)"
    elif current_price <= bollinger_lower:
        band_label = "ชนแถบล่าง (oversold)"
    else:
        band_label = "อยู่ในแถบกลาง"

    ma_label = "สูงกว่า" if current_price >= ma_value else "ต่ำกว่า"

    signal_label, signal_score = summarize_indicator_signal(
        current_price=current_price,
        ma_value=ma_value,
        rsi_value=rsi_value,
        macd_histogram=macd_histogram,
        bollinger_upper=bollinger_upper,
        bollinger_lower=bollinger_lower,
    )

    return "\n".join(
        [
            "📈 อินดิเคเตอร์เพิ่มเติม (H1, สำหรับดูเฉยๆ ไม่มีผลต่อการเทรด):",
            f"MA({ma_period}): {ma_value:.2f} (ราคาปัจจุบัน {ma_label} MA)",
            f"RSI({rsi_period}): {rsi_value:.1f} ({rsi_label})",
            f"MACD: line {macd_line:.3f} / signal {macd_signal:.3f} / "
            f"histogram {macd_histogram:.3f} ({macd_label})",
            f"Bollinger Bands({bollinger_period}): upper {bollinger_upper:.2f} / "
            f"middle {bollinger_middle:.2f} / lower {bollinger_lower:.2f} — {band_label}",
            "",
            f"🧭 สรุปสัญญาณ (heuristic ง่ายๆ รวมคะแนน 4 อินดิเคเตอร์ข้างบน): "
            f"{signal_label} (คะแนน {signal_score:+d}/4)",
            f"⚠️ ทดสอบย้อนหลังจริงแล้ว (out-of-sample กับข้อมูลจริง ~1 ปี): "
            f"ความแม่นยำวัดได้ ~{MEASURED_SIGNAL_OOS_ACCURACY:.0%} "
            "ใกล้เคียงการเดาสุ่ม ไม่ใช่คำแนะนำการลงทุน และไม่ใช่เงื่อนไขที่บอทใช้เปิดออเดอร์จริง",
        ]
    )


def _fetch_indicator_summary(gateway: MT5Gateway | None) -> str:
    """None-safe + connection-safe wrapper mirroring
    `_fetch_account_summary()`'s degrade-gracefully shape: computes the
    monitoring-only MA/RSI/MACD/Bollinger Bands readout from live H1 bars.
    """
    if gateway is None:
        return "⚠️ ไม่ได้ตั้งค่า MT5 สำหรับบอทมอนิเตอร์นี้ — ไม่สามารถคำนวณอินดิเคเตอร์ได้"
    try:
        h1_bars = gateway.get_bars(TIMEFRAME_H1, H1_BAR_COUNT)
        current_price = gateway.get_current_price()
    except BrokerConnectionError:
        try:
            gateway.connect()
            h1_bars = gateway.get_bars(TIMEFRAME_H1, H1_BAR_COUNT)
            current_price = gateway.get_current_price()
        except BrokerConnectionError:
            return "❌ ไม่สามารถดึงข้อมูลราคาจาก MT5 ได้ตอนนี้"

    closes = h1_bars.close
    ma_value = float(sma(closes, MA_PERIOD)[-1])
    rsi_value = float(rsi(closes, RSI_PERIOD)[-1])
    macd_line, macd_signal, macd_histogram = macd(
        closes,
        fast_period=MACD_FAST_PERIOD,
        slow_period=MACD_SLOW_PERIOD,
        signal_period=MACD_SIGNAL_PERIOD,
    )
    upper, middle, lower = bollinger_bands(
        closes, period=BOLLINGER_PERIOD, num_std=BOLLINGER_NUM_STD
    )

    return format_indicator_message(
        current_price=current_price,
        ma_period=MA_PERIOD,
        ma_value=ma_value,
        rsi_period=RSI_PERIOD,
        rsi_value=rsi_value,
        macd_line=float(macd_line[-1]),
        macd_signal=float(macd_signal[-1]),
        macd_histogram=float(macd_histogram[-1]),
        bollinger_period=BOLLINGER_PERIOD,
        bollinger_upper=float(upper[-1]),
        bollinger_middle=float(middle[-1]),
        bollinger_lower=float(lower[-1]),
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
    config: TelegramConfig,
    state_manager: StateManager,
    *,
    command: str,
    chat_id: int,
    gateway: MT5Gateway | None = None,
    mt5_config: ConfigManager | None = None,
) -> str:
    """Dispatch one recognized command to its effect + reply text.

    `/killbot` is recorded to the Audit Trail (`AuditActionType.MANUAL_OVERRIDE`)
    the same way any other state-altering administrative command is
    (`docs/PRODUCTION_SPEC.md` §7) — `actor` is the Telegram chat_id,
    hashed before storage by `record_audit_event()` itself. `/check`,
    `/checkhisorder`, and `/condition` are read-only and record nothing.
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

    if command == CONDITION_COMMAND:
        condition_summary = _fetch_condition_summary(gateway, mt5_config, state_manager)
        return f"{condition_summary}\n\n{_fetch_indicator_summary(gateway)}"

    heartbeat = state_manager.get_heartbeat()
    process_alive = _is_main_process_alive()
    status_message = format_status_message(
        heartbeat,
        now=datetime.now(timezone.utc),
        process_alive=process_alive,
    )
    return f"{status_message}\n\n{_fetch_account_summary(gateway)}"


def run_telegram_bot(
    config: TelegramConfig,
    state_manager: StateManager,
    *,
    gateway: MT5Gateway | None = None,
    mt5_config: ConfigManager | None = None,
) -> None:
    """Long-poll Telegram's `getUpdates` forever, dispatching
    `/check`/`/killbot`/`/checkhisorder`/`/condition` from an allowed chat.

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

            reply = _handle_command(
                config,
                state_manager,
                command=text,
                chat_id=chat_id,
                gateway=gateway,
                mt5_config=mt5_config,
            )
            try:
                _send_message(config.bot_token, chat_id, reply)
            except requests.exceptions.RequestException:
                logger.exception("Failed to send %s reply to chat_id=%s", text, chat_id)


def _build_gateway_from_env() -> tuple[MT5Gateway, ConfigManager] | None:
    """`/check`'s account summary and `/condition`'s market snapshot both
    need their own MT5 connection — this process never shares `main.py`'s
    (a separate OS process entirely). Returns `None` (rather than raising)
    on a missing/invalid MT5 config or a failed initial connection, so a
    monitoring-only deployment without MT5 credentials configured for it
    still runs; those two commands then just report themselves as
    unavailable. The `ConfigManager` is returned alongside the gateway
    since `/condition` needs `strategy_magic_number` to scope its market
    snapshot the same way `main.py` itself does.
    """
    try:
        config = ConfigManager.load()
    except ConfigurationError:
        logger.warning("MT5 not configured for this process; /check will skip account info.")
        return None
    gateway = MT5Gateway(
        login=config.mt5_login,
        password=config.mt5_password,
        server=config.mt5_server,
        magic_number=config.strategy_magic_number,
    )
    try:
        gateway.connect()
    except BrokerConnectionError:
        logger.exception("Could not connect to MT5; /check will skip account info.")
        return None
    return gateway, config


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = TelegramConfig.from_env()
    state_manager = StateManager()
    gateway_and_config = _build_gateway_from_env()
    gateway = gateway_and_config[0] if gateway_and_config is not None else None
    mt5_config = gateway_and_config[1] if gateway_and_config is not None else None
    run_telegram_bot(config, state_manager, gateway=gateway, mt5_config=mt5_config)


if __name__ == "__main__":
    main()
