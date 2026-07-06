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
from datetime import datetime, timezone
from pathlib import Path

import pytest

import broker.mt5_gateway as gw
import main as orchestrator
import optimizer.self_learning as sl
from broker.clock_provider import MT5ClockProvider
from container import ApplicationContainer
from execution.position_manager import OrderActionPayload
from news.calendar_provider import OfflineSnapshotCalendarProvider
from risk.drawdown_fsm import DrawdownState
from storage.state_manager import StateManager, TradeLedgerEntry
from tests.conftest import FakeMT5, FakePosition, FakeSymbolInfo, FakeTick

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
