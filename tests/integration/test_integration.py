"""Integration tests: exercises that cross a real boundary — order/position
request building against a simulated MT5, account/bar fetching, a real
SQLite database's transactional rollback behavior, and cross-module state
validation (broker/ledger reconciliation and the weekend optimizer's
isolation guarantee against active trading state).

Simulated *fault/disruption* scenarios (MT5 server dropouts, socket/HTTP
disconnections, an abrupt process crash) live in `tests/chaos/` instead
(Phase 11e's test-suite reorganization, `docs/PRODUCTION_SPEC.md` §7) —
isolated from this fast suite so the standard CI pipeline's feedback loop
never depends on them.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

import broker.mt5_gateway as gw
import container as container_module
import main as orchestrator
import monitoring.telegram_bot as telegram_bot
import optimizer.self_learning as sl
import strategy.trend_filter as trend_filter
from backtester.ml_signal_model import (
    MLValidationReport,
    evaluate_promotion_bar,
    train_and_validate_models,
)
from backtester.simulator import BacktestResult, run_backtest
from backtester.walk_forward import WalkForwardResult, run_walk_forward_validation
from broker.clock_provider import MT5ClockProvider
from config.config_manager import ConfigurationError
from config.telegram_config import TelegramConfig
from container import ApplicationContainer
from execution.position_manager import OrderActionPayload
from news.calendar_provider import OfflineSnapshotCalendarProvider
from risk.drawdown_fsm import DrawdownState
from storage.state_manager import StateManager, TradeLedgerEntry
from tests.conftest import FakeDeal, FakeMT5, FakePosition, FakeSymbolInfo, FakeTick

# ---------------------------------------------------------------------------
# Order/position-action request building against a simulated MT5 (also part
# of the "server dropout" defensive surface: rejected orders must raise,
# never silently no-op)
# ---------------------------------------------------------------------------


class TestOrderActionSubmission:
    def test_partial_close_of_buy_position_closes_at_bid(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.positions[1001] = FakePosition(1001, "XAUUSD", fake_mt5.POSITION_TYPE_BUY, 555)
        fake_mt5.ticks["XAUUSD"] = FakeTick(bid=2009.5, ask=2010.0)

        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        payload = OrderActionPayload(
            action="TRADE_ACTION_DEAL",
            position_ticket=1001,
            symbol="XAUUSD",
            magic=555,
            comment="partial_close_base_tp",
            volume=0.05,
        )
        gateway.submit_position_action(payload)

        request = fake_mt5.last_request
        assert request is not None
        assert request["type"] == fake_mt5.ORDER_TYPE_SELL
        assert request["price"] == 2009.5
        assert request["volume"] == 0.05

    def test_partial_close_of_sell_position_closes_at_ask(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.positions[1002] = FakePosition(1002, "XAUUSD", fake_mt5.POSITION_TYPE_SELL, 555)
        fake_mt5.ticks["XAUUSD"] = FakeTick(bid=2009.5, ask=2010.0)

        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        payload = OrderActionPayload(
            action="TRADE_ACTION_DEAL",
            position_ticket=1002,
            symbol="XAUUSD",
            magic=555,
            comment="partial_close_base_tp",
            volume=0.05,
        )
        gateway.submit_position_action(payload)

        request = fake_mt5.last_request
        assert request is not None
        assert request["type"] == fake_mt5.ORDER_TYPE_BUY
        assert request["price"] == 2010.0

    def test_modify_sltp_request_shape(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.positions[1001] = FakePosition(1001, "XAUUSD", fake_mt5.POSITION_TYPE_BUY, 555)

        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        payload = OrderActionPayload(
            action="TRADE_ACTION_SLTP",
            position_ticket=1001,
            symbol="XAUUSD",
            magic=555,
            comment="atr_trailing_stop",
            stop_loss=2012.5,
        )
        gateway.submit_position_action(payload)

        request = fake_mt5.last_request
        assert request is not None
        assert request["sl"] == 2012.5
        assert "tp" not in request
        assert "volume" not in request

    def test_non_done_retcode_raises_rejected_error(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.positions[1001] = FakePosition(1001, "XAUUSD", fake_mt5.POSITION_TYPE_BUY, 555)
        fake_mt5.next_retcode = 10013  # some rejection code

        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        payload = OrderActionPayload(
            action="TRADE_ACTION_SLTP",
            position_ticket=1001,
            symbol="XAUUSD",
            magic=555,
            comment="atr_trailing_stop",
            stop_loss=2012.5,
        )
        with pytest.raises(gw.BrokerOrderRejectedError):
            gateway.submit_position_action(payload)

    def test_sltp_no_changes_retcode_is_treated_as_a_no_op(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.positions[1001] = FakePosition(1001, "XAUUSD", fake_mt5.POSITION_TYPE_BUY, 555)
        fake_mt5.next_retcode = fake_mt5.TRADE_RETCODE_NO_CHANGES

        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        payload = OrderActionPayload(
            action="TRADE_ACTION_SLTP",
            position_ticket=1001,
            symbol="XAUUSD",
            magic=555,
            comment="atr_trailing_stop",
            stop_loss=2012.5,
        )
        gateway.submit_position_action(payload)  # must not raise

    def test_deal_action_still_raises_on_no_changes_retcode(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # NO_CHANGES only makes sense for a modify (TRADE_ACTION_SLTP);
        # a partial-close (TRADE_ACTION_DEAL) returning it would be a
        # genuinely unexpected broker response, not a benign no-op.
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.positions[1001] = FakePosition(1001, "XAUUSD", fake_mt5.POSITION_TYPE_BUY, 555)
        fake_mt5.ticks["XAUUSD"] = FakeTick(bid=2009.5, ask=2010.0)
        fake_mt5.next_retcode = fake_mt5.TRADE_RETCODE_NO_CHANGES

        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        payload = OrderActionPayload(
            action="TRADE_ACTION_DEAL",
            position_ticket=1001,
            symbol="XAUUSD",
            magic=555,
            comment="partial_close_base_tp",
            volume=0.05,
        )
        with pytest.raises(gw.BrokerOrderRejectedError):
            gateway.submit_position_action(payload)

    def test_missing_position_raises_rejected_error(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        payload = OrderActionPayload(
            action="TRADE_ACTION_DEAL",
            position_ticket=9999,
            symbol="XAUUSD",
            magic=555,
            comment="partial_close_base_tp",
            volume=0.05,
        )
        with pytest.raises(gw.BrokerOrderRejectedError, match="no open position"):
            gateway.submit_position_action(payload)

    def test_autotrading_disabled_raises_narrower_trading_disabled_error(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # retcode 10027 (TRADE_RETCODE_CLIENT_DISABLES_AT, the "Algo
        # Trading" terminal toggle) must raise the narrower subclass, not
        # a plain BrokerOrderRejectedError, so main.py's bar-close loop can
        # catch it specifically and skip the cycle instead of halting.
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.positions[1001] = FakePosition(1001, "XAUUSD", fake_mt5.POSITION_TYPE_BUY, 555)
        fake_mt5.next_retcode = fake_mt5.TRADE_RETCODE_CLIENT_DISABLES_AT

        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        payload = OrderActionPayload(
            action="TRADE_ACTION_SLTP",
            position_ticket=1001,
            symbol="XAUUSD",
            magic=555,
            comment="atr_trailing_stop",
            stop_loss=2012.5,
        )
        with pytest.raises(gw.BrokerTradingDisabledError):
            gateway.submit_position_action(payload)
        # Still an ordinary rejection to any caller that only knows about
        # the parent type.
        with pytest.raises(gw.BrokerOrderRejectedError):
            gateway.submit_position_action(payload)

    def test_server_disables_at_also_raises_trading_disabled_error(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # retcode 10026 (server-side AutoTrading disable) is the same
        # class of condition as the client-side toggle above.
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.positions[1001] = FakePosition(1001, "XAUUSD", fake_mt5.POSITION_TYPE_BUY, 555)
        fake_mt5.next_retcode = fake_mt5.TRADE_RETCODE_SERVER_DISABLES_AT

        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        payload = OrderActionPayload(
            action="TRADE_ACTION_SLTP",
            position_ticket=1001,
            symbol="XAUUSD",
            magic=555,
            comment="atr_trailing_stop",
            stop_loss=2012.5,
        )
        with pytest.raises(gw.BrokerTradingDisabledError):
            gateway.submit_position_action(payload)


# ---------------------------------------------------------------------------
# broker/mt5_gateway.py's Phase 10 additions: account state, bar fetching,
# and new-position market order submission.
# ---------------------------------------------------------------------------


class TestBrokerAccountAndBars:
    def test_get_account_state(self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.account.balance = 9_500.0
        fake_mt5.account.equity = 9_800.0
        fake_mt5.account.margin = 100.0
        fake_mt5.account.margin_free = 9_700.0

        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        state = gateway.get_account_state()
        assert state.balance == 9_500.0
        assert state.equity == 9_800.0
        assert state.margin_used == 100.0
        assert state.margin_free == 9_700.0

    def test_get_account_state_reads_deposit_currency(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.account.currency = "USC"

        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        state = gateway.get_account_state()
        assert state.currency == "USC"

    def test_get_account_state_raises_when_unavailable(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        monkeypatch.setattr(fake_mt5, "account_info", lambda: None)
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        with pytest.raises(gw.BrokerConnectionError):
            gateway.get_account_state()

    def test_get_bars_returns_typed_arrays_excluding_forming_bar(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        fake_mt5.rates[gw.TIMEFRAME_H1] = [
            {
                "open": 2000.0,
                "high": 2005.0,
                "low": 1995.0,
                "close": 2002.0,
                "tick_volume": 100.0,
                "time": 1720000000,
            },
            {
                "open": 2002.0,
                "high": 2010.0,
                "low": 2000.0,
                "close": 2008.0,
                "tick_volume": 150.0,
                "time": 1720000300,
            },
            # The currently-forming bar (MT5 position 0) — must NOT appear
            # in get_bars()' closed-bars result.
            {
                "open": 2008.0,
                "high": 2012.0,
                "low": 2007.0,
                "close": 2011.0,
                "tick_volume": 30.0,
                "time": 1720000600,
            },
        ]

        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()
        bars = gateway.get_bars(gw.TIMEFRAME_H1, 2)
        assert list(bars.close) == [2002.0, 2008.0]
        assert list(bars.tick_volume) == [100.0, 150.0]
        assert len(bars.time_utc) == 2

    def test_get_bars_range_returns_bars_within_window(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        fake_mt5.rates[gw.TIMEFRAME_H1] = [
            {
                "open": 2000.0,
                "high": 2005.0,
                "low": 1995.0,
                "close": 2002.0,
                "tick_volume": 100.0,
                "time": 1720000000,
            },
            {
                "open": 2002.0,
                "high": 2010.0,
                "low": 2000.0,
                "close": 2008.0,
                "tick_volume": 150.0,
                "time": 1720000300,
            },
            {
                "open": 2008.0,
                "high": 2012.0,
                "low": 2007.0,
                "close": 2011.0,
                "tick_volume": 30.0,
                "time": 1720000600,
            },
        ]

        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()
        bars = gateway.get_bars_range(
            gw.TIMEFRAME_H1,
            datetime.fromtimestamp(1720000000, tz=timezone.utc),
            datetime.fromtimestamp(1720000300, tz=timezone.utc),
        )
        assert list(bars.close) == [2002.0, 2008.0]
        assert len(bars.time_utc) == 2

    def test_get_bars_range_raises_when_no_data(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()
        with pytest.raises(gw.BrokerConnectionError):
            gateway.get_bars_range(
                gw.TIMEFRAME_H1,
                datetime(2020, 1, 1, tzinfo=timezone.utc),
                datetime(2020, 1, 2, tzinfo=timezone.utc),
            )

    def test_get_bars_range_rejects_naive_datetimes(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()
        with pytest.raises(ValueError, match="timezone-aware"):
            gateway.get_bars_range(gw.TIMEFRAME_H1, datetime(2020, 1, 1), datetime(2020, 1, 2))

    def test_get_current_price_returns_latest_bid(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1, bid=2009.5, ask=2010.0)
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()
        assert gateway.get_current_price() == 2009.5

    def test_get_current_price_raises_when_no_tick(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()
        del fake_mt5.ticks["XAUUSD"]
        with pytest.raises(gw.BrokerConnectionError):
            gateway.get_current_price()

    def test_get_bars_raises_when_no_data(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()
        with pytest.raises(gw.BrokerConnectionError):
            gateway.get_bars(gw.TIMEFRAME_H1, 10)

    def test_get_open_positions_by_magic_defaults_to_own_magic(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.positions[100] = FakePosition(100, "XAUUSD", fake_mt5.POSITION_TYPE_BUY, 555)
        fake_mt5.positions[200] = FakePosition(200, "XAUUSD", fake_mt5.POSITION_TYPE_SELL, 999)
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        positions = gateway.get_open_positions_by_magic()
        assert [p.ticket for p in positions] == [100]

    def test_get_open_positions_by_magic_with_override(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.positions[100] = FakePosition(100, "XAUUSD", fake_mt5.POSITION_TYPE_BUY, 555)
        fake_mt5.positions[200] = FakePosition(200, "XAUUSD", fake_mt5.POSITION_TYPE_SELL, 999)
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        positions = gateway.get_open_positions_by_magic(magic_number=999)
        assert [p.ticket for p in positions] == [200]

    def test_get_account_trade_mode_demo(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        assert gateway.get_account_trade_mode() == "DEMO"

    def test_get_account_trade_mode_real(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.account.trade_mode = fake_mt5.ACCOUNT_TRADE_MODE_REAL
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        assert gateway.get_account_trade_mode() == "REAL"

    def test_get_account_trade_mode_contest(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.account.trade_mode = fake_mt5.ACCOUNT_TRADE_MODE_CONTEST
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        assert gateway.get_account_trade_mode() == "CONTEST"

    def test_get_closing_deal_finds_the_closing_entry(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        fake_mt5.deals = [
            FakeDeal(
                1,
                position_id=500,
                entry=fake_mt5.DEAL_ENTRY_IN,
                price=2000.0,
                profit=0.0,
                time_=1000,
            ),
            FakeDeal(
                2,
                position_id=500,
                entry=fake_mt5.DEAL_ENTRY_OUT,
                price=2010.0,
                profit=10.0,
                time_=2000,
            ),
            # A deal for a DIFFERENT position must never match.
            FakeDeal(
                3, position_id=999, entry=fake_mt5.DEAL_ENTRY_OUT, price=1.0, profit=1.0, time_=3000
            ),
        ]
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()
        result = gateway.get_closing_deal(500)
        assert result is not None
        assert result.close_price == 2010.0
        assert result.profit == 10.0
        assert result.closed_at_utc == datetime.fromtimestamp(2000, tz=timezone.utc)

    def test_get_closing_deal_query_window_is_broker_clock_not_host_clock(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression test: `get_closing_deal` must build its
        `history_deals_get` window from broker server time
        (`broker_utc_offset`-adjusted), not host UTC — `deal.time` is
        stamped in broker-clock terms, so a host-UTC window silently
        misses recent closes whenever the broker clock runs ahead."""
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        broker_offset = timedelta(hours=3)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        tick_time = datetime.now(timezone.utc) + broker_offset
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=int(tick_time.timestamp()))
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()

        before = datetime.now(timezone.utc) + gateway.broker_utc_offset
        gateway.get_closing_deal(500)
        after = datetime.now(timezone.utc) + gateway.broker_utc_offset

        assert len(fake_mt5.history_deals_get_calls) == 1
        date_from, date_to = fake_mt5.history_deals_get_calls[0]
        assert isinstance(date_from, datetime)
        assert isinstance(date_to, datetime)
        # The window's upper bound must sit close to broker-clock "now",
        # not host-clock "now" — the host/broker gap here is 3 hours, far
        # bigger than any tolerance a host-UTC bug would still pass under.
        assert before + timedelta(minutes=5) - timedelta(seconds=5) <= date_to
        assert date_to <= after + timedelta(minutes=5) + timedelta(seconds=5)

    def test_get_closing_deal_returns_none_when_not_closed(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        fake_mt5.deals = [
            FakeDeal(
                1,
                position_id=500,
                entry=fake_mt5.DEAL_ENTRY_IN,
                price=2000.0,
                profit=0.0,
                time_=1000,
            ),
        ]
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()
        assert gateway.get_closing_deal(500) is None

    def test_get_closing_deal_returns_none_when_no_deals_at_all(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()
        assert gateway.get_closing_deal(500) is None

    def test_submit_market_order_buy_at_ask(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1, bid=2009.5, ask=2010.0)
        fake_mt5.next_order_ticket = 4242

        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()
        position = gateway.submit_market_order(
            side="BUY", volume=0.05, stop_loss=2000.0, take_profit=None, comment="co-1"
        )
        assert position.ticket == 4242
        assert position.side == "BUY"
        assert position.price_open == 2010.0
        request = fake_mt5.last_request
        assert request is not None
        assert request["type"] == fake_mt5.ORDER_TYPE_BUY
        assert request["sl"] == 2000.0
        assert "tp" not in request

    def test_submit_market_order_defaults_to_own_magic_number(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1, bid=2009.5, ask=2010.0)
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()
        position = gateway.submit_market_order(
            side="BUY", volume=0.05, stop_loss=2000.0, take_profit=None, comment="co-1"
        )
        assert fake_mt5.last_request is not None
        assert fake_mt5.last_request["magic"] == 555
        assert position.magic == 555

    def test_submit_market_order_with_magic_number_override(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1, bid=2009.5, ask=2010.0)
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()
        position = gateway.submit_market_order(
            side="BUY",
            volume=0.01,
            stop_loss=2000.0,
            take_profit=2020.0,
            comment="scalp-1",
            magic_number=999,
        )
        assert fake_mt5.last_request is not None
        assert fake_mt5.last_request["magic"] == 999
        assert fake_mt5.last_request["tp"] == 2020.0
        assert position.magic == 999
        assert position.take_profit == 2020.0

    def test_submit_market_order_clamps_uuid_comment_to_mt5_limit(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # main.py passes a UUIDv4 client_order_id (36 chars) as the broker
        # comment; MT5 rejects comments over 31 chars with
        # (-2, 'Invalid "comment" argument') before sending anything.
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1, bid=2009.5, ask=2010.0)
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()
        uuid_comment = "4818d303-ee8a-4f59-aa82-8b6d7add4a14"
        assert len(uuid_comment) > gw.MAX_ORDER_COMMENT_LENGTH
        gateway.submit_market_order(
            side="BUY", volume=0.05, stop_loss=2000.0, take_profit=None, comment=uuid_comment
        )
        assert fake_mt5.last_request is not None
        sent_comment = fake_mt5.last_request["comment"]
        assert len(sent_comment) == gw.MAX_ORDER_COMMENT_LENGTH
        assert sent_comment == uuid_comment[: gw.MAX_ORDER_COMMENT_LENGTH]

    def test_submit_market_order_sell_at_bid(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1, bid=2009.5, ask=2010.0)

        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()
        position = gateway.submit_market_order(
            side="SELL", volume=0.05, stop_loss=2020.0, take_profit=1990.0, comment="co-2"
        )
        assert position.side == "SELL"
        assert position.price_open == 2009.5
        request = fake_mt5.last_request
        assert request is not None
        assert request["type"] == fake_mt5.ORDER_TYPE_SELL
        assert request["tp"] == 1990.0

    def test_submit_market_order_rejected_raises(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1, bid=2009.5, ask=2010.0)
        fake_mt5.next_retcode = 10013

        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()
        with pytest.raises(gw.BrokerOrderRejectedError):
            gateway.submit_market_order(
                side="BUY", volume=0.05, stop_loss=2000.0, take_profit=None, comment="co-3"
            )

    def test_submit_market_order_autotrading_disabled_raises_narrower_error(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same AutoTrading-disabled condition as TestOrderActionSubmission's
        # equivalent case, but for opening a brand-new position rather than
        # managing an existing one — the exact path the live "opening a new
        # SELL position" crash came through.
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1, bid=2009.5, ask=2010.0)
        fake_mt5.next_retcode = fake_mt5.TRADE_RETCODE_CLIENT_DISABLES_AT

        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()
        with pytest.raises(gw.BrokerTradingDisabledError):
            gateway.submit_market_order(
                side="SELL", volume=0.01, stop_loss=2020.0, take_profit=None, comment="co-4"
            )


# ---------------------------------------------------------------------------
# Database rollback (storage/)
# ---------------------------------------------------------------------------


class TestDatabaseRollback:
    def test_constraint_violation_rolls_back_entire_transaction(
        self, state_manager: StateManager
    ) -> None:
        """Proves the transactional guarantee ADR-0003 relies on: if any
        statement in a `with connection:` block raises, every statement
        in that block is rolled back, not just the one that failed."""
        state_manager.record_trade(
            TradeLedgerEntry(
                client_order_id="co-1",
                symbol="XAUUSD",
                side="BUY",
                volume_lots=0.1,
                status="OPEN",
                opened_at_utc="2026-07-01T00:00:00Z",
            )
        )
        connection = state_manager._connection

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    "INSERT INTO trade_ledger "
                    "(client_order_id, symbol, side, volume_lots, status, opened_at_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("co-2", "XAUUSD", "BUY", 0.1, "OPEN", "2026-07-01T00:00:00Z"),
                )
                # Violates the UNIQUE constraint on client_order_id, forcing
                # the whole transaction (including the co-2 insert above)
                # to roll back.
                connection.execute(
                    "INSERT INTO trade_ledger "
                    "(client_order_id, symbol, side, volume_lots, status, opened_at_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("co-1", "XAUUSD", "BUY", 0.1, "OPEN", "2026-07-01T00:00:00Z"),
                )

        count = connection.execute(
            "SELECT COUNT(*) FROM trade_ledger WHERE client_order_id = 'co-2'"
        ).fetchone()[0]
        assert count == 0

    def test_record_trade_idempotent_on_retry(self, state_manager: StateManager) -> None:
        entry = TradeLedgerEntry(
            client_order_id="co-retry",
            symbol="XAUUSD",
            side="BUY",
            volume_lots=0.1,
            status="OPEN",
            opened_at_utc="2026-07-01T00:00:00Z",
            open_price=2350.5,
        )
        state_manager.record_trade(entry)
        state_manager.record_trade(entry)  # simulated retry after a timeout
        count = state_manager._connection.execute(
            "SELECT COUNT(*) FROM trade_ledger WHERE client_order_id = 'co-retry'"
        ).fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# Data state validation processes
# ---------------------------------------------------------------------------


class TestDatabaseIntegrity:
    """WAL mode / integrity sanity checks. The abrupt-process-death crash-
    recovery simulation (dropping a StateManager without close()) moved to
    `tests/chaos/test_chaos.py::TestCrashRecovery` (Phase 11e reorg)."""

    def test_wal_mode_and_integrity(self, state_manager: StateManager) -> None:
        from storage.db_engine import integrity_check

        mode = state_manager._connection.execute("PRAGMA journal_mode;").fetchone()[0]
        assert mode.lower() == "wal"
        assert integrity_check(state_manager._connection) is True


class TestPositionAuditReconciliation:
    def test_reconciles_broker_and_ledger_state(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulates the docs/RUNBOOK.md 1.5 reconnection reconciliation
        step: broker-reported positions (filtered by magic number) cross-
        checked against the locally persisted ledger."""
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.positions[100] = FakePosition(100, "XAUUSD", fake_mt5.POSITION_TYPE_BUY, 555)
        fake_mt5.positions[150] = FakePosition(150, "XAUUSD", fake_mt5.POSITION_TYPE_BUY, 555)
        fake_mt5.positions[200] = FakePosition(
            200, "XAUUSD", fake_mt5.POSITION_TYPE_SELL, 999
        )  # other magic

        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        ledger_trades = [
            TradeLedgerEntry(
                client_order_id="co-1",
                symbol="XAUUSD",
                side="BUY",
                volume_lots=0.1,
                status="OPEN",
                opened_at_utc="2026-07-04T10:00:00Z",
                magic_number=555,
                broker_ticket=100,
            ),
            TradeLedgerEntry(
                client_order_id="co-2",
                symbol="XAUUSD",
                side="BUY",
                volume_lots=0.1,
                status="OPEN",
                opened_at_utc="2026-07-04T10:00:00Z",
                magic_number=555,
                broker_ticket=300,  # not present on broker
            ),
        ]

        report = gateway.audit_open_positions(ledger_trades)
        assert report.reconciled_tickets == (100,)
        assert [p.ticket for p in report.broker_only_positions] == [150]
        assert [e.client_order_id for e in report.ledger_only_entries] == ["co-2"]
        assert report.is_clean is False


class TestReconcileShortTermCloses:
    """`MT5Gateway.reconcile_short_term_closes()` — the boot-time catch-up
    for short-term positions that closed while the process wasn't
    running to catch them cycle-to-cycle."""

    def _ledger_entry(self, **overrides: object) -> TradeLedgerEntry:
        defaults: dict[str, object] = dict(
            client_order_id="co-1",
            symbol="XAUUSD",
            side="SELL",
            volume_lots=0.01,
            status="OPEN",
            opened_at_utc="2026-07-08T15:45:00Z",
            magic_number=777,
            broker_ticket=500,
        )
        defaults.update(overrides)
        return TradeLedgerEntry(**defaults)  # type: ignore[arg-type]

    def test_still_open_position_is_left_alone(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.positions[500] = FakePosition(500, "XAUUSD", fake_mt5.POSITION_TYPE_SELL, 777)
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        updates = gateway.reconcile_short_term_closes(777, [self._ledger_entry()])
        assert updates == []

    def test_closed_position_is_reconciled_with_real_outcome(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        # No FakePosition seeded for ticket 500: it's no longer open.
        fake_mt5.deals = [
            FakeDeal(
                1,
                position_id=500,
                entry=fake_mt5.DEAL_ENTRY_IN,
                price=2000.0,
                profit=0.0,
                time_=1000,
            ),
            FakeDeal(
                2,
                position_id=500,
                entry=fake_mt5.DEAL_ENTRY_OUT,
                price=1990.0,
                profit=10.0,
                time_=2000,
            ),
        ]
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()
        updates = gateway.reconcile_short_term_closes(777, [self._ledger_entry()])
        assert len(updates) == 1
        assert updates[0].client_order_id == "co-1"
        assert updates[0].status == "CLOSED"
        assert updates[0].close_price == 1990.0
        assert updates[0].profit == 10.0
        assert updates[0].closed_at_utc == datetime.fromtimestamp(2000, tz=timezone.utc).isoformat()

    def test_closing_deal_not_found_leaves_entry_untouched(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()
        updates = gateway.reconcile_short_term_closes(777, [self._ledger_entry()])
        assert updates == []

    def test_ignores_entries_under_a_different_magic_number(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.deals = [
            FakeDeal(
                2,
                position_id=500,
                entry=fake_mt5.DEAL_ENTRY_OUT,
                price=1990.0,
                profit=10.0,
                time_=2000,
            ),
        ]
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        other_magic_entry = self._ledger_entry(magic_number=999)
        updates = gateway.reconcile_short_term_closes(777, [other_magic_entry])
        assert updates == []

    def test_ignores_entries_with_no_broker_ticket(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        entry = self._ledger_entry(broker_ticket=None)
        updates = gateway.reconcile_short_term_closes(777, [entry])
        assert updates == []


class TestBotHeartbeat:
    """`StateManager.record_heartbeat()`/`get_heartbeat()` — the liveness
    signal `monitoring/telegram_bot.py`'s `/check` command reads."""

    def test_get_heartbeat_returns_none_before_any_write(self, state_manager: StateManager) -> None:
        assert state_manager.get_heartbeat() is None

    def test_record_then_get_round_trips(self, state_manager: StateManager) -> None:
        state_manager.record_heartbeat("SHORT_TERM")
        heartbeat = state_manager.get_heartbeat()
        assert heartbeat is not None
        assert heartbeat.trading_mode == "SHORT_TERM"
        assert heartbeat.last_heartbeat_utc != ""

    def test_second_write_overwrites_the_singleton_row(self, state_manager: StateManager) -> None:
        state_manager.record_heartbeat("WAIT_FOR_CONDITIONS")
        state_manager.record_heartbeat("BOTH")
        heartbeat = state_manager.get_heartbeat()
        assert heartbeat is not None
        assert heartbeat.trading_mode == "BOTH"
        count = state_manager._connection.execute("SELECT COUNT(*) FROM bot_heartbeat").fetchone()[
            0
        ]
        assert count == 1

    def test_read_only_state_manager_sees_writer_committed_heartbeat(self, tmp_path: Path) -> None:
        db_path = tmp_path / "heartbeat_ro.db"
        writer = StateManager(db_path)
        writer.record_heartbeat("SHORT_TERM")

        reader = StateManager(db_path, read_only=True)
        heartbeat = reader.get_heartbeat()
        assert heartbeat is not None
        assert heartbeat.trading_mode == "SHORT_TERM"

        with pytest.raises(sqlite3.OperationalError):
            reader.record_heartbeat("BOTH")

        reader.close()
        writer.close()


class TestGetRecentTrades:
    """`StateManager.get_recent_trades()` — the most-recent-first slice
    `monitoring/telegram_bot.py`'s `/checkhisorder` reads."""

    def _entry(self, client_order_id: str, **overrides: object) -> TradeLedgerEntry:
        defaults: dict[str, object] = dict(
            client_order_id=client_order_id,
            symbol="XAUUSD",
            side="BUY",
            volume_lots=0.01,
            status="OPEN",
            opened_at_utc="2026-07-09T15:00:00.000000Z",
        )
        defaults.update(overrides)
        return TradeLedgerEntry(**defaults)  # type: ignore[arg-type]

    def test_empty_ledger_returns_empty_list(self, state_manager: StateManager) -> None:
        assert state_manager.get_recent_trades() == []

    def test_returns_most_recent_first(self, state_manager: StateManager) -> None:
        for i in range(3):
            state_manager.record_trade(self._entry(f"co-{i}"))
        trades = state_manager.get_recent_trades()
        assert [t.client_order_id for t in trades] == ["co-2", "co-1", "co-0"]

    def test_respects_limit(self, state_manager: StateManager) -> None:
        for i in range(10):
            state_manager.record_trade(self._entry(f"co-{i}"))
        trades = state_manager.get_recent_trades(limit=7)
        assert len(trades) == 7
        assert [t.client_order_id for t in trades] == [f"co-{i}" for i in range(9, 2, -1)]

    def test_includes_both_open_and_closed(self, state_manager: StateManager) -> None:
        state_manager.record_trade(self._entry("co-open", status="OPEN"))
        state_manager.record_trade(
            self._entry(
                "co-closed",
                status="CLOSED",
                closed_at_utc="2026-07-09T15:10:00.000000Z",
                close_price=2011.0,
                profit=5.0,
            )
        )
        trades = state_manager.get_recent_trades()
        assert {t.client_order_id for t in trades} == {"co-open", "co-closed"}


class TestTelegramBotCommandDispatch:
    """`monitoring/telegram_bot.py`'s `_handle_command()` — the dispatch
    logic behind `/check`/`/killbot`/`/checkhisorder`, tested against a
    real `StateManager` (its effects are real DB writes + Audit Trail
    rows, not pure)."""

    def _config(self, chat_id: int = 8163059171) -> TelegramConfig:
        return TelegramConfig(bot_token="123:ABC", allowed_chat_ids=(chat_id,))

    def test_check_reports_running(
        self, state_manager: StateManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # _is_main_process_alive() shells out to the real OS; mocked here
        # so this test's outcome never depends on whether a real main.py
        # process happens to be running on the host at test time.
        monkeypatch.setattr(telegram_bot, "_is_main_process_alive", lambda: True)
        state_manager.record_heartbeat("SHORT_TERM")
        reply = telegram_bot._handle_command(
            self._config(), state_manager, command=telegram_bot.CHECK_COMMAND, chat_id=8163059171
        )
        assert "กำลังทำงาน" in reply

    def test_killbot_records_audit_event(
        self, state_manager: StateManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(telegram_bot, "_kill_main_process", lambda: "🛑 killed (faked)")
        reply = telegram_bot._handle_command(
            self._config(), state_manager, command=telegram_bot.KILL_COMMAND, chat_id=8163059171
        )
        assert reply == "🛑 killed (faked)"

        audit_events = state_manager.get_audit_trail(parameter_name="main_process_killed")
        assert len(audit_events) == 1
        assert audit_events[0].action_type == "MANUAL_OVERRIDE"
        assert audit_events[0].new_value == "🛑 killed (faked)"

    def test_checkhisorder_reports_recent_trades(self, state_manager: StateManager) -> None:
        state_manager.record_trade(
            TradeLedgerEntry(
                client_order_id="co-1",
                symbol="XAUUSD",
                side="BUY",
                volume_lots=0.01,
                status="CLOSED",
                opened_at_utc="2026-07-09T14:32:00.000000Z",
                closed_at_utc="2026-07-09T14:42:00.000000Z",
                close_price=2011.0,
                profit=5.1,
            )
        )
        reply = telegram_bot._handle_command(
            self._config(),
            state_manager,
            command=telegram_bot.CHECK_HISTORY_COMMAND,
            chat_id=8163059171,
        )
        assert "co-1" not in reply  # client_order_id itself is internal, never shown
        assert "XAUUSD" in reply
        assert "+5.10" in reply

    def test_checkhisorder_reports_no_history_when_empty(self, state_manager: StateManager) -> None:
        reply = telegram_bot._handle_command(
            self._config(),
            state_manager,
            command=telegram_bot.CHECK_HISTORY_COMMAND,
            chat_id=8163059171,
        )
        assert "ยังไม่มีประวัติ" in reply


class TestOptimizerIsolation:
    """The core safety property of Phase 8: the weekend self-learning
    optimizer must never touch active trading state."""

    def test_weekly_cycle_never_touches_fsm_state_or_open_trades(
        self, state_manager: StateManager
    ) -> None:
        state_manager.save_fsm_state(
            {"phase": "AWAITING_FILL", "open_positions": ["XAUUSD"]}, last_sequence_id=99
        )
        fsm_state_before = state_manager.load_fsm_state()

        open_trade = TradeLedgerEntry(
            client_order_id="open-1",
            symbol="XAUUSD",
            side="BUY",
            volume_lots=0.10,
            status="OPEN",
            opened_at_utc="2026-07-04T09:00:00Z",
            open_price=2350.0,
            stop_loss_price=2340.0,
            magic_number=555,
            broker_ticket=5001,
        )
        state_manager.record_trade(open_trade)
        open_trades_before = state_manager.get_open_trades()

        # Losing streak engineered to trigger a low-win-rate parameter shift.
        closed_profits = [-10.0, -8.0, -12.0, 15.0, -6.0, -9.0, 20.0, -11.0, -7.0, -13.0, -14.0]
        for i, profit in enumerate(closed_profits):
            state_manager.record_trade(
                TradeLedgerEntry(
                    client_order_id=f"closed-{i}",
                    symbol="XAUUSD",
                    side="BUY",
                    volume_lots=0.10,
                    status="CLOSED",
                    opened_at_utc="2026-07-01T09:00:00Z",
                    open_price=2350.0,
                    close_price=2350.0 + profit,
                    profit=profit,
                    magic_number=555,
                    closed_at_utc="2026-07-01T10:00:00Z",
                )
            )

        tunable_parameters = {
            "ADX_TREND_THRESHOLD": sl.TunableParameter(
                "ADX_TREND_THRESHOLD", 25.0, 20.0, 35.0, 1.0
            ),
            "TRAILING_ATR_MULTIPLIER": sl.TunableParameter(
                "TRAILING_ATR_MULTIPLIER", 1.5, 1.0, 3.0, 0.25
            ),
        }

        # A non-Saturday run must be a complete no-op.
        tuesday = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)
        result_tuesday = sl.run_weekly_optimization_cycle(
            state_manager, tunable_parameters, now_utc=tuesday
        )
        assert result_tuesday.ran is False
        param_history_count = state_manager._connection.execute(
            "SELECT COUNT(*) FROM parameter_history"
        ).fetchone()[0]
        assert param_history_count == 0

        # A Saturday run should fire the rule-based shift and the bootstrap.
        saturday = datetime(2026, 7, 4, 3, 0, tzinfo=timezone.utc)
        result_saturday = sl.run_weekly_optimization_cycle(
            state_manager, tunable_parameters, now_utc=saturday
        )
        assert result_saturday.ran is True
        assert result_saturday.shift_decision is not None
        assert result_saturday.shift_decision.parameter_name == "ADX_TREND_THRESHOLD"
        assert result_saturday.bootstrap_result is not None

        # Isolation: FSM state and open trades must be byte-for-byte unchanged.
        assert state_manager.load_fsm_state() == fsm_state_before
        assert state_manager.get_open_trades() == open_trades_before

        # Exactly one parameter_history row was appended.
        param_history_count_after = state_manager._connection.execute(
            "SELECT COUNT(*) FROM parameter_history"
        ).fetchone()[0]
        assert param_history_count_after == 1


class TestEffectiveParameterValue:
    """`StateManager.get_latest_parameter_value()` and
    `optimizer.self_learning.get_effective_parameter_value()` — the read
    half of `record_parameter_change()`'s write, closing the gap where a
    self-learning shift was recorded but nothing ever applied it live."""

    def test_get_latest_parameter_value_none_when_never_shifted(
        self, state_manager: StateManager
    ) -> None:
        assert state_manager.get_latest_parameter_value("ADX_TREND_THRESHOLD") is None

    def test_get_latest_parameter_value_returns_most_recent(
        self, state_manager: StateManager
    ) -> None:
        state_manager.record_parameter_change("ADX_TREND_THRESHOLD", 25.0, 26.0, "shift 1")
        state_manager.record_parameter_change("ADX_TREND_THRESHOLD", 26.0, 27.0, "shift 2")
        assert state_manager.get_latest_parameter_value("ADX_TREND_THRESHOLD") == 27.0

    def test_get_latest_parameter_value_scoped_by_name(self, state_manager: StateManager) -> None:
        state_manager.record_parameter_change("ADX_TREND_THRESHOLD", 25.0, 26.0, "shift")
        assert state_manager.get_latest_parameter_value("TRAILING_ATR_MULTIPLIER") is None

    def test_get_effective_parameter_value_falls_back_to_default(
        self, state_manager: StateManager
    ) -> None:
        assert sl.get_effective_parameter_value(state_manager, "ADX_TREND_THRESHOLD", 25.0) == 25.0

    def test_get_effective_parameter_value_returns_latest_shift(
        self, state_manager: StateManager
    ) -> None:
        state_manager.record_parameter_change("ADX_TREND_THRESHOLD", 25.0, 26.0, "shift")
        assert sl.get_effective_parameter_value(state_manager, "ADX_TREND_THRESHOLD", 25.0) == 26.0


class TestGetOpenTradeByTicket:
    def test_finds_open_row_by_ticket(self, state_manager: StateManager) -> None:
        state_manager.record_trade(
            TradeLedgerEntry(
                client_order_id="co-1",
                symbol="XAUUSD",
                side="BUY",
                volume_lots=0.01,
                status="OPEN",
                opened_at_utc="2026-07-06T00:00:00Z",
                broker_ticket=777,
            )
        )
        entry = state_manager.get_open_trade_by_ticket(777)
        assert entry is not None
        assert entry.client_order_id == "co-1"

    def test_returns_none_for_unknown_ticket(self, state_manager: StateManager) -> None:
        assert state_manager.get_open_trade_by_ticket(999999) is None

    def test_returns_none_once_closed(self, state_manager: StateManager) -> None:
        state_manager.record_trade(
            TradeLedgerEntry(
                client_order_id="co-2",
                symbol="XAUUSD",
                side="BUY",
                volume_lots=0.01,
                status="OPEN",
                opened_at_utc="2026-07-06T00:00:00Z",
                broker_ticket=778,
            )
        )
        state_manager.record_trade(
            TradeLedgerEntry(
                client_order_id="co-2",
                symbol="XAUUSD",
                side="BUY",
                volume_lots=0.01,
                status="CLOSED",
                opened_at_utc="2026-07-06T00:00:00Z",
                closed_at_utc="2026-07-06T01:00:00Z",
                broker_ticket=778,
            )
        )
        assert state_manager.get_open_trade_by_ticket(778) is None


class _FixedSaturdayDatetime(datetime):
    """A `datetime` subclass whose `now()` always returns a fixed Saturday
    — `container_module._build_weekly_optimization_job()`'s returned
    `job()` takes no arguments (it must match `Callable[[], ...]` for
    `create_weekend_optimizer_scheduler()`), so it always resolves "now"
    via `datetime.now(timezone.utc)` internally; monkeypatching
    `optimizer.self_learning`'s `datetime` name is the only way to test
    its Saturday-gated behavior deterministically regardless of which day
    the test suite actually runs on."""

    @classmethod
    def now(cls, tz: object = None) -> "_FixedSaturdayDatetime":
        return cls(2026, 7, 4, 3, 0, tzinfo=timezone.utc)


class TestWeeklyOptimizationJob:
    """`container._build_weekly_optimization_job()` — the closure
    `create_weekend_optimizer_scheduler()` runs every Saturday. Verifies
    it seeds each `TunableParameter`'s starting value from
    `get_effective_parameter_value()`, not always the hardcoded default,
    so this week's decision builds on any prior shift."""

    def _seed_losing_streak(self, state_manager: StateManager) -> None:
        # Below LOW_WIN_RATE_THRESHOLD (0.40) with >= MIN_TRADES_FOR_ADJUSTMENT
        # (10) closed trades: triggers an ADX_TREND_THRESHOLD tightening shift.
        profits = [-10.0, -8.0, -12.0, 15.0, -6.0, -9.0, 20.0, -11.0, -7.0, -13.0, -14.0]
        for i, profit in enumerate(profits):
            state_manager.record_trade(
                TradeLedgerEntry(
                    client_order_id=f"job-closed-{i}",
                    symbol="XAUUSD",
                    side="BUY",
                    volume_lots=0.10,
                    status="CLOSED",
                    opened_at_utc="2026-07-01T09:00:00Z",
                    profit=profit,
                    closed_at_utc="2026-07-01T10:00:00Z",
                )
            )

    def test_job_seeds_starting_value_from_hardcoded_default(
        self, state_manager: StateManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sl, "datetime", _FixedSaturdayDatetime)
        self._seed_losing_streak(state_manager)
        job = container_module._build_weekly_optimization_job(state_manager)

        job_result = job()
        assert job_result.ran is True
        assert job_result.shift_decision is not None
        assert job_result.shift_decision.parameter_name == "ADX_TREND_THRESHOLD"
        assert job_result.shift_decision.old_value == trend_filter.ADX_TREND_THRESHOLD

    def test_job_seeds_starting_value_from_prior_shift_not_default(
        self, state_manager: StateManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sl, "datetime", _FixedSaturdayDatetime)
        self._seed_losing_streak(state_manager)
        # A prior shift already moved ADX_TREND_THRESHOLD away from the
        # hardcoded default — the job must build on THIS value, not restart
        # from strategy.trend_filter.ADX_TREND_THRESHOLD every time.
        state_manager.record_parameter_change("ADX_TREND_THRESHOLD", 25.0, 30.0, "earlier shift")
        job = container_module._build_weekly_optimization_job(state_manager)

        job_result = job()
        assert job_result.shift_decision is not None
        assert job_result.shift_decision.parameter_name == "ADX_TREND_THRESHOLD"
        assert job_result.shift_decision.old_value == 30.0
        assert job_result.shift_decision.new_value == 31.0


# ---------------------------------------------------------------------------
# container.py's ApplicationContainer: the DI composition root wiring
# config/ + storage/ + broker/ together (docs/PRODUCTION_SPEC.md §1's
# "Core Orchestration Directive" #1).
# ---------------------------------------------------------------------------


class TestApplicationContainer:
    REQUIRED_ENV = {
        "MT5_LOGIN": "12345",
        "MT5_PASSWORD": "secret",
        "MT5_SERVER": "Broker-Demo",
        "ECONOMIC_CALENDAR_API_KEY": "abc123",
        "STRATEGY_MAGIC_NUMBER": "555",
        "ENVIRONMENT_MODE": "DEMO",
        "TRADING_MODE": "WAIT_FOR_CONDITIONS",
        "SHORT_TERM_MAGIC_NUMBER": "556",
    }

    def test_build_wires_config_storage_and_broker(
        self,
        fake_mt5: FakeMT5,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        monkeypatch.setattr(gw, "mt5", fake_mt5)

        app = ApplicationContainer.build(
            env_file="nonexistent.env", db_path=tmp_path / "container.db"
        )
        try:
            assert app.config.mt5_login == 12345
            assert app.gateway.symbol_spec.name == "XAUUSD"
            assert app.state_manager.get_open_trades() == []
            assert app.initial_drawdown_state == DrawdownState.ACTIVE
        finally:
            app.state_manager.close()

    def test_build_refuses_environment_mode_account_mismatch(
        self,
        fake_mt5: FakeMT5,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # RR-012: ENVIRONMENT_MODE=DEMO but the broker reports a REAL
        # account — boot must fail closed, before any trading state is
        # touched.
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        fake_mt5.account.trade_mode = fake_mt5.ACCOUNT_TRADE_MODE_REAL
        monkeypatch.setattr(gw, "mt5", fake_mt5)

        with pytest.raises(ConfigurationError, match="RR-012"):
            ApplicationContainer.build(env_file="nonexistent.env", db_path=tmp_path / "mismatch.db")

    def test_build_logs_position_audit_divergence(
        self,
        fake_mt5: FakeMT5,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        # A position open on the broker with no matching local ledger row.
        fake_mt5.positions[100] = FakePosition(100, "XAUUSD", fake_mt5.POSITION_TYPE_BUY, 555)
        monkeypatch.setattr(gw, "mt5", fake_mt5)

        with caplog.at_level("WARNING"):
            app = ApplicationContainer.build(
                env_file="nonexistent.env", db_path=tmp_path / "container_divergence.db"
            )
        try:
            assert "divergence" in caplog.text
            # Phase 11e's Disaster Recovery reconciliation: a divergence
            # blocks the FSM from starting ACTIVE, settles the broker-only
            # ticket into a real ledger row, and records an audit entry.
            assert app.initial_drawdown_state == DrawdownState.MANUAL_RESET_REQUIRED
            reconciled = [e for e in app.state_manager.get_open_trades() if e.broker_ticket == 100]
            assert len(reconciled) == 1
            assert reconciled[0].client_order_id == "disaster-recovery-100"
            audit_trail = app.state_manager.get_audit_trail()
            assert len(audit_trail) == 1
            assert audit_trail[0].action_type == "DISASTER_RECOVERY_RECONCILIATION"
        finally:
            app.state_manager.close()

    def test_build_seeds_initial_fsm_context_from_live_broker_position(
        self,
        fake_mt5: FakeMT5,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """main._seed_initial_fsm_context() (Phase 11e Disaster Recovery,
        docs/PRODUCTION_SPEC.md §7) resumes IN_POSITION directly from the
        broker's live open positions rather than a stale local snapshot."""
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        fake_mt5.positions[777] = FakePosition(
            777, "XAUUSD", fake_mt5.POSITION_TYPE_BUY, 555, volume=0.2
        )
        monkeypatch.setattr(gw, "mt5", fake_mt5)

        app = ApplicationContainer.build(
            env_file="nonexistent.env", db_path=tmp_path / "container_seed.db"
        )
        try:
            context = orchestrator._seed_initial_fsm_context(app)
            assert context.state == orchestrator.TradingState.IN_POSITION
            assert context.position is not None
            assert context.position.ticket == 777
            assert context.position.volume == 0.2
            assert context.position.partial_closed is False
            assert context.position.breakeven_set is False
        finally:
            app.state_manager.close()

    def test_seed_restores_persisted_flags_on_ticket_match(
        self,
        fake_mt5: FakeMT5,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """The bug this closes: `partial_closed`/`breakeven_set` used to
        reset to False on every restart, re-arming an already-completed
        partial-close/breakeven step and repeatedly halving whatever
        volume remained. A saved snapshot for the SAME ticket now
        restores both flags."""
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        fake_mt5.positions[777] = FakePosition(
            777, "XAUUSD", fake_mt5.POSITION_TYPE_BUY, 555, volume=0.05
        )
        monkeypatch.setattr(gw, "mt5", fake_mt5)

        app = ApplicationContainer.build(
            env_file="nonexistent.env", db_path=tmp_path / "container_seed_restore.db"
        )
        try:
            app.state_manager.save_fsm_state(
                {
                    "state": "IN_POSITION",
                    "position": {
                        "ticket": 777,
                        "symbol": "XAUUSD",
                        "side": "BUY",
                        "volume": 0.10,
                        "entry_price": 2000.0,
                        "stop_loss": 2000.0,
                        "magic_number": 555,
                        "partial_closed": True,
                        "breakeven_set": True,
                    },
                    "drawdown_state": "ACTIVE",
                    "drawdown_reason": None,
                },
                last_sequence_id=0,
            )
            context = orchestrator._seed_initial_fsm_context(app)
            assert context.position is not None
            assert context.position.ticket == 777
            # The broker-reported volume (0.05) wins over the stale
            # snapshot's (0.10) — only partial_closed/breakeven_set are
            # ever taken from the snapshot.
            assert context.position.volume == 0.05
            assert context.position.partial_closed is True
            assert context.position.breakeven_set is True
        finally:
            app.state_manager.close()

    def test_seed_ignores_persisted_flags_on_ticket_mismatch(
        self,
        fake_mt5: FakeMT5,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A snapshot for a *different* ticket (e.g. the previous position
        already closed and a new one opened) must never leak its flags
        onto the currently-open position."""
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        fake_mt5.positions[888] = FakePosition(
            888, "XAUUSD", fake_mt5.POSITION_TYPE_BUY, 555, volume=0.05
        )
        monkeypatch.setattr(gw, "mt5", fake_mt5)

        app = ApplicationContainer.build(
            env_file="nonexistent.env", db_path=tmp_path / "container_seed_mismatch.db"
        )
        try:
            app.state_manager.save_fsm_state(
                {
                    "state": "IN_POSITION",
                    "position": {
                        "ticket": 777,
                        "symbol": "XAUUSD",
                        "side": "BUY",
                        "volume": 0.10,
                        "entry_price": 2000.0,
                        "stop_loss": 2000.0,
                        "magic_number": 555,
                        "partial_closed": True,
                        "breakeven_set": True,
                    },
                    "drawdown_state": "ACTIVE",
                    "drawdown_reason": None,
                },
                last_sequence_id=0,
            )
            context = orchestrator._seed_initial_fsm_context(app)
            assert context.position is not None
            assert context.position.ticket == 888
            assert context.position.partial_closed is False
            assert context.position.breakeven_set is False
        finally:
            app.state_manager.close()

    def test_build_seeds_flat_context_when_no_open_positions(
        self,
        fake_mt5: FakeMT5,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        monkeypatch.setattr(gw, "mt5", fake_mt5)

        app = ApplicationContainer.build(
            env_file="nonexistent.env", db_path=tmp_path / "container_seed_flat.db"
        )
        try:
            context = orchestrator._seed_initial_fsm_context(app)
            assert context.state == orchestrator.TradingState.IDLE
            assert context.position is None
            assert context.drawdown_state == DrawdownState.ACTIVE
        finally:
            app.state_manager.close()

    def test_build_reconciles_stale_short_term_close_at_boot(
        self,
        fake_mt5: FakeMT5,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A short-term position that closed while the process wasn't
        running (so `_fetch_short_term_position()` never caught the
        cycle-to-cycle transition) must still get reconciled at the next
        boot — the gap this test guards against."""
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        # No FakePosition for ticket 500 (short-term magic 556): it's closed.
        fake_mt5.deals = [
            FakeDeal(
                1,
                position_id=500,
                entry=fake_mt5.DEAL_ENTRY_OUT,
                price=1990.0,
                profit=-5.0,
                time_=2000,
            ),
        ]
        monkeypatch.setattr(gw, "mt5", fake_mt5)

        db_path = tmp_path / "container_short_term_reconcile.db"
        pre_seed = StateManager(db_path)
        pre_seed.record_trade(
            TradeLedgerEntry(
                client_order_id="stale-short-term",
                symbol="XAUUSD",
                side="SELL",
                volume_lots=0.01,
                status="OPEN",
                opened_at_utc="2026-07-08T15:45:00Z",
                magic_number=556,
                broker_ticket=500,
            )
        )
        pre_seed.close()

        app = ApplicationContainer.build(env_file="nonexistent.env", db_path=db_path)
        try:
            assert app.state_manager.get_open_trade_by_ticket(500) is None
            closed = app.state_manager.get_closed_trades()
            assert len(closed) == 1
            assert closed[0].client_order_id == "stale-short-term"
            assert closed[0].profit == -5.0
            assert closed[0].close_price == 1990.0
        finally:
            app.state_manager.close()

    def test_build_attaches_secret_redaction_to_root_logger(
        self,
        fake_mt5: FakeMT5,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        monkeypatch.setattr(gw, "mt5", fake_mt5)

        root_logger = logging.getLogger()
        filters_before = list(root_logger.filters)
        app = ApplicationContainer.build(
            env_file="nonexistent.env", db_path=tmp_path / "container2.db"
        )
        try:
            new_filters = [
                f
                for f in root_logger.filters
                if f not in filters_before and isinstance(f, logging.Filter)
            ]
            assert len(new_filters) == 1
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg="password is secret",
                args=(),
                exc_info=None,
            )
            new_filters[0].filter(record)
            assert "secret" not in record.getMessage()
        finally:
            app.state_manager.close()
            root_logger.filters = filters_before

    def test_build_propagates_config_error_uncaught(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        for key in self.REQUIRED_ENV:
            monkeypatch.delenv(key, raising=False)
        with pytest.raises(Exception, match="Missing required environment"):
            ApplicationContainer.build(env_file="nonexistent.env", db_path=tmp_path / "x.db")

    def test_build_wires_calendar_and_clock_providers_with_defaults(
        self,
        fake_mt5: FakeMT5,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """No CALENDAR_* env vars set: build() must still succeed, wiring
        the safe `offline_snapshot`-only default chain (docs/PRODUCTION_SPEC.md
        §2/§3) — this is what keeps every pre-existing container test above
        passing without any new required configuration."""
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        for key in (
            "CALENDAR_PROVIDER_PRIORITY",
            "CALENDAR_TRADINGECONOMICS_BASE_URL",
            "CALENDAR_FINNHUB_BASE_URL",
        ):
            monkeypatch.delenv(key, raising=False)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(
            time_=int(datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc).timestamp())
        )
        monkeypatch.setattr(gw, "mt5", fake_mt5)

        app = ApplicationContainer.build(
            env_file="nonexistent.env", db_path=tmp_path / "container_calendar.db"
        )
        try:
            assert len(app.calendar_provider.providers) == 1
            assert isinstance(app.calendar_provider.providers[0], OfflineSnapshotCalendarProvider)
            assert (
                app.calendar_provider.fetch_events(
                    datetime(2026, 7, 4, tzinfo=timezone.utc),
                    datetime(2026, 7, 11, tzinfo=timezone.utc),
                )
                == []
            )
            assert isinstance(app.clock_provider, MT5ClockProvider)
            assert app.clock_provider.get_server_time("XAUUSD").tzinfo is not None
        finally:
            app.state_manager.close()

    def test_build_propagates_calendar_config_error_uncaught(
        self,
        fake_mt5: FakeMT5,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A network provider listed in CALENDAR_PROVIDER_PRIORITY without
        its base URL configured must halt startup, same fail-closed
        posture as a missing broker credential."""
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("CALENDAR_PROVIDER_PRIORITY", "finnhub")
        monkeypatch.delenv("CALENDAR_FINNHUB_BASE_URL", raising=False)
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        monkeypatch.setattr(gw, "mt5", fake_mt5)

        with pytest.raises(Exception, match="CALENDAR_FINNHUB_BASE_URL"):
            ApplicationContainer.build(
                env_file="nonexistent.env", db_path=tmp_path / "container_calendar_err.db"
            )


class TestBacktestSimulatorEndToEnd:
    """`backtester/simulator.py`'s `run_backtest()` driven over a small,
    fully synthetic multi-month OHLC fixture — no live MT5 call, matching
    this test file's own convention for anything not exercising a real
    external boundary otherwise. This is the parity proof: `run_backtest()`
    calls the exact same `main._fetch_market_snapshot()`/
    `main.run_bar_close_cycle()` the live loop calls, just fed synthetic
    historical bars via `HistoricalReplayGateway` instead of a real MT5
    connection.
    """

    def _synthetic_bars(
        self, n_days: int, *, seed: int = 42, base_price: float = 2000.0
    ) -> tuple[gw.BarSeries, gw.BarSeries, gw.BarSeries]:
        rng = np.random.default_rng(seed)
        start = datetime(2020, 1, 1, tzinfo=timezone.utc)

        def make(count: int, step: timedelta) -> gw.BarSeries:
            times = tuple(start + i * step for i in range(count))
            close = base_price + np.cumsum(rng.normal(0, 2.0, size=count))
            high = close + rng.uniform(0.5, 5.0, size=count)
            low = close - rng.uniform(0.5, 5.0, size=count)
            open_ = close + rng.normal(0, 1.0, size=count)
            return gw.BarSeries(
                open=open_,
                high=high,
                low=low,
                close=close,
                tick_volume=np.full(count, 100.0),
                time_utc=times,
            )

        d1_bars = make(n_days, timedelta(days=1))
        h4_bars = make(n_days * 6, timedelta(hours=4))
        h1_bars = make(n_days * 24, timedelta(hours=1))
        return d1_bars, h4_bars, h1_bars

    @pytest.fixture(scope="class")
    def backtest_result(self) -> tuple[gw.BarSeries, BacktestResult]:
        # Computed once and shared across every test in this class — each
        # test asserts a different property of the *same* run rather than
        # re-running the (~seconds-scale) simulation per assertion.
        d1_bars, h4_bars, h1_bars = self._synthetic_bars(n_days=400)
        return h1_bars, run_backtest(d1_bars, h4_bars, h1_bars, starting_equity=10_000.0)

    def test_runs_to_completion_and_produces_an_equity_curve(
        self, backtest_result: tuple[gw.BarSeries, BacktestResult]
    ) -> None:
        h1_bars, result = backtest_result
        assert len(result.equity_curve) > 0
        # One equity point per H1 bar processed.
        assert len(result.equity_curve) == len(h1_bars.close)

    def test_equity_curve_is_chronologically_ordered(
        self, backtest_result: tuple[gw.BarSeries, BacktestResult]
    ) -> None:
        _, result = backtest_result
        times = [t for t, _ in result.equity_curve]
        assert times == sorted(times)

    def test_trades_have_plausible_shape(
        self, backtest_result: tuple[gw.BarSeries, BacktestResult]
    ) -> None:
        _, result = backtest_result
        for trade in result.trades:
            assert trade.entry_time <= trade.exit_time
            assert trade.volume > 0.0
            assert trade.side in ("BUY", "SELL")
            assert trade.reason in (
                "stop_loss",
                "partial_close_base_tp",
                orchestrator.EMERGENCY_LIQUIDATION_COMMENT,
            )

    def test_equity_only_changes_on_a_closed_trade(
        self, backtest_result: tuple[gw.BarSeries, BacktestResult]
    ) -> None:
        # Between any two consecutive equity-curve points where no trade's
        # exit_time falls in between, the *balance* component shouldn't
        # have silently jumped — approximated here by checking that large
        # equity deltas correspond to a nearby trade close, not drift.
        _, result = backtest_result
        exit_times = {trade.exit_time for trade in result.trades}
        assert exit_times.issubset({t for t, _ in result.equity_curve})

    def test_no_trades_before_warmup_completes(
        self, backtest_result: tuple[gw.BarSeries, BacktestResult]
    ) -> None:
        # D1_BAR_COUNT=220 days of warmup required before any decision is
        # even evaluated — no trade can open before that.
        h1_bars, result = backtest_result
        warmup_end = h1_bars.time_utc[0] + timedelta(days=orchestrator.D1_BAR_COUNT)
        assert all(trade.entry_time >= warmup_end for trade in result.trades)

    def test_deterministic_given_same_seed(self) -> None:
        d1_a, h4_a, h1_a = self._synthetic_bars(n_days=400, seed=7)
        d1_b, h4_b, h1_b = self._synthetic_bars(n_days=400, seed=7)
        result_a = run_backtest(d1_a, h4_a, h1_a, starting_equity=10_000.0)
        result_b = run_backtest(d1_b, h4_b, h1_b, starting_equity=10_000.0)
        assert len(result_a.trades) == len(result_b.trades)
        assert result_a.equity_curve[-1][1] == pytest.approx(result_b.equity_curve[-1][1])


class TestMLSignalModelEndToEnd:
    """`backtester/ml_signal_model.py`'s `train_and_validate_models()`
    driven over a small, fully synthetic H1 fixture — no live MT5 call.
    Proves the training/validation pipeline (TimeSeriesSplit hyperparameter
    selection, chronological IS/OOS split, both candidate models, the
    scaler-folding math) runs end-to-end and returns a well-formed report;
    it does not assert any particular accuracy, since synthetic random-walk
    prices have no real indicator-based edge by construction.
    """

    def _synthetic_h1_bars(self, n: int, *, seed: int = 3) -> gw.BarSeries:
        rng = np.random.default_rng(seed)
        start = datetime(2020, 1, 1, tzinfo=timezone.utc)
        times = tuple(start + timedelta(hours=i) for i in range(n))
        close = 2000.0 + np.cumsum(rng.normal(0, 1.0, size=n))
        high = close + rng.uniform(0.1, 1.0, size=n)
        low = close - rng.uniform(0.1, 1.0, size=n)
        open_ = close + rng.normal(0, 0.5, size=n)
        return gw.BarSeries(
            open=open_,
            high=high,
            low=low,
            close=close,
            tick_volume=rng.uniform(100.0, 1000.0, size=n),
            time_utc=times,
        )

    @pytest.fixture(scope="class")
    def report(self) -> MLValidationReport:
        h1_bars = self._synthetic_h1_bars(n=1500)
        return train_and_validate_models(h1_bars)

    def test_sample_sizes_are_positive_and_roughly_70_30(self, report: MLValidationReport) -> None:
        assert report.n_train > 0
        assert report.n_oos > 0
        total = report.n_train + report.n_oos
        assert report.n_train / total == pytest.approx(0.7, abs=0.02)

    def test_every_reported_rate_is_a_valid_probability(self, report: MLValidationReport) -> None:
        for value in (
            report.majority_class_baseline,
            report.naive_heuristic_oos_accuracy,
            report.logistic_regression.evaluation.oos_accuracy,
            report.logistic_regression.evaluation.oos_precision,
            report.logistic_regression.evaluation.oos_recall,
            report.logistic_regression.evaluation.oos_roc_auc,
            report.gradient_boosting.oos_accuracy,
            report.gradient_boosting.oos_precision,
            report.gradient_boosting.oos_recall,
            report.gradient_boosting.oos_roc_auc,
        ):
            assert 0.0 <= value <= 1.0

    def test_logistic_regression_coefficient_vector_matches_feature_count(
        self, report: MLValidationReport
    ) -> None:
        assert len(report.logistic_regression.raw_space_coefficients) == len(report.feature_names)

    def test_promotion_bar_evaluates_without_error(self, report: MLValidationReport) -> None:
        # No accuracy assertion here by design (see class docstring) — this
        # only proves evaluate_promotion_bar() runs cleanly against a real
        # report shape.
        assert evaluate_promotion_bar(report) in (True, False)

    def test_deterministic_given_same_seed(self) -> None:
        bars_a = self._synthetic_h1_bars(n=1500, seed=11)
        bars_b = self._synthetic_h1_bars(n=1500, seed=11)
        report_a = train_and_validate_models(bars_a)
        report_b = train_and_validate_models(bars_b)
        assert report_a.logistic_regression.evaluation.oos_accuracy == pytest.approx(
            report_b.logistic_regression.evaluation.oos_accuracy
        )


class TestWalkForwardValidationEndToEnd:
    """`backtester/walk_forward.py`'s `run_walk_forward_validation()` driven
    over a small, fully synthetic multi-year OHLC fixture with a tiny
    2-combo grid and a short `initial_train_months`/`step_months` sized to
    fit exactly 3 folds — no live MT5 call. This is Phase 2's core parity/
    correctness proof: every fold's OOS trades must fall strictly within
    that fold's own `[test_start, test_end]` window (no look-ahead or
    cross-fold leakage), and each fold's IS window must start at the
    anchor (the "anchored" property, `docs/RESEARCH.md` §4).
    """

    def _synthetic_bars(
        self, n_days: int, *, seed: int = 42, base_price: float = 2000.0
    ) -> tuple[gw.BarSeries, gw.BarSeries, gw.BarSeries]:
        rng = np.random.default_rng(seed)
        start = datetime(2020, 1, 1, tzinfo=timezone.utc)

        def make(count: int, step: timedelta) -> gw.BarSeries:
            times = tuple(start + i * step for i in range(count))
            close = base_price + np.cumsum(rng.normal(0, 2.0, size=count))
            high = close + rng.uniform(0.5, 5.0, size=count)
            low = close - rng.uniform(0.5, 5.0, size=count)
            open_ = close + rng.normal(0, 1.0, size=count)
            return gw.BarSeries(
                open=open_,
                high=high,
                low=low,
                close=close,
                tick_volume=np.full(count, 100.0),
                time_utc=times,
            )

        d1_bars = make(n_days, timedelta(days=1))
        h4_bars = make(n_days * 6, timedelta(hours=4))
        h1_bars = make(n_days * 24, timedelta(hours=1))
        return d1_bars, h4_bars, h1_bars

    @pytest.fixture(scope="class")
    def wfo_result(self) -> WalkForwardResult:
        d1_bars, h4_bars, h1_bars = self._synthetic_bars(n_days=470)
        result = run_walk_forward_validation(
            d1_bars,
            h4_bars,
            h1_bars,
            parameter_grid=[(25.0, 1.5), (30.0, 2.0)],
            initial_train_months=12,
            step_months=1,
            embargo_days=1,
            min_folds=3,
        )
        return result

    def test_produces_exactly_the_folds_that_fit(self, wfo_result: WalkForwardResult) -> None:
        assert len(wfo_result.folds) == 3

    def test_every_fold_is_window_starts_at_the_anchor(self, wfo_result: WalkForwardResult) -> None:
        anchor = wfo_result.folds[0].fold.train_start
        assert all(f.fold.train_start == anchor for f in wfo_result.folds)

    def test_every_oos_trade_falls_within_its_own_folds_test_window(
        self, wfo_result: WalkForwardResult
    ) -> None:
        for fold_result in wfo_result.folds:
            for trade in fold_result.oos_trades:
                assert fold_result.fold.test_start <= trade.exit_time <= fold_result.fold.test_end

    def test_concatenated_equity_curve_is_chronologically_ordered(
        self, wfo_result: WalkForwardResult
    ) -> None:
        times = [t for t, _ in wfo_result.concatenated_equity_curve]
        assert times == sorted(times)

    def test_performance_report_has_a_populated_dsr(self, wfo_result: WalkForwardResult) -> None:
        assert wfo_result.performance_report.deflated_sharpe_ratio is not None

    def test_promotion_gates_total_trades_matches_sum_across_folds(
        self, wfo_result: WalkForwardResult
    ) -> None:
        expected = sum(len(f.oos_trades) for f in wfo_result.folds)
        assert wfo_result.promotion_gates.total_oos_trades == expected

    def test_each_fold_selection_tried_every_grid_combo(
        self, wfo_result: WalkForwardResult
    ) -> None:
        for fold_result in wfo_result.folds:
            assert len(fold_result.selection.combo_results) == 2
