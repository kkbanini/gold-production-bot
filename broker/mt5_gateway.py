"""MetaTrader 5 broker gateway: connection lifecycle, dynamic Gold symbol
resolution, server-time alignment, and magic-number position recovery.

Sole module (along with backtester/'s future historical test double,
ADR-0002) permitted to import MetaTrader5. No other module may call into
the MetaTrader5 package directly (RQ-001).
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import MetaTrader5 as mt5
import numpy as np

from execution.position_manager import OrderActionPayload
from indicators.math_engine import FloatArray
from storage.state_manager import TradeLedgerEntry

# Re-exported MT5 timeframe constants, so callers (e.g. main.py) never need
# to import MetaTrader5 directly (ADR-0002/RQ-001: only broker/ may).
TIMEFRAME_D1 = mt5.TIMEFRAME_D1
TIMEFRAME_H4 = mt5.TIMEFRAME_H4
TIMEFRAME_H1 = mt5.TIMEFRAME_H1
TIMEFRAME_M5 = mt5.TIMEFRAME_M5

GOLD_SYMBOL_CANDIDATES: tuple[str, ...] = (
    "XAUUSD",
    "XAUUSD.m",
    "XAUUSD.a",
    "XAUUSDm",
    "XAUUSD_i",
    "GOLD",
    "GOLD.m",
    "GOLDm",
)

GMT_SESSION_START_HOUR = 7
GMT_SESSION_END_HOUR = 22

# Weekly forex/CFD market closure: Friday 22:00 UTC through Sunday 22:00
# UTC — a common broker convention, not a universal, per-broker-verified
# fact (some brokers use 21:00 UTC instead, depending on daylight saving).
# Made-up-but-documented, same as this project's other invented-but-
# flagged numeric defaults (risk/README.md's compounding tiers, etc.).
WEEKEND_CLOSE_WEEKDAY = 4  # Friday (datetime.weekday(): Monday=0 ... Sunday=6)
WEEKEND_CLOSE_HOUR_UTC = 22
WEEKEND_REOPEN_WEEKDAY = 6  # Sunday
WEEKEND_REOPEN_HOUR_UTC = 22

# Slippage tolerance for partial-close deals, in broker points. A
# placeholder default, not a policy decision — the full slippage guard
# (RQ-010, RR-006) is a later-phase concern.
CLOSE_DEVIATION_POINTS = 20

# The MetaTrader5 Python wrapper rejects long order comments outright —
# order_send()/order_check() return None with last_error
# (-2, 'Invalid "comment" argument') before anything reaches the broker.
# Empirically bisected against MetaTrader5==5.0.4500 on a live demo
# connection: 29 chars accepted, 30+ rejected — NOT the 31 chars MT5's
# own docs suggest, so this clamps to 25 for margin rather than riding
# the exact observed boundary. main.py passes a UUIDv4 client_order_id
# (36 chars) as the comment; the full id is always preserved in the
# storage/ Event Store — the broker-side comment is informational only.
MAX_ORDER_COMMENT_LENGTH = 25


def _clamp_comment(comment: str) -> str:
    return comment[:MAX_ORDER_COMMENT_LENGTH]


class BrokerConnectionError(Exception):
    """Raised when connecting to the MT5 terminal fails after backoff is exhausted."""


class BrokerSymbolUnavailableError(Exception):
    """Raised when no candidate Gold symbol variant is found on the connected broker."""


class BrokerOrderRejectedError(Exception):
    """Raised when MT5 rejects an order/position-modification request (a
    non-DONE retcode from order_send(), or no result/position/tick found
    to build the request from)."""


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    """Resolved broker-specific contract spec for the traded Gold instrument."""

    name: str
    point: float
    digits: int
    tick_value: float
    tick_size: float
    volume_min: float
    volume_max: float
    volume_step: float


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    """A single open position reported by the broker, filtered by magic number."""

    ticket: int
    symbol: str
    side: str
    volume: float
    price_open: float
    price_current: float
    stop_loss: float
    take_profit: float
    profit: float
    magic: int
    opened_at_utc: datetime


@dataclass(frozen=True, slots=True)
class PositionAuditReport:
    """Result of reconciling broker-reported open positions (filtered by this
    gateway's magic number) against the locally persisted trade_ledger.

    A non-empty broker_only_positions or ledger_only_entries means the local
    ledger and the broker's truth have diverged while disconnected (RR-008)
    and must be reviewed before automated trading resumes
    (docs/RUNBOOK.md §1 step 5).
    """

    reconciled_tickets: tuple[int, ...]
    broker_only_positions: tuple[BrokerPosition, ...]
    ledger_only_entries: tuple[TradeLedgerEntry, ...]

    @property
    def is_clean(self) -> bool:
        return not self.broker_only_positions and not self.ledger_only_entries


@dataclass(frozen=True, slots=True)
class DisasterRecoveryPlan:
    """The settlement `docs/PRODUCTION_SPEC.md` §7 requires: "verify data
    integrity against active live tickets... and settle any parameter
    discrepancies before transitioning the FSM." Computed purely from an
    already-fetched `PositionAuditReport` — no I/O of its own. The caller
    (`container.py`) applies `ledger_upserts` via
    `StateManager.record_trade()` and gates FSM startup on
    `requires_manual_review`.
    """

    ledger_upserts: tuple[TradeLedgerEntry, ...]
    requires_manual_review: bool
    summary: str


def resolve_position_audit(
    report: PositionAuditReport, *, reconciled_at_utc: datetime | None = None
) -> DisasterRecoveryPlan:
    """Turn a `PositionAuditReport` into a concrete settlement plan.

    Broker-only positions (open on the broker, no local ledger row) are
    reconstructed into a new `OPEN` `trade_ledger` row from the broker's
    own reported fields, keyed by a deterministic
    `f"disaster-recovery-{ticket}"` `client_order_id` — idempotent:
    re-running reconciliation for the same still-open ticket upserts the
    same row rather than duplicating it (RR-007's usual idempotency
    guarantee, applied here too). Ledger-only entries (the ledger thinks
    it's open, the broker disagrees) are settled by marking them closed at
    `reconciled_at_utc` (the reconciliation moment — the position's real
    close time isn't knowable after the fact, an approximation flagged
    here explicitly).

    Any divergence at all (either list non-empty) sets
    `requires_manual_review=True` — `docs/RUNBOOK.md`'s own established
    policy already treats a position-audit mismatch as a `HIGH`-severity
    RiskBreach (RR-008) that "blocks automated trading pending manual
    reconciliation"; the caller must honor that by not starting the FSM in
    `ACTIVE` when this is `True`.
    """
    now = reconciled_at_utc if reconciled_at_utc is not None else datetime.now(timezone.utc)
    upserts: list[TradeLedgerEntry] = []

    for position in report.broker_only_positions:
        upserts.append(
            TradeLedgerEntry(
                client_order_id=f"disaster-recovery-{position.ticket}",
                symbol=position.symbol,
                side=position.side,
                volume_lots=position.volume,
                status="OPEN",
                opened_at_utc=position.opened_at_utc.isoformat(),
                open_price=position.price_open,
                stop_loss_price=position.stop_loss,
                take_profit_price=position.take_profit,
                profit=position.profit,
                magic_number=position.magic,
                broker_ticket=position.ticket,
            )
        )

    for entry in report.ledger_only_entries:
        upserts.append(
            dataclasses.replace(entry, status="CLOSED_RECONCILED", closed_at_utc=now.isoformat())
        )

    summary = (
        f"reconciled={len(report.reconciled_tickets)} "
        f"broker_only_settled={len(report.broker_only_positions)} "
        f"ledger_only_settled={len(report.ledger_only_entries)}"
    )
    return DisasterRecoveryPlan(
        ledger_upserts=tuple(upserts),
        requires_manual_review=not report.is_clean,
        summary=summary,
    )


@dataclass(frozen=True, slots=True)
class AccountState:
    """A snapshot of the connected account's balance/equity/margin."""

    balance: float
    equity: float
    margin_used: float
    margin_free: float
    as_of_utc: datetime


@dataclass(frozen=True, slots=True)
class BarSeries:
    """A batch of closed bars for one timeframe, as numpy arrays ready for
    `indicators/`/`strategy/` consumption — no `Bar`-per-element overhead."""

    open: FloatArray
    high: FloatArray
    low: FloatArray
    close: FloatArray
    tick_volume: FloatArray
    time_utc: tuple[datetime, ...]


def resolve_gold_symbol(candidates: tuple[str, ...] = GOLD_SYMBOL_CANDIDATES) -> SymbolSpec:
    """Find the broker's Gold symbol among common naming variants and parse
    its contract spec directly from the broker rather than hardcoding it.

    Different brokers expose XAUUSD under different suffixes/aliases (raw
    ECN accounts often append '.m'/'.a'/'_i'; some white-label brokers use
    'GOLD' instead of 'XAUUSD'). Candidates are tried in priority order; the
    first one visible (or made visible via symbol_select) on the connected
    broker wins, and its point size / tick value / tick size / volume
    constraints are read from the broker rather than hardcoded.
    """
    for candidate in candidates:
        info = mt5.symbol_info(candidate)
        if info is None:
            continue
        if not info.visible and not mt5.symbol_select(candidate, True):
            continue
        return SymbolSpec(
            name=info.name,
            point=info.point,
            digits=info.digits,
            tick_value=info.trade_tick_value,
            tick_size=info.trade_tick_size,
            volume_min=info.volume_min,
            volume_max=info.volume_max,
            volume_step=info.volume_step,
        )
    raise BrokerSymbolUnavailableError(
        f"No Gold symbol variant found among candidates {candidates!r} on the connected broker."
    )


def is_within_execution_window(now_utc: datetime) -> bool:
    """True if now_utc falls within the 07:00-22:00 GMT execution filter.

    GMT and UTC share the same civil time year-round (GMT observes no DST),
    so a UTC-aware datetime is compared directly with no further conversion
    (docs/RESEARCH.md §2).
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    hour = now_utc.astimezone(timezone.utc).hour
    return GMT_SESSION_START_HOUR <= hour < GMT_SESSION_END_HOUR


def is_weekend_market_closed(now_utc: datetime) -> bool:
    """True if `now_utc` falls within the weekly forex/CFD market closure:
    Friday 22:00 UTC through Sunday 22:00 UTC.

    This is a separate axis from `is_within_execution_window()`'s daily
    07:00-22:00 GMT filter — that one repeats every day; this one is the
    once-a-week closure. Neither is currently wired into `main.py`'s live
    loop on its own; `main()` calls this one directly to skip a cycle
    entirely during weekend closure (docs/ARCHITECTURE_SUMMARY.md §5 still
    lists `is_within_execution_window()` as unwired).
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    aware = now_utc.astimezone(timezone.utc)
    weekday = aware.weekday()
    hour = aware.hour

    if weekday == WEEKEND_CLOSE_WEEKDAY:
        return hour >= WEEKEND_CLOSE_HOUR_UTC
    if weekday == WEEKEND_REOPEN_WEEKDAY:
        return hour < WEEKEND_REOPEN_HOUR_UTC
    return WEEKEND_CLOSE_WEEKDAY < weekday < WEEKEND_REOPEN_WEEKDAY


class MT5Gateway:
    """Production BrokerGateway adapter over the MetaTrader5 package (ADR-0002).

    Owns the connection lifecycle, the resolved Gold SymbolSpec, and the
    broker-to-UTC time offset. Order submission/cancellation (the remaining
    docs/API_SPEC.md BrokerGateway methods) land in a later phase alongside
    execution/.
    """

    def __init__(self, login: int, password: str, server: str, magic_number: int) -> None:
        self._login = login
        self._password = password
        self._server = server
        self._magic_number = magic_number
        self._symbol_spec: SymbolSpec | None = None
        self._broker_utc_offset: timedelta | None = None

    @property
    def symbol_spec(self) -> SymbolSpec:
        if self._symbol_spec is None:
            raise BrokerConnectionError("connect() must succeed before symbol_spec is available.")
        return self._symbol_spec

    @property
    def broker_utc_offset(self) -> timedelta:
        if self._broker_utc_offset is None:
            raise BrokerConnectionError(
                "connect() must succeed before broker_utc_offset is available."
            )
        return self._broker_utc_offset

    def connect(
        self,
        *,
        max_attempts: int = 6,
        initial_delay_seconds: float = 1.0,
        max_delay_seconds: float = 60.0,
    ) -> None:
        """Connect to the MT5 terminal with exponential backoff on failure.

        Delay doubles each attempt (1s, 2s, 4s, 8s, ... capped at
        max_delay_seconds) up to max_attempts. Raises BrokerConnectionError
        with the last mt5.last_error() detail if every attempt fails (RR-002).
        On success, resolves the Gold SymbolSpec and broker_utc_offset before
        returning, so a caller never observes a half-initialized gateway.
        """
        delay = initial_delay_seconds
        last_error: tuple[int, str] | None = None
        for attempt in range(1, max_attempts + 1):
            if mt5.initialize(login=self._login, password=self._password, server=self._server):
                self._symbol_spec = resolve_gold_symbol()
                self._broker_utc_offset = self._resolve_broker_utc_offset(self._symbol_spec)
                return
            last_error = mt5.last_error()
            if attempt < max_attempts:
                time.sleep(min(delay, max_delay_seconds))
                delay *= 2
        raise BrokerConnectionError(
            f"Failed to connect to MT5 after {max_attempts} attempts. Last error: {last_error!r}"
        )

    def disconnect(self) -> None:
        mt5.shutdown()
        self._symbol_spec = None
        self._broker_utc_offset = None

    @staticmethod
    def _resolve_broker_utc_offset(symbol_spec: SymbolSpec) -> timedelta:
        """Compute the connected broker's server-time offset from UTC using
        the resolved Gold symbol's latest tick (ADR-0002)."""
        tick = mt5.symbol_info_tick(symbol_spec.name)
        if tick is None:
            raise BrokerConnectionError(
                f"Could not read a tick for {symbol_spec.name!r} to resolve broker_utc_offset."
            )
        broker_time = datetime.fromtimestamp(tick.time, tz=timezone.utc)
        return broker_time - datetime.now(timezone.utc)

    def get_open_positions_by_magic(self, magic_number: int | None = None) -> list[BrokerPosition]:
        """Return all open positions on the connected account matching
        `magic_number` (defaults to this gateway's own magic number).

        The override lets a second, distinct strategy — e.g. the
        short-term mode's own magic number (`docs/ARCHITECTURE_SUMMARY.md`)
        — query its own positions through the same shared MT5 terminal
        connection, without needing a second `MT5Gateway` instance.
        `audit_open_positions()`'s no-arg call is unaffected, so Disaster
        Recovery reconciliation still only ever covers this gateway's own
        magic number.
        """
        target_magic = self._magic_number if magic_number is None else magic_number
        positions: Any = mt5.positions_get()
        if not positions:
            return []
        matched: list[BrokerPosition] = []
        for position in positions:
            if position.magic != target_magic:
                continue
            side = "BUY" if position.type == mt5.POSITION_TYPE_BUY else "SELL"
            matched.append(
                BrokerPosition(
                    ticket=position.ticket,
                    symbol=position.symbol,
                    side=side,
                    volume=position.volume,
                    price_open=position.price_open,
                    price_current=position.price_current,
                    stop_loss=position.sl,
                    take_profit=position.tp,
                    profit=position.profit,
                    magic=position.magic,
                    opened_at_utc=datetime.fromtimestamp(position.time, tz=timezone.utc),
                )
            )
        return matched

    def is_ticket_still_open(self, ticket: int) -> bool:
        """Query the broker's live position cache for `ticket` — the
        "query the server cache" half of the pre-flight idempotency audit
        `docs/PRODUCTION_SPEC.md` §4 requires before any automated retry:
        confirms whether a ticket recorded locally from a previous `SENT`
        attempt is still open on the broker, so a retry after an ambiguous
        timeout doesn't blindly resubmit an order that actually landed."""
        positions: Any = mt5.positions_get(ticket=ticket)
        return bool(positions)

    def audit_open_positions(
        self, ledger_open_trades: list[TradeLedgerEntry]
    ) -> PositionAuditReport:
        """Reconcile broker-reported open positions (this gateway's magic
        number) against the locally persisted trade_ledger.

        Called on every successful (re)connection (docs/RUNBOOK.md §1 step 5
        / RR-002 recovery path): a non-empty broker-only or ledger-only set
        means the bot's local view of the world diverged from the broker's
        while disconnected, and must be resolved before automated trading
        resumes (RR-008). This function only detects and reports the
        divergence; resolving it is a caller responsibility.
        """
        broker_positions = self.get_open_positions_by_magic()
        broker_by_ticket = {position.ticket: position for position in broker_positions}
        ledger_by_ticket: dict[int, TradeLedgerEntry] = {
            entry.broker_ticket: entry
            for entry in ledger_open_trades
            if entry.magic_number == self._magic_number and entry.broker_ticket is not None
        }

        reconciled_tickets = tuple(sorted(set(broker_by_ticket) & set(ledger_by_ticket)))
        broker_only = tuple(
            position
            for ticket, position in broker_by_ticket.items()
            if ticket not in ledger_by_ticket
        )
        ledger_only = tuple(
            entry for ticket, entry in ledger_by_ticket.items() if ticket not in broker_by_ticket
        )
        return PositionAuditReport(
            reconciled_tickets=reconciled_tickets,
            broker_only_positions=broker_only,
            ledger_only_entries=ledger_only,
        )

    def submit_position_action(self, payload: OrderActionPayload) -> None:
        """Translate an execution.position_manager.OrderActionPayload into
        a real mt5.order_send() request and submit it.

        Raises BrokerOrderRejectedError if MT5 returns a non-DONE retcode,
        no result at all, or the payload references a ticket/symbol this
        gateway cannot currently resolve on the broker (e.g. the position
        already closed, or no tick is available).
        """
        if payload.action == "TRADE_ACTION_DEAL":
            request = self._build_partial_close_request(payload)
        else:
            request = self._build_modify_sltp_request(payload)

        result: Any = mt5.order_send(request)
        retcode = getattr(result, "retcode", None)
        if result is None or retcode != mt5.TRADE_RETCODE_DONE:
            raise BrokerOrderRejectedError(
                f"order_send failed for ticket {payload.position_ticket}: "
                f"retcode={retcode!r}, last_error={mt5.last_error()!r}"
            )

    def _build_partial_close_request(self, payload: OrderActionPayload) -> dict[str, Any]:
        positions: Any = mt5.positions_get(ticket=payload.position_ticket)
        if not positions:
            raise BrokerOrderRejectedError(
                f"cannot partial-close: no open position found for ticket "
                f"{payload.position_ticket}"
            )
        position = positions[0]
        is_buy_position = position.type == mt5.POSITION_TYPE_BUY
        closing_order_type = mt5.ORDER_TYPE_SELL if is_buy_position else mt5.ORDER_TYPE_BUY

        tick = mt5.symbol_info_tick(payload.symbol)
        if tick is None:
            raise BrokerOrderRejectedError(
                f"cannot partial-close: no tick available for {payload.symbol!r}"
            )
        closing_price = tick.bid if is_buy_position else tick.ask

        return {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": payload.position_ticket,
            "symbol": payload.symbol,
            "volume": payload.volume,
            "type": closing_order_type,
            "price": closing_price,
            "deviation": CLOSE_DEVIATION_POINTS,
            "magic": payload.magic,
            "comment": _clamp_comment(payload.comment),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

    def _build_modify_sltp_request(self, payload: OrderActionPayload) -> dict[str, Any]:
        request: dict[str, Any] = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": payload.position_ticket,
            "symbol": payload.symbol,
            "magic": payload.magic,
            "comment": _clamp_comment(payload.comment),
        }
        if payload.stop_loss is not None:
            request["sl"] = payload.stop_loss
        if payload.take_profit is not None:
            request["tp"] = payload.take_profit
        return request

    def get_account_state(self) -> AccountState:
        """Snapshot the connected account's balance/equity/margin — the
        input `main.py`'s drawdown-breaker checks are computed from."""
        info: Any = mt5.account_info()
        if info is None:
            raise BrokerConnectionError(
                f"account_info() returned None; last_error={mt5.last_error()!r}"
            )
        return AccountState(
            balance=info.balance,
            equity=info.equity,
            margin_used=info.margin,
            margin_free=info.margin_free,
            as_of_utc=datetime.now(timezone.utc),
        )

    def get_bars(self, timeframe: int, count: int) -> BarSeries:
        """Fetch the last `count` *closed* bars for `timeframe` (one of the
        `TIMEFRAME_*` constants re-exported by this module) on the resolved
        Gold symbol.

        Starts at position 1, not 0: MT5's position 0 is the currently
        *forming* bar, and including it silently violated this docstring's
        closed-bars contract — every signal consumer (`strategy/`'s
        2-candle breakout "on the latest two closed bars", the wick-fill
        ratios, ATR) was evaluating a partially-formed bar as if it were
        final. Live price for entry-stop/trailing math comes from
        `get_current_price()` (the latest tick), never from a forming
        bar's close."""
        rates: Any = mt5.copy_rates_from_pos(self.symbol_spec.name, timeframe, 1, count)
        if rates is None or len(rates) == 0:
            raise BrokerConnectionError(
                f"copy_rates_from_pos returned no data for {self.symbol_spec.name!r} "
                f"(timeframe={timeframe}, count={count}); last_error={mt5.last_error()!r}"
            )
        return BarSeries(
            open=np.array([bar["open"] for bar in rates], dtype=np.float64),
            high=np.array([bar["high"] for bar in rates], dtype=np.float64),
            low=np.array([bar["low"] for bar in rates], dtype=np.float64),
            close=np.array([bar["close"] for bar in rates], dtype=np.float64),
            tick_volume=np.array([bar["tick_volume"] for bar in rates], dtype=np.float64),
            time_utc=tuple(datetime.fromtimestamp(bar["time"], tz=timezone.utc) for bar in rates),
        )

    def get_current_price(self) -> float:
        """The resolved Gold symbol's latest bid — the live price reference
        `main.py` uses for entry-stop and trailing-stop math. Bid, not ask
        or mid, because MT5 bars are bid-built, so this stays on the same
        price basis as every bar-derived indicator (ATR/EMA) it's combined
        with."""
        tick = mt5.symbol_info_tick(self.symbol_spec.name)
        if tick is None:
            raise BrokerConnectionError(
                f"symbol_info_tick returned no tick for {self.symbol_spec.name!r}; "
                f"last_error={mt5.last_error()!r}"
            )
        return float(tick.bid)

    def submit_market_order(
        self,
        side: Literal["BUY", "SELL"],
        volume: float,
        stop_loss: float,
        take_profit: float | None,
        comment: str,
        *,
        magic_number: int | None = None,
    ) -> BrokerPosition:
        """Submit a new market order to open a position.

        This opens a *new* position (docs/API_SPEC.md's `submit_order`);
        it does not perform the pre-trade risk gate, slippage guard, or
        duplicate-submission idempotency check (RQ-009/RQ-010, RR-007) —
        those remain the caller's responsibility (`main.py`), and are not
        yet a dedicated `execution/` risk gate module (still an open gap,
        see `docs/TRACEABILITY_MATRIX.md`).

        `magic_number` defaults to this gateway's own magic number; passing
        an override (e.g. the short-term mode's distinct magic number,
        `docs/ARCHITECTURE_SUMMARY.md`) submits under that value instead,
        without needing a second `MT5Gateway` instance/connection.
        """
        tick = mt5.symbol_info_tick(self.symbol_spec.name)
        if tick is None:
            raise BrokerOrderRejectedError(
                f"cannot open position: no tick available for {self.symbol_spec.name!r}"
            )
        order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        price = tick.ask if side == "BUY" else tick.bid
        target_magic = self._magic_number if magic_number is None else magic_number

        request: dict[str, Any] = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol_spec.name,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": stop_loss,
            "deviation": CLOSE_DEVIATION_POINTS,
            "magic": target_magic,
            "comment": _clamp_comment(comment),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if take_profit is not None:
            request["tp"] = take_profit

        result: Any = mt5.order_send(request)
        retcode = getattr(result, "retcode", None)
        if result is None or retcode != mt5.TRADE_RETCODE_DONE:
            raise BrokerOrderRejectedError(
                f"order_send failed opening a new {side} position: "
                f"retcode={retcode!r}, last_error={mt5.last_error()!r}"
            )

        return BrokerPosition(
            ticket=result.order,
            symbol=self.symbol_spec.name,
            side=side,
            volume=volume,
            price_open=price,
            price_current=price,
            stop_loss=stop_loss,
            take_profit=take_profit if take_profit is not None else 0.0,
            profit=0.0,
            magic=target_magic,
            opened_at_utc=datetime.now(timezone.utc),
        )
