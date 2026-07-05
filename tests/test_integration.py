"""Integration tests: exercises that cross a real boundary — a simulated
MT5 server dropout, a simulated socket/HTTP disconnection, a real SQLite
database's transactional rollback behavior, and cross-module state
validation (broker/ledger reconciliation, crash recovery, and the
weekend optimizer's isolation guarantee against active trading state).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

import broker.mt5_gateway as gw
import news.news_engine as ne
import optimizer.self_learning as sl
from execution.position_manager import OrderActionPayload
from storage.state_manager import StateManager, TradeLedgerEntry
from tests.conftest import FakeMT5, FakePosition, FakeSymbolInfo, FakeTick

# ---------------------------------------------------------------------------
# Simulated MT5 server dropouts (broker/mt5_gateway.py)
# ---------------------------------------------------------------------------


class TestMT5ServerDropouts:
    def test_reconnect_succeeds_after_transient_failures(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulates a flaky MT5 terminal: two failed initialize() calls
        (as if the terminal/server connection dropped) followed by a
        successful third attempt, verifying exponential backoff delays
        and that the gateway ends up fully initialized."""
        fake_mt5.initialize_results = [False, False, True]
        fake_mt5.symbols["XAUUSD"] = _make_symbol("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(
            time_=int(datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc).timestamp())
        )
        monkeypatch.setattr(gw, "mt5", fake_mt5)

        sleep_calls: list[float] = []
        monkeypatch.setattr(
            "broker.mt5_gateway.time.sleep", lambda seconds: sleep_calls.append(seconds)
        )

        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect(max_attempts=6, initial_delay_seconds=1.0, max_delay_seconds=60.0)

        assert sleep_calls == [1.0, 2.0]
        assert gateway.symbol_spec.name == "XAUUSD"
        assert gateway.broker_utc_offset is not None

    def test_reconnect_exhaustion_raises_with_last_error(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulates a fully down MT5 server: every initialize() attempt
        fails, and backoff exhausts without ever connecting."""
        fake_mt5.initialize_results = [False, False, False]
        fake_mt5.last_error_value = (10004, "no connection")
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        monkeypatch.setattr("broker.mt5_gateway.time.sleep", lambda seconds: None)

        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=1)
        with pytest.raises(gw.BrokerConnectionError, match="3 attempts"):
            gateway.connect(max_attempts=3, initial_delay_seconds=0.01, max_delay_seconds=1.0)


def _make_symbol(
    name: str, visible: bool, point: float = 0.01, tick_value: float = 1.0
) -> FakeSymbolInfo:
    return FakeSymbolInfo(name, visible=visible, point=point, tick_value=tick_value)


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

    def test_get_account_state_raises_when_unavailable(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        monkeypatch.setattr(fake_mt5, "account_info", lambda: None)
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        with pytest.raises(gw.BrokerConnectionError):
            gateway.get_account_state()

    def test_get_bars_returns_typed_arrays(
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
        ]

        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=555)
        gateway.connect()
        bars = gateway.get_bars(gw.TIMEFRAME_H1, 2)
        assert list(bars.close) == [2002.0, 2008.0]
        assert list(bars.tick_volume) == [100.0, 150.0]
        assert len(bars.time_utc) == 2

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


# ---------------------------------------------------------------------------
# Simulated socket/HTTP disconnections (news/news_engine.py)
# ---------------------------------------------------------------------------


class FakeHTTPResponse:
    def __init__(self, status_code: int, json_data: object = None, text: str = "") -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self) -> object:
        if self._json_data is None:
            raise ValueError("no JSON body")
        return self._json_data


class TestNewsFeedSocketDisconnections:
    FROM_UTC = datetime(2026, 7, 4, tzinfo=timezone.utc)
    TO_UTC = datetime(2026, 7, 11, tzinfo=timezone.utc)

    def test_successful_fetch_with_custom_timeouts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = {}

        def fake_get(
            url: str,
            params: dict[str, str] | None = None,
            timeout: tuple[float, float] | None = None,
        ) -> FakeHTTPResponse:
            captured["timeout"] = timeout
            return FakeHTTPResponse(
                200,
                [
                    {
                        "title": "Non-Farm Payrolls",
                        "country": "US",
                        "impact": "High",
                        "date": "2026-07-04T12:30:00+00:00",
                    }
                ],
            )

        monkeypatch.setattr(requests, "get", fake_get)
        events = ne.fetch_calendar_events(
            "https://example.com/calendar",
            "fake-key",
            self.FROM_UTC,
            self.TO_UTC,
            connect_timeout_seconds=3.0,
            read_timeout_seconds=7.0,
        )
        assert len(events) == 1
        assert captured["timeout"] == (3.0, 7.0)

    def test_connection_error_raises_news_feed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_get(
            url: str,
            params: dict[str, str] | None = None,
            timeout: tuple[float, float] | None = None,
        ) -> FakeHTTPResponse:
            raise requests.exceptions.ConnectionError("socket refused")

        monkeypatch.setattr(requests, "get", fake_get)
        with pytest.raises(ne.NewsFeedConnectionError, match="unreachable"):
            ne.fetch_calendar_events(
                "https://example.com/calendar", "fake-key", self.FROM_UTC, self.TO_UTC
            )

    def test_timeout_raises_news_feed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_get(
            url: str,
            params: dict[str, str] | None = None,
            timeout: tuple[float, float] | None = None,
        ) -> FakeHTTPResponse:
            raise requests.exceptions.Timeout("timed out")

        monkeypatch.setattr(requests, "get", fake_get)
        with pytest.raises(ne.NewsFeedConnectionError, match="unreachable"):
            ne.fetch_calendar_events(
                "https://example.com/calendar", "fake-key", self.FROM_UTC, self.TO_UTC
            )

    def test_non_200_status_raises_news_feed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_get(
            url: str,
            params: dict[str, str] | None = None,
            timeout: tuple[float, float] | None = None,
        ) -> FakeHTTPResponse:
            return FakeHTTPResponse(503, text="Service Unavailable")

        monkeypatch.setattr(requests, "get", fake_get)
        with pytest.raises(ne.NewsFeedConnectionError, match="HTTP 503"):
            ne.fetch_calendar_events(
                "https://example.com/calendar", "fake-key", self.FROM_UTC, self.TO_UTC
            )

    def test_invalid_json_raises_news_feed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_get(
            url: str,
            params: dict[str, str] | None = None,
            timeout: tuple[float, float] | None = None,
        ) -> FakeHTTPResponse:
            return FakeHTTPResponse(200, json_data=None)

        monkeypatch.setattr(requests, "get", fake_get)
        with pytest.raises(ne.NewsFeedConnectionError, match="invalid JSON"):
            ne.fetch_calendar_events(
                "https://example.com/calendar", "fake-key", self.FROM_UTC, self.TO_UTC
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


class TestCrashRecovery:
    def test_fsm_state_survives_ungraceful_process_death(self, tmp_path: Path) -> None:
        db_path = tmp_path / "crash.db"
        first_instance = StateManager(db_path)
        first_instance.save_fsm_state(
            {"open_positions": ["XAUUSD"], "phase": "AWAITING_FILL"}, last_sequence_id=42
        )
        del first_instance  # simulate abrupt process death (no close())

        second_instance = StateManager(db_path)
        loaded = second_instance.load_fsm_state()
        assert loaded is not None
        state, sequence_id = loaded
        assert state == {"open_positions": ["XAUUSD"], "phase": "AWAITING_FILL"}
        assert sequence_id == 42
        second_instance.close()

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
