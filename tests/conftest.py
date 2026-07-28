"""Shared pytest fixtures: a fake MetaTrader5 substitute (no live terminal
exists in CI or in this development environment) and a temp-file-backed
StateManager for storage-dependent tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from storage.state_manager import StateManager


class FakeSymbolInfo:
    def __init__(
        self,
        name: str,
        visible: bool,
        point: float = 0.01,
        digits: int = 2,
        tick_value: float = 1.0,
        tick_size: float = 0.01,
        volume_min: float = 0.01,
        volume_max: float = 100.0,
        volume_step: float = 0.01,
    ) -> None:
        self.name = name
        self.visible = visible
        self.point = point
        self.digits = digits
        self.trade_tick_value = tick_value
        self.trade_tick_size = tick_size
        self.volume_min = volume_min
        self.volume_max = volume_max
        self.volume_step = volume_step


class FakeTick:
    def __init__(self, time_: int = 0, bid: float = 0.0, ask: float = 0.0) -> None:
        self.time = time_
        self.bid = bid
        self.ask = ask


class FakePosition:
    def __init__(
        self,
        ticket: int,
        symbol: str,
        type_: int,
        magic: int,
        volume: float = 0.1,
        price_open: float = 0.0,
        price_current: float = 0.0,
        sl: float = 0.0,
        tp: float = 0.0,
        profit: float = 0.0,
        time_: int = 0,
    ) -> None:
        self.ticket = ticket
        self.symbol = symbol
        self.type = type_
        self.magic = magic
        self.volume = volume
        self.price_open = price_open
        self.price_current = price_current
        self.sl = sl
        self.tp = tp
        self.profit = profit
        self.time = time_


class FakeDeal:
    """Mirrors the real MT5 deal record's field structure (verified
    against a live account), used by `get_closing_deal()`'s tests."""

    def __init__(
        self,
        ticket: int,
        position_id: int,
        entry: int,
        price: float,
        profit: float,
        time_: int,
        symbol: str = "XAUUSD",
    ) -> None:
        self.ticket = ticket
        self.position_id = position_id
        self.entry = entry
        self.price = price
        self.profit = profit
        self.time = time_
        self.symbol = symbol


class FakeOrderResult:
    def __init__(self, retcode: int, order: int = 0) -> None:
        self.retcode = retcode
        self.order = order


class FakeAccountInfo:
    def __init__(
        self,
        balance: float = 10_000.0,
        equity: float = 10_000.0,
        margin: float = 0.0,
        margin_free: float = 10_000.0,
        trade_mode: int = 0,  # ACCOUNT_TRADE_MODE_DEMO
        leverage: int = 100,
        profit: float = 0.0,
        currency: str = "USD",
    ) -> None:
        self.balance = balance
        self.equity = equity
        self.margin = margin
        self.margin_free = margin_free
        self.trade_mode = trade_mode
        self.leverage = leverage
        self.profit = profit
        self.currency = currency


class FakeMT5:
    """A drop-in substitute for the MetaTrader5 module surface used by
    broker/mt5_gateway.py. Assign to `broker.mt5_gateway.mt5` in a test to
    exercise gateway logic with no live terminal."""

    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = "DEAL"
    TRADE_ACTION_SLTP = "SLTP"
    ORDER_TIME_GTC = "GTC"
    ORDER_FILLING_IOC = "IOC"
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_NO_CHANGES = 10025
    TRADE_RETCODE_SERVER_DISABLES_AT = 10026
    TRADE_RETCODE_CLIENT_DISABLES_AT = 10027
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2

    def __init__(self) -> None:
        self.symbols: dict[str, FakeSymbolInfo] = {}
        self.ticks: dict[str, FakeTick] = {}
        self.positions: dict[int, FakePosition] = {}
        self.initialize_results: list[bool] = [True]
        self.initialize_call_count = 0
        self.select_calls: list[tuple[str, bool]] = []
        self.last_error_value: tuple[int, str] = (0, "no error")
        self.last_request: dict[str, Any] | None = None
        self.next_retcode: int = 10009
        self.next_order_ticket: int = 0
        self.account: FakeAccountInfo = FakeAccountInfo()
        self.rates: dict[int, list[dict[str, float]]] = {}
        self.deals: list[FakeDeal] = []
        self.history_deals_get_calls: list[tuple[object, object]] = []

    def history_deals_get(self, date_from: object, date_to: object) -> tuple[FakeDeal, ...]:
        self.history_deals_get_calls.append((date_from, date_to))
        return tuple(self.deals)

    def symbol_info(self, name: str) -> FakeSymbolInfo | None:
        return self.symbols.get(name)

    def symbol_select(self, name: str, enable: bool) -> bool:
        self.select_calls.append((name, enable))
        if name in self.symbols:
            self.symbols[name].visible = True
            return True
        return False

    def symbol_info_tick(self, name: str) -> FakeTick | None:
        return self.ticks.get(name)

    def positions_get(self, ticket: int | None = None) -> tuple[FakePosition, ...]:
        if ticket is not None:
            position = self.positions.get(ticket)
            return (position,) if position is not None else ()
        return tuple(self.positions.values())

    def initialize(
        self, login: int | None = None, password: str | None = None, server: str | None = None
    ) -> bool:
        result = self.initialize_results[self.initialize_call_count]
        self.initialize_call_count += 1
        return result

    def last_error(self) -> tuple[int, str]:
        return self.last_error_value

    def shutdown(self) -> None:
        pass

    def order_send(self, request: dict[str, Any]) -> FakeOrderResult:
        self.last_request = request
        return FakeOrderResult(self.next_retcode, order=self.next_order_ticket)

    def account_info(self) -> FakeAccountInfo | None:
        return self.account

    def copy_rates_from_pos(
        self, symbol: str, timeframe: int, start: int, count: int
    ) -> tuple[dict[str, float], ...] | None:
        # Mirrors real MT5 semantics: the seeded list is oldest-first with
        # the *last* element at position 0 (the currently-forming bar);
        # `start` counts back from it, so start=1 skips the forming bar.
        series = self.rates.get(timeframe, ())
        end = len(series) - start
        if end <= 0:
            return tuple()
        return tuple(series[max(0, end - count) : end])

    def copy_rates_range(
        self, symbol: str, timeframe: int, date_from: Any, date_to: Any
    ) -> tuple[dict[str, float], ...] | None:
        # Mirrors real MT5 semantics: every seeded bar whose `time` (unix
        # seconds) falls in [date_from, date_to] inclusive, oldest-first.
        series = self.rates.get(timeframe, ())
        start_ts = date_from.timestamp()
        end_ts = date_to.timestamp()
        return tuple(bar for bar in series if start_ts <= bar["time"] <= end_ts)


@pytest.fixture
def fake_mt5() -> FakeMT5:
    return FakeMT5()


@pytest.fixture
def state_manager(tmp_path: Path) -> Any:
    manager = StateManager(tmp_path / "test.db")
    yield manager
    manager.close()
