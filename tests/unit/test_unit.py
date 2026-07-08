"""Unit tests: pure-function/logic-level coverage for every module with
business logic. No network, no live MT5 terminal, no external process —
anything that crosses a real boundary (HTTP, MT5, multi-module state
persistence) belongs in tests/integration/test_integration.py instead;
simulated fault/disruption scenarios belong in tests/chaos/test_chaos.py.
"""

from __future__ import annotations

import ast
import glob
import logging
import os
import random
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

import broker.clock_provider as cp
import broker.mt5_gateway as gw
import main as orchestrator
import news.calendar_provider as calp
import resilience.backoff as backoff
from config.calendar_config import CalendarConfig
from config.config_manager import ConfigManager, ConfigurationError, ConfigValidator
from config.feature_flags import FeatureFlagManager, FeatureFlags
from config.secret_redaction import SecretRedactingFilter
from execution.position_manager import (
    EMERGENCY_LIQUIDATION_COMMENT,
    PositionState,
    build_emergency_liquidation_action,
    build_short_term_liquidation_action,
    calculate_base_take_profit,
    calculate_trailing_stop,
    evaluate_partial_close_and_breakeven,
)
from execution.validation import SeverityLevel, check_duplicate_order_before_retry
from indicators.math_engine import adx, atr, ema, sma
from news.news_engine import (
    EconomicEvent,
    NewsFeedConnectionError,
    NewsFeedHealthState,
    apply_news_feed_fail_safe,
    is_trade_entry_locked,
)
from optimizer.self_learning import (
    LedgerPerformanceMetrics,
    TunableParameter,
    compute_ledger_metrics,
    decide_parameter_shift,
    is_market_closed_for_optimization,
    run_monte_carlo_bootstrap,
)
from risk.drawdown_fsm import (
    BaselineEpoch,
    DrawdownEvent,
    DrawdownState,
    EquityBaselines,
    blocks_new_entries,
    blocks_position_management,
    classify_drawdown_event,
    decide_hard_lock_response,
    roll_equity_baselines,
    seed_equity_baselines,
    transition_drawdown_state,
)
from risk.risk_manager import calculate_compounded_lot_size, clamp_lot_size
from storage.db_engine import DEFAULT_BUSY_TIMEOUT_MS, checkpoint_wal, connect, initialize_schema
from storage.migrations import MIGRATIONS, apply_pending_migrations, get_applied_migrations
from storage.state_manager import (
    AuditActionType,
    OrderEvent,
    OrderLifecycleState,
    StateManager,
    TradeLedgerEntry,
)
from strategy.execution_triggers import (
    BreakoutSignal,
    PullbackSignal,
    WickFillResult,
    analyze_wick_fill,
    detect_breakout,
    detect_pullback,
)
from strategy.trend_filter import (
    ShortTermTrendAlignment,
    TrendAlignment,
    evaluate_master_trend,
    evaluate_short_term_trend,
)
from tests.conftest import FakeMT5, FakePosition, FakeSymbolInfo, FakeTick

# ---------------------------------------------------------------------------
# config/config_manager.py
# ---------------------------------------------------------------------------


class TestConfigManager:
    REQUIRED_ENV = {
        "MT5_LOGIN": "12345",
        "MT5_PASSWORD": "secret",
        "MT5_SERVER": "Broker-Demo",
        "ECONOMIC_CALENDAR_API_KEY": "abc123",
        "STRATEGY_MAGIC_NUMBER": "987654",
        "ENVIRONMENT_MODE": "DEMO",
        "TRADING_MODE": "WAIT_FOR_CONDITIONS",
        "SHORT_TERM_MAGIC_NUMBER": "987655",
    }

    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in self.REQUIRED_ENV:
            monkeypatch.delenv(key, raising=False)

    def test_missing_vars_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        with pytest.raises(ConfigurationError, match="Missing required environment"):
            ConfigManager.load(env_file="nonexistent.env")

    def test_invalid_environment_mode_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("ENVIRONMENT_MODE", "PRODUCTION")
        with pytest.raises(ConfigurationError, match="ENVIRONMENT_MODE"):
            ConfigManager.load(env_file="nonexistent.env")

    def test_valid_config_loads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        cfg = ConfigManager.load(env_file="nonexistent.env")
        assert cfg.mt5_login == 12345
        assert cfg.strategy_magic_number == 987654
        assert cfg.environment_mode == "DEMO"
        assert cfg.trading_mode == "WAIT_FOR_CONDITIONS"
        assert cfg.short_term_magic_number == 987655

    def test_invalid_trading_mode_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("TRADING_MODE", "TURBO")
        with pytest.raises(ConfigurationError, match="TRADING_MODE"):
            ConfigManager.load(env_file="nonexistent.env")

    def test_short_term_magic_number_equal_to_strategy_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_env(monkeypatch)
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("SHORT_TERM_MAGIC_NUMBER", "987654")
        with pytest.raises(ConfigurationError, match="SHORT_TERM_MAGIC_NUMBER"):
            ConfigManager.load(env_file="nonexistent.env")

    def test_non_integer_short_term_magic_number_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_env(monkeypatch)
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("SHORT_TERM_MAGIC_NUMBER", "not-a-number")
        with pytest.raises(ConfigurationError, match="SHORT_TERM_MAGIC_NUMBER"):
            ConfigManager.load(env_file="nonexistent.env")

    def test_non_integer_mt5_login_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("MT5_LOGIN", "not-a-number")
        with pytest.raises(ConfigurationError, match="MT5_LOGIN"):
            ConfigManager.load(env_file="nonexistent.env")

    def test_placeholder_password_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("MT5_PASSWORD", "CHANGEME")
        with pytest.raises(ConfigurationError, match="Placeholder"):
            ConfigManager.load(env_file="nonexistent.env")

    def test_placeholder_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("ECONOMIC_CALENDAR_API_KEY", "your_api_key_here")
        with pytest.raises(ConfigurationError, match="Placeholder"):
            ConfigManager.load(env_file="nonexistent.env")

    def test_placeholder_check_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("MT5_SERVER", "REPLACE_ME")
        with pytest.raises(ConfigurationError, match="Placeholder"):
            ConfigManager.load(env_file="nonexistent.env")

    def test_real_looking_values_do_not_false_positive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "Broker-Demo" and "abc123" must not trip the placeholder check.
        self._clear_env(monkeypatch)
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        cfg = ConfigManager.load(env_file="nonexistent.env")
        assert cfg.mt5_server == "Broker-Demo"


class TestConfigValidator:
    """Direct unit tests for ConfigValidator's individual check methods,
    independent of ConfigManager.load()'s environment-variable plumbing."""

    VALID_ENV = {
        "MT5_LOGIN": "1",
        "MT5_PASSWORD": "secret",
        "MT5_SERVER": "Broker-Demo",
        "ECONOMIC_CALENDAR_API_KEY": "abc123",
        "STRATEGY_MAGIC_NUMBER": "555",
        "ENVIRONMENT_MODE": "LIVE",
        "TRADING_MODE": "BOTH",
        "SHORT_TERM_MAGIC_NUMBER": "556",
    }

    def test_validate_returns_typed_tuple(self) -> None:
        validator = ConfigValidator()
        mt5_login, magic_number, mode, trading_mode, short_term_magic = validator.validate(
            self.VALID_ENV
        )
        assert (mt5_login, magic_number, mode, trading_mode, short_term_magic) == (
            1,
            555,
            "LIVE",
            "BOTH",
            556,
        )

    def test_check_trading_mode_valid_passes(self) -> None:
        assert ConfigValidator().check_trading_mode(self.VALID_ENV) == "BOTH"

    def test_check_trading_mode_invalid_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="TRADING_MODE"):
            ConfigValidator().check_trading_mode({**self.VALID_ENV, "TRADING_MODE": "TURBO"})

    def test_check_magic_numbers_distinct_passes_when_different(self) -> None:
        ConfigValidator().check_magic_numbers_distinct(555, 556)

    def test_check_magic_numbers_distinct_raises_when_equal(self) -> None:
        with pytest.raises(ConfigurationError, match="SHORT_TERM_MAGIC_NUMBER"):
            ConfigValidator().check_magic_numbers_distinct(555, 555)

    def test_check_presence_passes_silently_when_complete(self) -> None:
        ConfigValidator().check_presence(self.VALID_ENV)

    def test_check_no_placeholder_leak_passes_silently_when_clean(self) -> None:
        ConfigValidator().check_no_placeholder_leak(self.VALID_ENV)


# ---------------------------------------------------------------------------
# config/calendar_config.py (docs/PRODUCTION_SPEC.md §2)
# ---------------------------------------------------------------------------


class TestCalendarConfig:
    CALENDAR_ENV_KEYS = (
        "CALENDAR_PROVIDER_PRIORITY",
        "CALENDAR_TIMEOUT_MS",
        "CALENDAR_RATE_LIMIT_PER_MIN",
        "CALENDAR_TRADINGECONOMICS_BASE_URL",
        "CALENDAR_FINNHUB_BASE_URL",
        "CALENDAR_OFFLINE_SNAPSHOT_PATH",
    )

    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in self.CALENDAR_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)

    def test_defaults_require_no_configuration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        config = CalendarConfig.from_env()
        assert config.provider_priority == ("offline_snapshot",)
        assert config.timeout_ms == 3000
        assert config.rate_limit_per_min == 60
        assert config.provider_base_urls == {}

    def test_custom_priority_with_base_urls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv(
            "CALENDAR_PROVIDER_PRIORITY", "tradingeconomics,finnhub,offline_snapshot"
        )
        monkeypatch.setenv("CALENDAR_TRADINGECONOMICS_BASE_URL", "https://te.example.com/cal")
        monkeypatch.setenv("CALENDAR_FINNHUB_BASE_URL", "https://finnhub.example.com/cal")
        config = CalendarConfig.from_env()
        assert config.provider_priority == ("tradingeconomics", "finnhub", "offline_snapshot")
        assert config.provider_base_urls == {
            "tradingeconomics": "https://te.example.com/cal",
            "finnhub": "https://finnhub.example.com/cal",
        }

    def test_unknown_provider_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("CALENDAR_PROVIDER_PRIORITY", "not_a_real_provider")
        with pytest.raises(ConfigurationError, match="unknown provider"):
            CalendarConfig.from_env()

    def test_network_provider_missing_base_url_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("CALENDAR_PROVIDER_PRIORITY", "finnhub")
        with pytest.raises(ConfigurationError, match="CALENDAR_FINNHUB_BASE_URL"):
            CalendarConfig.from_env()

    def test_non_integer_timeout_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("CALENDAR_TIMEOUT_MS", "not-a-number")
        with pytest.raises(ConfigurationError, match="CALENDAR_TIMEOUT_MS"):
            CalendarConfig.from_env()


# ---------------------------------------------------------------------------
# config/secret_redaction.py
# ---------------------------------------------------------------------------


class TestSecretRedaction:
    def _make_record(self, message: str) -> logging.LogRecord:
        return logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=message,
            args=(),
            exc_info=None,
        )

    def test_redacts_configured_secret(self) -> None:
        filt = SecretRedactingFilter(["super-secret-password"])
        record = self._make_record("login failed with password super-secret-password")
        assert filt.filter(record) is True
        assert "super-secret-password" not in record.getMessage()
        assert SecretRedactingFilter.REDACTED in record.getMessage()

    def test_leaves_unrelated_messages_untouched(self) -> None:
        filt = SecretRedactingFilter(["super-secret-password"])
        record = self._make_record("connected to broker successfully")
        filt.filter(record)
        assert record.getMessage() == "connected to broker successfully"

    def test_empty_secret_list_is_a_no_op(self) -> None:
        filt = SecretRedactingFilter([])
        record = self._make_record("anything goes here")
        assert filt.filter(record) is True
        assert record.getMessage() == "anything goes here"

    def test_ignores_blank_secret_values(self) -> None:
        # An empty-string "secret" (e.g. an optional field left unset)
        # must never cause every log message to be wiped out.
        filt = SecretRedactingFilter(["", "real-secret"])
        record = self._make_record("some message with real-secret in it")
        filt.filter(record)
        assert record.getMessage() == f"some message with {SecretRedactingFilter.REDACTED} in it"

    def test_longest_match_wins_for_overlapping_secrets(self) -> None:
        # "secret" is a substring of "secret123"; redacting the shorter one
        # first would leave a mangled "***REDACTED***123" behind.
        filt = SecretRedactingFilter(["secret", "secret123"])
        record = self._make_record("token=secret123")
        filt.filter(record)
        assert record.getMessage() == f"token={SecretRedactingFilter.REDACTED}"


# ---------------------------------------------------------------------------
# storage/db_engine.py
# ---------------------------------------------------------------------------


class TestDbEngine:
    def test_read_only_connection_can_read_but_not_write(self, tmp_path: Path) -> None:
        db_path = tmp_path / "readonly.db"
        writer = connect(db_path)
        initialize_schema(writer)
        writer.close()

        reader = connect(db_path, read_only=True)
        try:
            mode = reader.execute("PRAGMA query_only;").fetchone()[0]
            assert mode == 1
            # A read-only connection can still query existing tables.
            count = reader.execute("SELECT COUNT(*) FROM trade_ledger").fetchone()[0]
            assert count == 0
        finally:
            reader.close()

    def test_checkpoint_wal_does_not_raise(self, tmp_path: Path) -> None:
        connection = connect(tmp_path / "checkpoint.db")
        initialize_schema(connection)
        checkpoint_wal(connection)  # must not raise
        connection.close()


# ---------------------------------------------------------------------------
# storage/migrations.py (docs/PRODUCTION_SPEC.md §4's "lightweight internal
# Schema Migration framework")
# ---------------------------------------------------------------------------


class TestSchemaMigrations:
    def test_fresh_database_applies_every_migration(self, tmp_path: Path) -> None:
        connection = connect(tmp_path / "migrate.db")
        try:
            applied = apply_pending_migrations(connection)
            assert applied == tuple(m.version for m in MIGRATIONS)
            assert get_applied_migrations(connection) == tuple(m.version for m in MIGRATIONS)
        finally:
            connection.close()

    def test_rerun_against_already_migrated_database_is_a_no_op(self, tmp_path: Path) -> None:
        connection = connect(tmp_path / "migrate_rerun.db")
        try:
            apply_pending_migrations(connection)
            second_run = apply_pending_migrations(connection)
            assert second_run == ()
        finally:
            connection.close()

    def test_order_ledger_and_event_store_tables_exist_after_migration(
        self, tmp_path: Path
    ) -> None:
        connection = connect(tmp_path / "migrate_tables.db")
        try:
            apply_pending_migrations(connection)
            connection.execute(
                "INSERT INTO order_events (client_order_id, event_type, metadata_json, "
                "created_at_utc) VALUES ('abc', 'REQUESTED', '{}', '2026-07-04T00:00:00Z')"
            )
            connection.execute(
                "INSERT INTO order_ledger (client_order_id, state, timestamp) "
                "VALUES ('abc', 'REQUESTED', '2026-07-04T00:00:00Z')"
            )
        finally:
            connection.close()

    def test_invalid_event_type_rejected_by_check_constraint(self, tmp_path: Path) -> None:
        connection = connect(tmp_path / "migrate_invalid.db")
        try:
            apply_pending_migrations(connection)
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO order_events (client_order_id, event_type, metadata_json, "
                    "created_at_utc) VALUES ('abc', 'NOT_A_REAL_STATE', '{}', "
                    "'2026-07-04T00:00:00Z')"
                )
        finally:
            connection.close()

    def test_order_events_is_structurally_append_only(self, tmp_path: Path) -> None:
        """UPDATE/DELETE against order_events must be rejected by the
        database engine itself (a trigger), not merely by convention — the
        literal reading of "structurally append-only"."""
        connection = connect(tmp_path / "migrate_append_only.db")
        try:
            apply_pending_migrations(connection)
            connection.execute(
                "INSERT INTO order_events (client_order_id, event_type, metadata_json, "
                "created_at_utc) VALUES ('abc', 'REQUESTED', '{}', '2026-07-04T00:00:00Z')"
            )
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute("UPDATE order_events SET event_type = 'SENT' WHERE id = 1")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute("DELETE FROM order_events WHERE id = 1")
        finally:
            connection.close()

    def test_audit_trail_table_exists_and_is_append_only(self, tmp_path: Path) -> None:
        connection = connect(tmp_path / "migrate_audit_trail.db")
        try:
            apply_pending_migrations(connection)
            connection.execute(
                "INSERT INTO audit_trail (timestamp_utc, actor_signature, action_type, "
                "parameter_name, old_value, new_value, metadata_json) VALUES "
                "('2026-07-04T00:00:00Z', 'deadbeef', 'MANUAL_OVERRIDE', 'x', '1', '2', '{}')"
            )
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute("UPDATE audit_trail SET action_type = 'OTHER'")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute("DELETE FROM audit_trail")
        finally:
            connection.close()


# ---------------------------------------------------------------------------
# storage/state_manager.py's order-lifecycle Event Store additions
# (docs/PRODUCTION_SPEC.md §4/§5)
# ---------------------------------------------------------------------------


class TestOrderEventStore:
    def test_record_order_event_writes_event_and_projection_atomically(
        self, state_manager: StateManager
    ) -> None:
        state_manager.record_order_event(
            "order-1", OrderLifecycleState.REQUESTED, {"symbol": "XAUUSD"}
        )
        assert state_manager.get_order_ledger_state("order-1") == "REQUESTED"
        events = state_manager.get_order_events("order-1")
        assert len(events) == 1
        assert events[0].event_type == OrderLifecycleState.REQUESTED
        assert events[0].metadata == {"symbol": "XAUUSD"}

    def test_sequential_events_are_ordered_and_projection_reflects_latest(
        self, state_manager: StateManager
    ) -> None:
        state_manager.record_order_event("order-2", OrderLifecycleState.REQUESTED)
        state_manager.record_order_event("order-2", OrderLifecycleState.SENT)
        state_manager.record_order_event("order-2", OrderLifecycleState.FILLED)

        events = state_manager.get_order_events("order-2")
        assert [e.event_type for e in events] == [
            OrderLifecycleState.REQUESTED,
            OrderLifecycleState.SENT,
            OrderLifecycleState.FILLED,
        ]
        assert events[0].sequence_id < events[1].sequence_id < events[2].sequence_id
        assert state_manager.get_order_ledger_state("order-2") == "FILLED"

    def test_latest_order_event_returns_most_recent(self, state_manager: StateManager) -> None:
        state_manager.record_order_event("order-3", OrderLifecycleState.REQUESTED)
        state_manager.record_order_event("order-3", OrderLifecycleState.REJECTED, {"error": "x"})
        latest = state_manager.get_latest_order_event("order-3")
        assert latest is not None
        assert latest.event_type == OrderLifecycleState.REJECTED
        assert latest.metadata == {"error": "x"}

    def test_unknown_client_order_id_returns_none(self, state_manager: StateManager) -> None:
        assert state_manager.get_order_ledger_state("never-seen") is None
        assert state_manager.get_latest_order_event("never-seen") is None
        assert state_manager.get_order_events("never-seen") == []

    def test_missing_metadata_defaults_to_empty_dict(self, state_manager: StateManager) -> None:
        state_manager.record_order_event("order-4", OrderLifecycleState.REQUESTED)
        events = state_manager.get_order_events("order-4")
        assert events[0].metadata == {}

    def test_order_events_append_only_via_state_manager_connection(
        self, state_manager: StateManager
    ) -> None:
        state_manager.record_order_event("order-5", OrderLifecycleState.REQUESTED)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            state_manager._connection.execute(
                "UPDATE order_events SET event_type = 'SENT' WHERE client_order_id = 'order-5'"
            )


# ---------------------------------------------------------------------------
# execution/validation.py's PreTradeValidator duplicate-order gate
# (docs/PRODUCTION_SPEC.md §4/§5, RR-007)
# ---------------------------------------------------------------------------


class TestCheckDuplicateOrderBeforeRetry:
    def _event(self, event_type: OrderLifecycleState) -> OrderEvent:
        return OrderEvent(
            sequence_id=1,
            client_order_id="order-1",
            event_type=event_type,
            metadata={},
            created_at_utc="2026-07-04T00:00:00.000000Z",
        )

    def test_no_prior_event_is_a_new_order(self) -> None:
        result = check_duplicate_order_before_retry(None, broker_ticket_still_open=None)
        assert result.is_valid is True
        assert result.is_retryable is True
        assert result.reason_code == "NEW_ORDER_NO_PRIOR_EVENT"
        assert result.severity == SeverityLevel.INFO

    @pytest.mark.parametrize(
        "state", [OrderLifecycleState.REQUESTED, OrderLifecycleState.VALIDATED]
    )
    def test_pre_send_states_are_safe_to_retry(self, state: OrderLifecycleState) -> None:
        result = check_duplicate_order_before_retry(
            self._event(state), broker_ticket_still_open=None
        )
        assert result.is_valid is True
        assert result.is_retryable is True
        assert result.reason_code == "NOT_YET_SENT_SAFE_TO_RETRY"

    def test_closed_order_is_not_retryable(self) -> None:
        result = check_duplicate_order_before_retry(
            self._event(OrderLifecycleState.CLOSED), broker_ticket_still_open=None
        )
        assert result.is_valid is False
        assert result.is_retryable is False
        assert result.reason_code == "ORDER_ALREADY_CLOSED"
        assert result.severity == SeverityLevel.ERROR

    @pytest.mark.parametrize(
        "state",
        [OrderLifecycleState.REJECTED, OrderLifecycleState.EXPIRED, OrderLifecycleState.CANCELLED],
    )
    def test_terminal_negative_states_are_safe_to_retry(self, state: OrderLifecycleState) -> None:
        result = check_duplicate_order_before_retry(
            self._event(state), broker_ticket_still_open=None
        )
        assert result.is_valid is True
        assert result.is_retryable is True
        assert result.reason_code == "PRIOR_ATTEMPT_TERMINALLY_REJECTED"
        assert result.severity == SeverityLevel.WARNING

    def test_in_flight_state_with_broker_confirmed_open_ticket_is_duplicate(self) -> None:
        result = check_duplicate_order_before_retry(
            self._event(OrderLifecycleState.SENT), broker_ticket_still_open=True
        )
        assert result.is_valid is False
        assert result.is_retryable is False
        assert result.reason_code == "DUPLICATE_ORDER_DETECTED"
        assert result.severity == SeverityLevel.CRITICAL

    def test_in_flight_state_with_no_confirmed_ticket_is_ambiguous_not_safe(self) -> None:
        result = check_duplicate_order_before_retry(
            self._event(OrderLifecycleState.FILLED), broker_ticket_still_open=False
        )
        assert result.is_valid is False
        assert result.is_retryable is False
        assert result.reason_code == "AMBIGUOUS_SENT_STATE_MANUAL_REVIEW_REQUIRED"
        assert result.severity == SeverityLevel.CRITICAL

    def test_in_flight_state_with_no_ticket_to_check_is_ambiguous_not_safe(self) -> None:
        result = check_duplicate_order_before_retry(
            self._event(OrderLifecycleState.PENDING), broker_ticket_still_open=None
        )
        assert result.is_valid is False
        assert result.is_retryable is False
        assert result.reason_code == "AMBIGUOUS_SENT_STATE_MANUAL_REVIEW_REQUIRED"


# ---------------------------------------------------------------------------
# indicators/math_engine.py
# ---------------------------------------------------------------------------


def _ref_ema(values: list[float], period: int) -> list[float]:
    n = len(values)
    result = [float("nan")] * n
    alpha = 2.0 / (period + 1.0)
    result[period - 1] = sum(values[:period]) / period
    for i in range(period, n):
        result[i] = alpha * values[i] + (1.0 - alpha) * result[i - 1]
    return result


def _ref_true_range(high: list[float], low: list[float], close: list[float]) -> list[float]:
    n = len(high)
    tr = [0.0] * n
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    return tr


def _ref_wilder_smooth(values: list[float], period: int, start: int = 0) -> list[float]:
    n = len(values)
    result = [float("nan")] * n
    seed_index = start + period - 1
    result[seed_index] = sum(values[start : seed_index + 1])
    for i in range(seed_index + 1, n):
        result[i] = result[i - 1] - (result[i - 1] / period) + values[i]
    return result


def _ref_atr(high: list[float], low: list[float], close: list[float], period: int) -> list[float]:
    tr = _ref_true_range(high, low, close)
    smoothed = _ref_wilder_smooth(tr, period)
    return [v / period if v == v else float("nan") for v in smoothed]


def _ref_adx(high: list[float], low: list[float], close: list[float], period: int) -> list[float]:
    n = len(high)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
    tr = _ref_true_range(high, low, close)
    smoothed_tr = _ref_wilder_smooth(tr, period)
    smoothed_plus_dm = _ref_wilder_smooth(plus_dm, period)
    smoothed_minus_dm = _ref_wilder_smooth(minus_dm, period)

    dx = [float("nan")] * n
    for i in range(period - 1, n):
        if smoothed_tr[i] == 0:
            plus_di, minus_di = 0.0, 0.0
        else:
            plus_di = 100.0 * smoothed_plus_dm[i] / smoothed_tr[i]
            minus_di = 100.0 * smoothed_minus_dm[i] / smoothed_tr[i]
        denom = plus_di + minus_di
        dx[i] = 0.0 if denom == 0 else 100.0 * abs(plus_di - minus_di) / denom

    smoothed_dx = _ref_wilder_smooth(dx, period, start=period - 1)
    return [v / period if v == v else float("nan") for v in smoothed_dx]


class TestMathEngine:
    def test_sma_matches_reference(self) -> None:
        rng = np.random.default_rng(1)
        values = rng.uniform(1000, 5000, size=100)

        def ref_sma(vals: list[float], period: int) -> list[float]:
            n = len(vals)
            out = [float("nan")] * n
            for i in range(period - 1, n):
                out[i] = sum(vals[i - period + 1 : i + 1]) / period
            return out

        result = sma(values, 20)
        reference = ref_sma(values.tolist(), 20)
        np.testing.assert_allclose(result[19:], reference[19:], rtol=1e-10)

    def test_ema_of_constant_series_holds_steady(self) -> None:
        constant = np.full(50, 100.0)
        result = ema(constant, 10)
        assert np.all(result[9:] == 100.0)

    def test_ema_matches_reference(self) -> None:
        rng = np.random.default_rng(42)
        values = rng.uniform(1900, 2000, size=300)
        result = ema(values, 40)
        reference = _ref_ema(values.tolist(), 40)
        np.testing.assert_allclose(result[39:], reference[39:], rtol=1e-10)

    def test_ema_insufficient_data_raises(self) -> None:
        with pytest.raises(ValueError, match="need at least"):
            ema(np.array([1.0, 2.0, 3.0]), 10)

    @pytest.fixture
    def synthetic_ohlc(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        rng = np.random.default_rng(42)
        close = 1950 + np.cumsum(rng.normal(0, 2, size=300))
        high = close + rng.uniform(0.5, 5, size=300)
        low = close - rng.uniform(0.5, 5, size=300)
        return high, low, close

    def test_atr_matches_reference(
        self, synthetic_ohlc: tuple[np.ndarray, np.ndarray, np.ndarray]
    ) -> None:
        high, low, close = synthetic_ohlc
        result = atr(high, low, close, 14)
        reference = _ref_atr(high.tolist(), low.tolist(), close.tolist(), 14)
        np.testing.assert_allclose(result[13:], reference[13:], rtol=1e-9)

    def test_atr_mismatched_lengths_raises(self) -> None:
        with pytest.raises(ValueError, match="same length"):
            atr(np.array([1.0, 2.0]), np.array([1.0]), np.array([1.0, 2.0]), 14)

    def test_adx_matches_reference_and_stays_bounded(
        self, synthetic_ohlc: tuple[np.ndarray, np.ndarray, np.ndarray]
    ) -> None:
        high, low, close = synthetic_ohlc
        result = adx(high, low, close, 14)
        reference = _ref_adx(high.tolist(), low.tolist(), close.tolist(), 14)
        valid_from = 2 * (14 - 1)
        np.testing.assert_allclose(result[valid_from:], reference[valid_from:], rtol=1e-8)
        assert np.all(result[valid_from:] >= 0.0)
        assert np.all(result[valid_from:] <= 100.0)

    def test_adx_strong_uptrend_exceeds_threshold(self) -> None:
        rng = np.random.default_rng(7)
        n = 200
        close = 1900 + np.arange(n) * 3.0 + rng.normal(0, 0.5, size=n)
        high, low = close + 1.0, close - 1.0
        result = adx(high, low, close, 14)
        assert result[-1] > 25

    def test_adx_choppy_market_stays_below_threshold(self) -> None:
        rng = np.random.default_rng(7)
        n = 200
        close = 1950 + rng.normal(0, 1.0, size=n)
        high, low = close + 1.0, close - 1.0
        result = adx(high, low, close, 14)
        assert result[-1] < 25

    def test_adx_insufficient_data_raises(self) -> None:
        with pytest.raises(ValueError, match="need at least"):
            adx(np.ones(10), np.ones(10), np.ones(10), 14)


# ---------------------------------------------------------------------------
# strategy/trend_filter.py
# ---------------------------------------------------------------------------


def _uptrend(
    n: int, start: float, step: float, seed: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    close = start + np.arange(n) * step + rng.normal(0, 0.3, size=n)
    return close + 1.0, close - 1.0, close


def _downtrend(
    n: int, start: float, step: float, seed: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    return _uptrend(n, start, -step, seed)


def _choppy(n: int, level: float, seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    close = level + rng.normal(0, 1.0, size=n)
    return close + 1.0, close - 1.0, close


class TestTrendFilter:
    def test_full_bullish_alignment(self) -> None:
        _, _, d1_close = _uptrend(220, 1800, 2.0, 1)
        h4_high, h4_low, h4_close = _uptrend(70, 1800, 2.0, 2)
        h1_high, h1_low, h1_close = _uptrend(67, 1800, 2.0, 3)

        result = evaluate_master_trend(
            d1_close, h4_high, h4_low, h4_close, h1_high, h1_low, h1_close
        )
        assert result.direction == "BULLISH"
        assert result.d1_bullish and result.h4_bullish and result.h1_bullish
        assert result.adx_confirmed
        assert result.is_valid is True

    def test_full_bearish_alignment(self) -> None:
        _, _, d1_close = _downtrend(220, 2200, 2.0, 4)
        h4_high, h4_low, h4_close = _downtrend(70, 2200, 2.0, 5)
        h1_high, h1_low, h1_close = _downtrend(67, 2200, 2.0, 6)

        result = evaluate_master_trend(
            d1_close, h4_high, h4_low, h4_close, h1_high, h1_low, h1_close
        )
        assert result.direction == "BEARISH"
        assert result.is_valid is True

    def test_adx_trend_threshold_override_can_reject_a_default_confirm(self) -> None:
        # This alignment confirms at the default (25.0) threshold...
        _, _, d1_close = _uptrend(220, 1800, 2.0, 1)
        h4_high, h4_low, h4_close = _uptrend(70, 1800, 2.0, 2)
        h1_high, h1_low, h1_close = _uptrend(67, 1800, 2.0, 3)
        default_result = evaluate_master_trend(
            d1_close, h4_high, h4_low, h4_close, h1_high, h1_low, h1_close
        )
        assert default_result.adx_confirmed is True

        # ...but a stricter overridden threshold (above the real ADX value)
        # must reject the exact same alignment — proving the self-learning
        # optimizer's applied shift actually changes live behavior.
        strict_result = evaluate_master_trend(
            d1_close,
            h4_high,
            h4_low,
            h4_close,
            h1_high,
            h1_low,
            h1_close,
            adx_trend_threshold=default_result.adx_value + 10.0,
        )
        assert strict_result.adx_confirmed is False
        assert strict_result.is_valid is False

    def test_adx_trend_threshold_override_can_accept_a_default_reject(self) -> None:
        # This alignment fails ADX confirmation at the default threshold...
        _, _, d1_close = _choppy(220, 1950, 10)
        h4_high, h4_low, h4_close = _choppy(70, 1950, 11)
        h1_high, h1_low, h1_close = _choppy(67, 1950, 12)
        default_result = evaluate_master_trend(
            d1_close, h4_high, h4_low, h4_close, h1_high, h1_low, h1_close
        )
        assert default_result.adx_confirmed is False

        # ...but a looser overridden threshold (below the real ADX value)
        # must confirm it.
        loose_result = evaluate_master_trend(
            d1_close,
            h4_high,
            h4_low,
            h4_close,
            h1_high,
            h1_low,
            h1_close,
            adx_trend_threshold=0.5,
        )
        assert loose_result.adx_confirmed is True

    def test_mismatched_alignment_yields_none(self) -> None:
        _, _, d1_close = _uptrend(220, 1800, 2.0, 1)
        h4_high, h4_low, h4_close = _uptrend(70, 1800, 2.0, 2)
        h1_high, h1_low, h1_close = _downtrend(67, 2200, 2.0, 6)

        result = evaluate_master_trend(
            d1_close, h4_high, h4_low, h4_close, h1_high, h1_low, h1_close
        )
        assert result.direction == "NONE"
        assert result.is_valid is False

    def test_choppy_market_not_adx_confirmed(self) -> None:
        _, _, d1_close = _choppy(220, 1950, 10)
        h4_high, h4_low, h4_close = _choppy(70, 1950, 11)
        h1_high, h1_low, h1_close = _choppy(67, 1950, 12)

        result = evaluate_master_trend(
            d1_close, h4_high, h4_low, h4_close, h1_high, h1_low, h1_close
        )
        assert result.adx_confirmed is False
        assert result.is_valid is False

    def test_insufficient_history_raises(self) -> None:
        _, _, d1_close = _uptrend(220, 1800, 2.0, 1)
        h4_high, h4_low, h4_close = _uptrend(70, 1800, 2.0, 2)
        h1_high, h1_low, h1_close = _uptrend(67, 1800, 2.0, 3)
        with pytest.raises(ValueError, match="need at least"):
            evaluate_master_trend(
                d1_close[:50], h4_high, h4_low, h4_close, h1_high, h1_low, h1_close
            )


class TestShortTermTrendFilter:
    """`evaluate_short_term_trend()` — the short-term mode's relaxed,
    H1-only trend check (no D1/H4 alignment requirement, lower ADX bar)."""

    def test_bullish_h1_only_is_valid(self) -> None:
        h1_high, h1_low, h1_close = _uptrend(67, 1800, 2.0, 3)
        result = evaluate_short_term_trend(h1_high, h1_low, h1_close)
        assert result.direction == "BULLISH"
        assert result.h1_bullish is True
        assert result.adx_confirmed is True
        assert result.is_valid is True

    def test_bearish_h1_only_is_valid(self) -> None:
        h1_high, h1_low, h1_close = _downtrend(67, 2200, 2.0, 6)
        result = evaluate_short_term_trend(h1_high, h1_low, h1_close)
        assert result.direction == "BEARISH"
        assert result.h1_bearish is True
        assert result.is_valid is True

    def test_choppy_market_not_adx_confirmed(self) -> None:
        h1_high, h1_low, h1_close = _choppy(67, 1950, 12)
        result = evaluate_short_term_trend(h1_high, h1_low, h1_close)
        assert result.adx_confirmed is False
        assert result.is_valid is False

    def test_does_not_require_d1_or_h4_data(self) -> None:
        # Only 3 positional args (H1 series) — this would be a TypeError
        # if the function silently required higher-timeframe data too.
        h1_high, h1_low, h1_close = _uptrend(67, 1800, 2.0, 3)
        result = evaluate_short_term_trend(h1_high, h1_low, h1_close)
        assert isinstance(result, ShortTermTrendAlignment)


# ---------------------------------------------------------------------------
# strategy/execution_triggers.py ("M5 candle flags")
# ---------------------------------------------------------------------------


class TestExecutionTriggers:
    POINT = 0.01

    @pytest.fixture
    def flat_tick_volume(self) -> np.ndarray:
        return np.concatenate([np.full(19, 100.0), [151.0]])

    @pytest.fixture
    def confirmed_tick_volume(self) -> np.ndarray:
        return np.concatenate([np.full(19, 100.0), [200.0]])

    def test_bullish_breakout_at_exact_boundary(self, flat_tick_volume: np.ndarray) -> None:
        high = np.array([2350.0, 2351.0])
        low = np.array([2340.0, 2341.0])
        close = np.array([2340.0, 2350.50])  # prior_high(2350.0) + 50*0.01
        result = detect_breakout(high, low, close, flat_tick_volume, self.POINT)
        assert result.direction == "BUY"
        assert abs(result.breakout_distance_points - 50.0) < 1e-9

    def test_just_under_boundary_does_not_trigger(self, flat_tick_volume: np.ndarray) -> None:
        high = np.array([2350.0, 2351.0])
        low = np.array([2340.0, 2341.0])
        close = np.array([2340.0, 2350.49])
        result = detect_breakout(high, low, close, flat_tick_volume, self.POINT)
        assert result.direction == "NONE"

    def test_bearish_breakout_triggers(self, flat_tick_volume: np.ndarray) -> None:
        high = np.array([2350.0, 2351.0])
        low = np.array([2340.0, 2341.0])
        close = np.array([2351.0, 2339.5])  # prior_low(2340.0) - 50*0.01
        result = detect_breakout(high, low, close, flat_tick_volume, self.POINT)
        assert result.direction == "SELL"

    def test_volume_confirmation_gates_is_valid(
        self, flat_tick_volume: np.ndarray, confirmed_tick_volume: np.ndarray
    ) -> None:
        high = np.array([2350.0, 2351.0])
        low = np.array([2340.0, 2341.0])
        close = np.array([2340.0, 2350.50])
        unconfirmed = detect_breakout(high, low, close, flat_tick_volume, self.POINT)
        confirmed = detect_breakout(high, low, close, confirmed_tick_volume, self.POINT)
        assert unconfirmed.is_valid is False
        assert confirmed.is_valid is True

    def test_breakout_invalid_point_raises(self, flat_tick_volume: np.ndarray) -> None:
        with pytest.raises(ValueError, match="point must be"):
            detect_breakout(
                np.array([1.0, 2.0]),
                np.array([1.0, 2.0]),
                np.array([1.0, 2.0]),
                flat_tick_volume,
                0.0,
            )

    def test_bullish_pullback(self) -> None:
        result = detect_pullback(
            np.array([2360.0]),
            np.array([2348.0]),
            np.array([2352.0]),
            np.array([2350.0]),
            "BUY",
        )
        assert result.direction == "BUY"

    def test_no_trend_never_locks_pullback(self) -> None:
        result = detect_pullback(
            np.array([2360.0]),
            np.array([2348.0]),
            np.array([2352.0]),
            np.array([2350.0]),
            "NONE",
        )
        assert result.direction == "NONE"

    def test_close_exactly_at_level_does_not_count(self) -> None:
        result = detect_pullback(
            np.array([2360.0]),
            np.array([2348.0]),
            np.array([2350.0]),
            np.array([2350.0]),
            "BUY",
        )
        assert result.direction == "NONE"

    def test_bearish_pullback(self) -> None:
        result = detect_pullback(
            np.array([2352.0]),
            np.array([2340.0]),
            np.array([2348.0]),
            np.array([2350.0]),
            "SELL",
        )
        assert result.direction == "SELL"

    def test_long_lower_wick_is_bullish_rejection(self) -> None:
        result = analyze_wick_fill(
            np.array([108.0]), np.array([110.0]), np.array([100.0]), np.array([109.0])
        )
        assert result.rejection == "BUY"
        assert result.lower_shadow_ratio > 0.6

    def test_long_upper_wick_is_bearish_rejection(self) -> None:
        result = analyze_wick_fill(
            np.array([102.0]), np.array([110.0]), np.array([100.0]), np.array([101.0])
        )
        assert result.rejection == "SELL"

    def test_balanced_body_no_rejection(self) -> None:
        result = analyze_wick_fill(
            np.array([100.0]), np.array([110.0]), np.array([99.0]), np.array([109.0])
        )
        assert result.rejection == "NONE"

    def test_zero_range_bar_handled_safely(self) -> None:
        result = analyze_wick_fill(
            np.array([100.0]), np.array([100.0]), np.array([100.0]), np.array([100.0])
        )
        assert result.rejection == "NONE"
        assert result.upper_shadow_ratio == 0.0
        assert result.lower_shadow_ratio == 0.0


# ---------------------------------------------------------------------------
# risk/risk_manager.py ("lot math metrics")
# ---------------------------------------------------------------------------


class TestRiskManager:
    def test_clamp_rounds_down_to_step(self) -> None:
        assert clamp_lot_size(0.037, 0.01, 100.0, 0.01) == 0.03

    def test_clamp_below_min_clamps_up(self) -> None:
        assert clamp_lot_size(0.002, 0.01, 100.0, 0.01) == 0.01

    def test_clamp_above_max_clamps_down(self) -> None:
        assert clamp_lot_size(500.0, 0.01, 100.0, 0.01) == 100.0

    def test_clamp_non_positive_input_yields_min(self) -> None:
        assert clamp_lot_size(0.0, 0.01, 100.0, 0.01) == 0.01
        assert clamp_lot_size(-5.0, 0.01, 100.0, 0.01) == 0.01

    def test_clamp_invalid_constraints_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid broker volume constraints"):
            clamp_lot_size(1.0, 0.0, 100.0, 0.01)

    def test_compounding_tier_scaling(self) -> None:
        assert calculate_compounded_lot_size(500.0, 0.01, 100.0, 0.01) == 0.01
        assert calculate_compounded_lot_size(2500.0, 0.01, 100.0, 0.01) == 0.03

    def test_compounding_clamps_to_max(self) -> None:
        assert calculate_compounded_lot_size(1_000_000.0, 0.01, 5.0, 0.01) == 5.0

    def test_compounding_non_positive_equity_raises(self) -> None:
        with pytest.raises(ValueError, match="equity must be"):
            calculate_compounded_lot_size(0.0, 0.01, 100.0, 0.01)


# ---------------------------------------------------------------------------
# execution/position_manager.py
# ---------------------------------------------------------------------------


class TestPositionManager:
    def test_base_take_profit_buy_and_sell(self) -> None:
        assert calculate_base_take_profit(2000.0, 5.0, "BUY") == 2010.0
        assert calculate_base_take_profit(2000.0, 5.0, "SELL") == 1990.0

    def test_base_take_profit_non_positive_atr_raises(self) -> None:
        with pytest.raises(ValueError, match="atr_value must be"):
            calculate_base_take_profit(2000.0, 0.0, "BUY")

    def test_no_action_before_base_tp(self) -> None:
        position = PositionState(
            ticket=1,
            symbol="XAUUSD",
            side="BUY",
            volume=0.10,
            entry_price=2000.0,
            stop_loss=1990.0,
            magic_number=555,
            partial_closed=False,
            breakeven_set=False,
        )
        actions = evaluate_partial_close_and_breakeven(
            position,
            current_price=2005.0,
            atr_value=5.0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
        )
        assert actions == []

    def test_base_tp_reached_triggers_partial_close_and_breakeven(self) -> None:
        position = PositionState(
            ticket=1,
            symbol="XAUUSD",
            side="BUY",
            volume=0.10,
            entry_price=2000.0,
            stop_loss=1990.0,
            magic_number=555,
            partial_closed=False,
            breakeven_set=False,
        )
        actions = evaluate_partial_close_and_breakeven(
            position,
            current_price=2010.0,
            atr_value=5.0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
        )
        assert len(actions) == 2
        close_action, breakeven_action = actions
        assert close_action.action == "TRADE_ACTION_DEAL"
        assert close_action.volume == 0.05
        assert breakeven_action.action == "TRADE_ACTION_SLTP"
        assert breakeven_action.stop_loss == 2000.0

    def test_already_partial_closed_yields_no_action(self) -> None:
        position = PositionState(
            ticket=1,
            symbol="XAUUSD",
            side="BUY",
            volume=0.05,
            entry_price=2000.0,
            stop_loss=2000.0,
            magic_number=555,
            partial_closed=True,
            breakeven_set=True,
        )
        actions = evaluate_partial_close_and_breakeven(
            position,
            current_price=2050.0,
            atr_value=5.0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
        )
        assert actions == []

    def test_trailing_stop_inactive_before_breakeven(self) -> None:
        position = PositionState(
            ticket=1,
            symbol="XAUUSD",
            side="BUY",
            volume=0.05,
            entry_price=2000.0,
            stop_loss=1990.0,
            magic_number=555,
            partial_closed=True,
            breakeven_set=False,
        )
        assert calculate_trailing_stop(position, current_price=2050.0, atr_value=5.0) is None

    def test_trailing_stop_tightens_for_buy(self) -> None:
        position = PositionState(
            ticket=1,
            symbol="XAUUSD",
            side="BUY",
            volume=0.05,
            entry_price=2000.0,
            stop_loss=2000.0,
            magic_number=555,
            partial_closed=True,
            breakeven_set=True,
        )
        action = calculate_trailing_stop(position, current_price=2020.0, atr_value=5.0)
        assert action is not None
        assert action.stop_loss == 2012.5

    def test_trailing_stop_never_loosens(self) -> None:
        position = PositionState(
            ticket=1,
            symbol="XAUUSD",
            side="BUY",
            volume=0.05,
            entry_price=2000.0,
            stop_loss=2012.5,
            magic_number=555,
            partial_closed=True,
            breakeven_set=True,
        )
        assert calculate_trailing_stop(position, current_price=2015.0, atr_value=5.0) is None

    def test_trailing_stop_tightens_for_sell(self) -> None:
        position = PositionState(
            ticket=1,
            symbol="XAUUSD",
            side="SELL",
            volume=0.05,
            entry_price=2000.0,
            stop_loss=2000.0,
            magic_number=555,
            partial_closed=True,
            breakeven_set=True,
        )
        action = calculate_trailing_stop(position, current_price=1980.0, atr_value=5.0)
        assert action is not None
        assert action.stop_loss == 1987.5


# ---------------------------------------------------------------------------
# broker/mt5_gateway.py (pure-logic parts only; MT5 connection paths are
# integration-level, see test_integration.py)
# ---------------------------------------------------------------------------


class TestBrokerPureLogic:
    def test_resolve_gold_symbol_priority_and_visibility_fallback(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_mt5.symbols["XAUUSD.m"] = FakeSymbolInfo(
            "XAUUSD.m", visible=False, point=0.01, tick_value=1.0
        )
        fake_mt5.symbols["GOLD"] = FakeSymbolInfo("GOLD", visible=True, point=0.1, tick_value=10.0)
        monkeypatch.setattr(gw, "mt5", fake_mt5)

        spec = gw.resolve_gold_symbol()
        assert spec.name == "XAUUSD.m"
        assert ("XAUUSD.m", True) in fake_mt5.select_calls

    def test_resolve_gold_symbol_no_candidate_raises(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        with pytest.raises(gw.BrokerSymbolUnavailableError):
            gw.resolve_gold_symbol()

    def test_execution_window_boundaries(self) -> None:
        assert (
            gw.is_within_execution_window(datetime(2026, 7, 4, 6, 59, tzinfo=timezone.utc)) is False
        )
        assert (
            gw.is_within_execution_window(datetime(2026, 7, 4, 7, 0, tzinfo=timezone.utc)) is True
        )
        assert (
            gw.is_within_execution_window(datetime(2026, 7, 4, 21, 59, tzinfo=timezone.utc)) is True
        )
        assert (
            gw.is_within_execution_window(datetime(2026, 7, 4, 22, 0, tzinfo=timezone.utc)) is False
        )

    def test_execution_window_naive_datetime_raises(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            gw.is_within_execution_window(datetime(2026, 7, 4, 12, 0))

    def test_is_ticket_still_open_true_when_broker_confirms(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        fake_mt5.positions[555] = FakePosition(555, "XAUUSD", fake_mt5.POSITION_TYPE_BUY, 1)
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=1)
        assert gateway.is_ticket_still_open(555) is True

    def test_is_ticket_still_open_false_when_broker_has_no_record(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        gateway = gw.MT5Gateway(login=1, password="x", server="y", magic_number=1)
        assert gateway.is_ticket_still_open(999) is False

    def test_weekend_market_closed_friday_boundary(self) -> None:
        # 2026-07-03 is a Friday.
        assert (
            gw.is_weekend_market_closed(datetime(2026, 7, 3, 21, 59, tzinfo=timezone.utc)) is False
        )
        assert gw.is_weekend_market_closed(datetime(2026, 7, 3, 22, 0, tzinfo=timezone.utc)) is True
        assert (
            gw.is_weekend_market_closed(datetime(2026, 7, 3, 23, 59, tzinfo=timezone.utc)) is True
        )

    def test_weekend_market_closed_all_saturday(self) -> None:
        # 2026-07-04 is a Saturday.
        assert gw.is_weekend_market_closed(datetime(2026, 7, 4, 0, 0, tzinfo=timezone.utc)) is True
        assert gw.is_weekend_market_closed(datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)) is True
        assert (
            gw.is_weekend_market_closed(datetime(2026, 7, 4, 23, 59, tzinfo=timezone.utc)) is True
        )

    def test_weekend_market_closed_sunday_boundary(self) -> None:
        # 2026-07-05 is a Sunday.
        assert gw.is_weekend_market_closed(datetime(2026, 7, 5, 0, 0, tzinfo=timezone.utc)) is True
        assert (
            gw.is_weekend_market_closed(datetime(2026, 7, 5, 21, 59, tzinfo=timezone.utc)) is True
        )
        assert (
            gw.is_weekend_market_closed(datetime(2026, 7, 5, 22, 0, tzinfo=timezone.utc)) is False
        )

    def test_weekend_market_closed_false_on_a_weekday(self) -> None:
        # 2026-07-01 is a Wednesday.
        assert gw.is_weekend_market_closed(datetime(2026, 7, 1, 3, 0, tzinfo=timezone.utc)) is False
        assert (
            gw.is_weekend_market_closed(datetime(2026, 7, 1, 23, 0, tzinfo=timezone.utc)) is False
        )

    def test_weekend_market_closed_naive_datetime_raises(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            gw.is_weekend_market_closed(datetime(2026, 7, 4, 12, 0))


# ---------------------------------------------------------------------------
# broker/clock_provider.py (docs/PRODUCTION_SPEC.md §3)
# ---------------------------------------------------------------------------


class TestMT5ClockProvider:
    def test_get_server_time_derives_from_broker_utc_offset(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(
            time_=int(datetime(2026, 7, 4, 15, 0, tzinfo=timezone.utc).timestamp())
        )
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        gateway = gw.MT5Gateway(login=1, password="x", server="s", magic_number=1)
        gateway.connect()

        provider = cp.MT5ClockProvider(gateway=gateway)
        before = datetime.now(timezone.utc)
        server_time = provider.get_server_time("XAUUSD")
        after = datetime.now(timezone.utc)

        # `server_time` = (some instant between `before` and `after`) +
        # `broker_utc_offset`; ordering is preserved by adding the same
        # constant offset to all three, so this holds without freezing time.
        assert before + gateway.broker_utc_offset <= server_time
        assert server_time <= after + gateway.broker_utc_offset

    def test_symbol_mismatch_raises(
        self, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_mt5.symbols["XAUUSD"] = FakeSymbolInfo("XAUUSD", visible=True)
        fake_mt5.ticks["XAUUSD"] = FakeTick(time_=1)
        monkeypatch.setattr(gw, "mt5", fake_mt5)
        gateway = gw.MT5Gateway(login=1, password="x", server="s", magic_number=1)
        gateway.connect()

        provider = cp.MT5ClockProvider(gateway=gateway)
        with pytest.raises(ValueError, match="bound to 'XAUUSD'"):
            provider.get_server_time("EURUSD")


# ---------------------------------------------------------------------------
# news/news_engine.py (pure-logic parts; fetch_calendar_events is
# integration-level, see test_integration.py)
# ---------------------------------------------------------------------------


class TestNewsEnginePureLogic:
    def test_is_core_macro_event_classification(self) -> None:
        def make(title: str) -> EconomicEvent:
            return EconomicEvent(
                title=title,
                country="US",
                impact="High",
                scheduled_at_utc=datetime(2026, 7, 4, tzinfo=timezone.utc),
            )

        assert make("Non-Farm Payrolls").is_core_macro_event is True
        assert make("NFP Employment Change").is_core_macro_event is True
        assert make("CPI y/y").is_core_macro_event is True
        assert make("FOMC Statement").is_core_macro_event is True
        assert make("Retail Sales m/m").is_core_macro_event is False

    def test_trade_entry_locked_boundary(self) -> None:
        nfp_event = EconomicEvent(
            title="Non-Farm Payrolls",
            country="US",
            impact="High",
            scheduled_at_utc=datetime(2026, 7, 4, 12, 30, tzinfo=timezone.utc),
        )
        assert (
            is_trade_entry_locked(datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc), [nfp_event])
            is True
        )  # exactly 30 min before
        assert (
            is_trade_entry_locked(datetime(2026, 7, 4, 13, 0, tzinfo=timezone.utc), [nfp_event])
            is True
        )  # exactly 30 min after
        assert (
            is_trade_entry_locked(datetime(2026, 7, 4, 11, 59, tzinfo=timezone.utc), [nfp_event])
            is False
        )
        assert (
            is_trade_entry_locked(datetime(2026, 7, 4, 13, 1, tzinfo=timezone.utc), [nfp_event])
            is False
        )

    def test_non_macro_event_never_locks(self) -> None:
        other_event = EconomicEvent(
            title="Retail Sales m/m",
            country="US",
            impact="Medium",
            scheduled_at_utc=datetime(2026, 7, 4, 12, 30, tzinfo=timezone.utc),
        )
        assert (
            is_trade_entry_locked(datetime(2026, 7, 4, 12, 30, tzinfo=timezone.utc), [other_event])
            is False
        )

    def test_naive_now_utc_raises(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            is_trade_entry_locked(datetime(2026, 7, 4, 12, 30), [])

    def test_fail_safe_healthy_feed_unchanged(self) -> None:
        healthy = NewsFeedHealthState(
            is_healthy=True, last_successful_fetch_utc=None, last_error=None
        )
        assert apply_news_feed_fail_safe(0.10, 30.0, healthy) == (0.10, 30.0)

    def test_fail_safe_unhealthy_feed_adjusts(self) -> None:
        unhealthy = NewsFeedHealthState(
            is_healthy=False, last_successful_fetch_utc=None, last_error="timeout"
        )
        risk, spread = apply_news_feed_fail_safe(0.10, 30.0, unhealthy)
        assert risk == 0.05
        assert spread == 60.0


# ---------------------------------------------------------------------------
# news/calendar_provider.py (docs/PRODUCTION_SPEC.md §2; NetworkCalendarProvider's
# HTTP path is covered via fetch_calendar_events in test_integration.py)
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_allows_up_to_the_limit_then_denies(self) -> None:
        limiter = calp.RateLimiter(max_calls_per_minute=2)
        assert limiter.allow(now=0.0) is True
        assert limiter.allow(now=1.0) is True
        assert limiter.allow(now=2.0) is False

    def test_sliding_window_expires_old_calls(self) -> None:
        limiter = calp.RateLimiter(max_calls_per_minute=1)
        assert limiter.allow(now=0.0) is True
        assert limiter.allow(now=30.0) is False
        assert limiter.allow(now=60.1) is True

    def test_invalid_limit_raises(self) -> None:
        with pytest.raises(ValueError, match="max_calls_per_minute"):
            calp.RateLimiter(max_calls_per_minute=0)


class TestOfflineSnapshotCalendarProvider:
    FROM_UTC = datetime(2026, 7, 4, tzinfo=timezone.utc)
    TO_UTC = datetime(2026, 7, 11, tzinfo=timezone.utc)

    def test_reads_and_filters_snapshot(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot.json"
        snapshot.write_text(
            '[{"title": "NFP", "country": "US", "impact": "High", '
            '"date": "2026-07-04T12:30:00+00:00"}, '
            '{"title": "Old Event", "country": "US", "impact": "Low", '
            '"date": "2026-06-01T00:00:00+00:00"}]',
            encoding="utf-8",
        )
        provider = calp.OfflineSnapshotCalendarProvider(snapshot_path=snapshot)
        events = provider.fetch_events(self.FROM_UTC, self.TO_UTC)
        assert len(events) == 1
        assert events[0].title == "NFP"
        assert provider.name == "offline_snapshot"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        provider = calp.OfflineSnapshotCalendarProvider(snapshot_path=tmp_path / "missing.json")
        with pytest.raises(NewsFeedConnectionError, match="not found"):
            provider.fetch_events(self.FROM_UTC, self.TO_UTC)

    def test_malformed_json_raises(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "bad.json"
        snapshot.write_text("{not valid json", encoding="utf-8")
        provider = calp.OfflineSnapshotCalendarProvider(snapshot_path=snapshot)
        with pytest.raises(NewsFeedConnectionError, match="unreadable"):
            provider.fetch_events(self.FROM_UTC, self.TO_UTC)


class _FakeCalendarProvider:
    def __init__(self, name: str, events: list[EconomicEvent] | None = None) -> None:
        self.name = name
        self._events = events
        self.call_count = 0

    def fetch_events(self, from_utc: datetime, to_utc: datetime) -> list[EconomicEvent]:
        self.call_count += 1
        if self._events is None:
            raise NewsFeedConnectionError(f"{self.name} is down")
        return self._events


class TestCalendarProviderChain:
    FROM_UTC = datetime(2026, 7, 4, tzinfo=timezone.utc)
    TO_UTC = datetime(2026, 7, 11, tzinfo=timezone.utc)

    def _event(self, title: str) -> EconomicEvent:
        return EconomicEvent(
            title=title, country="US", impact="High", scheduled_at_utc=self.FROM_UTC
        )

    def test_falls_through_to_next_provider_on_failure(self) -> None:
        primary = _FakeCalendarProvider("primary", events=None)
        fallback = _FakeCalendarProvider("fallback", events=[self._event("NFP")])
        chain = calp.CalendarProviderChain(providers=[primary, fallback])
        events = chain.fetch_events(self.FROM_UTC, self.TO_UTC)
        assert [e.title for e in events] == ["NFP"]
        assert primary.call_count == 1
        assert fallback.call_count == 1

    def test_exhausted_rate_limiter_skips_to_next_provider(self) -> None:
        primary = _FakeCalendarProvider("primary", events=[self._event("should not be used")])
        fallback = _FakeCalendarProvider("fallback", events=[self._event("NFP")])
        denying_limiter = calp.RateLimiter(max_calls_per_minute=1)
        # Consume the only slot using the real clock (no injected `now`), so
        # it's within the same 60s window `CalendarProviderChain.fetch_events()`
        # checks against internally (it never passes an explicit `now`).
        denying_limiter.allow()
        chain = calp.CalendarProviderChain(
            providers=[primary, fallback],
            rate_limiters={"primary": denying_limiter},
        )
        events = chain.fetch_events(self.FROM_UTC, self.TO_UTC)
        assert [e.title for e in events] == ["NFP"]
        assert primary.call_count == 0

    def test_all_providers_exhausted_raises_with_all_reasons(self) -> None:
        first = _FakeCalendarProvider("first", events=None)
        second = _FakeCalendarProvider("second", events=None)
        chain = calp.CalendarProviderChain(providers=[first, second])
        with pytest.raises(NewsFeedConnectionError) as exc_info:
            chain.fetch_events(self.FROM_UTC, self.TO_UTC)
        assert "first is down" in str(exc_info.value)
        assert "second is down" in str(exc_info.value)


class TestBuildCalendarProviderChain:
    def test_default_config_yields_offline_only_chain(self) -> None:
        config = CalendarConfig(
            provider_priority=("offline_snapshot",),
            timeout_ms=3000,
            rate_limit_per_min=60,
            provider_base_urls={},
            offline_snapshot_path=Path("news/offline_calendar_snapshot.json"),
        )
        chain = calp.build_calendar_provider_chain(config, api_key="unused")
        assert len(chain.providers) == 1
        assert isinstance(chain.providers[0], calp.OfflineSnapshotCalendarProvider)
        assert chain.rate_limiters == {}

    def test_network_providers_get_rate_limiters_and_base_urls(self, tmp_path: Path) -> None:
        config = CalendarConfig(
            provider_priority=("finnhub", "offline_snapshot"),
            timeout_ms=1500,
            rate_limit_per_min=10,
            provider_base_urls={"finnhub": "https://finnhub.example.com/cal"},
            offline_snapshot_path=tmp_path / "snapshot.json",
        )
        chain = calp.build_calendar_provider_chain(config, api_key="key123")
        assert [p.name for p in chain.providers] == ["finnhub", "offline_snapshot"]
        finnhub_provider = chain.providers[0]
        assert isinstance(finnhub_provider, calp.NetworkCalendarProvider)
        assert finnhub_provider.base_url == "https://finnhub.example.com/cal"
        assert finnhub_provider.api_key == "key123"
        assert finnhub_provider.read_timeout_seconds == 1.5
        assert "finnhub" in chain.rate_limiters
        assert chain.rate_limiters["finnhub"].max_calls_per_minute == 10


# ---------------------------------------------------------------------------
# optimizer/self_learning.py (pure-logic parts; the real-database isolation
# test lives in test_integration.py)
# ---------------------------------------------------------------------------


class TestSelfLearningPureLogic:
    def test_is_market_closed_only_on_saturday(self) -> None:
        assert (
            is_market_closed_for_optimization(datetime(2026, 7, 4, 3, 0, tzinfo=timezone.utc))
            is True
        )
        assert (
            is_market_closed_for_optimization(datetime(2026, 7, 3, 23, 59, tzinfo=timezone.utc))
            is False
        )
        assert (
            is_market_closed_for_optimization(datetime(2026, 7, 5, 0, 0, tzinfo=timezone.utc))
            is False
        )

    def test_naive_datetime_raises(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            is_market_closed_for_optimization(datetime(2026, 7, 4))

    def _closed_trade(self, profit: float, ticket: int) -> TradeLedgerEntry:
        return TradeLedgerEntry(
            client_order_id=f"co-{ticket}",
            symbol="XAUUSD",
            side="BUY",
            volume_lots=0.1,
            status="CLOSED",
            opened_at_utc="2026-07-01T00:00:00Z",
            profit=profit,
            closed_at_utc="2026-07-01T01:00:00Z",
        )

    def test_compute_ledger_metrics_empty(self) -> None:
        metrics = compute_ledger_metrics([])
        assert metrics.trade_count == 0
        assert metrics.win_rate == 0.0

    def test_compute_ledger_metrics_mixed(self) -> None:
        trades = [self._closed_trade(p, i) for i, p in enumerate([10.0, -5.0, 20.0, -15.0])]
        metrics = compute_ledger_metrics(trades)
        assert metrics.trade_count == 4
        assert metrics.win_rate == 0.5
        assert abs(metrics.profit_factor - 1.5) < 1e-9

    def test_compute_ledger_metrics_all_wins_infinite_profit_factor(self) -> None:
        trades = [self._closed_trade(p, i) for i, p in enumerate([10.0, 20.0])]
        metrics = compute_ledger_metrics(trades)
        assert metrics.profit_factor == float("inf")

    def test_compute_ledger_metrics_all_losses_zero_profit_factor(self) -> None:
        trades = [self._closed_trade(p, i) for i, p in enumerate([-10.0, -20.0])]
        metrics = compute_ledger_metrics(trades)
        assert metrics.profit_factor == 0.0

    @pytest.fixture
    def tunable_parameters(self) -> dict[str, TunableParameter]:
        return {
            "ADX_TREND_THRESHOLD": TunableParameter("ADX_TREND_THRESHOLD", 25.0, 20.0, 35.0, 1.0),
            "TRAILING_ATR_MULTIPLIER": TunableParameter(
                "TRAILING_ATR_MULTIPLIER", 1.5, 1.0, 3.0, 0.25
            ),
        }

    def test_decide_shift_below_min_trades_no_change(
        self, tunable_parameters: dict[str, TunableParameter]
    ) -> None:
        metrics = LedgerPerformanceMetrics(
            trade_count=5, win_rate=0.2, profit_factor=0.5, total_profit=-10.0
        )
        assert decide_parameter_shift(tunable_parameters, metrics) is None

    def test_decide_shift_low_win_rate_tightens_adx(
        self, tunable_parameters: dict[str, TunableParameter]
    ) -> None:
        metrics = LedgerPerformanceMetrics(
            trade_count=20, win_rate=0.30, profit_factor=1.2, total_profit=50.0
        )
        decision = decide_parameter_shift(tunable_parameters, metrics)
        assert decision is not None
        assert decision.parameter_name == "ADX_TREND_THRESHOLD"
        assert decision.new_value == 26.0

    def test_decide_shift_low_profit_factor_widens_trailing(
        self, tunable_parameters: dict[str, TunableParameter]
    ) -> None:
        metrics = LedgerPerformanceMetrics(
            trade_count=20, win_rate=0.55, profit_factor=0.8, total_profit=-5.0
        )
        decision = decide_parameter_shift(tunable_parameters, metrics)
        assert decision is not None
        assert decision.parameter_name == "TRAILING_ATR_MULTIPLIER"
        assert abs(decision.new_value - 1.75) < 1e-9

    def test_decide_shift_healthy_metrics_no_change(
        self, tunable_parameters: dict[str, TunableParameter]
    ) -> None:
        metrics = LedgerPerformanceMetrics(
            trade_count=20, win_rate=0.60, profit_factor=1.8, total_profit=100.0
        )
        assert decide_parameter_shift(tunable_parameters, metrics) is None

    def test_decide_shift_parameter_at_max_no_change(self) -> None:
        maxed = {
            "ADX_TREND_THRESHOLD": TunableParameter("ADX_TREND_THRESHOLD", 35.0, 20.0, 35.0, 1.0)
        }
        metrics = LedgerPerformanceMetrics(
            trade_count=20, win_rate=0.30, profit_factor=1.2, total_profit=50.0
        )
        assert decide_parameter_shift(maxed, metrics) is None

    def test_decide_shift_missing_parameter_no_crash(self) -> None:
        metrics = LedgerPerformanceMetrics(
            trade_count=20, win_rate=0.30, profit_factor=1.2, total_profit=50.0
        )
        assert decide_parameter_shift({}, metrics) is None

    def test_bootstrap_invalid_iterations_raises(self) -> None:
        with pytest.raises(ValueError, match="iterations must be"):
            run_monte_carlo_bootstrap([1.0], iterations=0)

    def test_bootstrap_empty_profits_raises(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            run_monte_carlo_bootstrap([])

    def test_bootstrap_reproducible_with_seed(self) -> None:
        profits = [10.0, -5.0, 20.0, -15.0, 8.0, -3.0, 12.0]
        result_a = run_monte_carlo_bootstrap(profits, iterations=1000, rng=random.Random(42))
        result_b = run_monte_carlo_bootstrap(profits, iterations=1000, rng=random.Random(42))
        assert result_a == result_b

    def test_bootstrap_all_positive_fully_profitable(self) -> None:
        result = run_monte_carlo_bootstrap([10.0, 5.0, 8.0], iterations=1000, rng=random.Random(1))
        assert result.fraction_profitable == 1.0
        assert result.bootstrap_p05_final_pnl > 0

    def test_bootstrap_all_negative_never_profitable(self) -> None:
        result = run_monte_carlo_bootstrap(
            [-10.0, -5.0, -8.0], iterations=1000, rng=random.Random(1)
        )
        assert result.fraction_profitable == 0.0

    def test_bootstrap_default_iteration_count(self) -> None:
        result = run_monte_carlo_bootstrap([1.0, 2.0], rng=random.Random(1))
        assert result.iterations == 1000


# ---------------------------------------------------------------------------
# main.py (Phase 10 orchestration: bar-close cadence, processing cap,
# drawdown breakers, entry-signal combination)
# ---------------------------------------------------------------------------


class TestBarCloseCadence:
    def test_seconds_until_next_m5_close(self) -> None:
        now = datetime(2026, 7, 4, 12, 3, 30, tzinfo=timezone.utc)
        assert orchestrator.seconds_until_next_bar_close(now, 5) == 90.0

    def test_exactly_on_boundary_returns_zero(self) -> None:
        now = datetime(2026, 7, 4, 12, 5, 0, tzinfo=timezone.utc)
        assert orchestrator.seconds_until_next_bar_close(now, 5) == 0.0

    def test_naive_datetime_raises(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            orchestrator.seconds_until_next_bar_close(datetime(2026, 7, 4, 12, 3, 30))


class TestSubmitWithPreFlightLedger:
    """`orchestrator.submit_with_pre_flight_ledger()` (Phase 11c,
    docs/PRODUCTION_SPEC.md §4): the pre-flight idempotency write wrapped
    around `main.py`'s two real broker-submission call sites. Exercised
    against a real temp-file StateManager (so the ledger writes are real
    SQLite, not mocked) with a fake `submit` callable standing in for the
    broker call — no MT5 terminal needed, so this belongs in test_unit.py
    rather than test_integration.py."""

    def test_success_path_records_requested_sent_and_terminal_state(
        self, state_manager: StateManager
    ) -> None:
        def fake_submit(client_order_id: str) -> str:
            assert client_order_id  # a real UUIDv4 string was generated
            return "submitted"

        client_order_id, result = orchestrator.submit_with_pre_flight_ledger(
            state_manager, fake_submit, {"symbol": "XAUUSD"}, OrderLifecycleState.FILLED
        )

        assert result == "submitted"
        events = state_manager.get_order_events(client_order_id)
        assert [e.event_type for e in events] == [
            OrderLifecycleState.REQUESTED,
            OrderLifecycleState.SENT,
            OrderLifecycleState.FILLED,
        ]
        assert state_manager.get_order_ledger_state(client_order_id) == "FILLED"

    def test_rejection_records_requested_then_rejected_and_reraises(
        self, state_manager: StateManager
    ) -> None:
        def failing_submit(client_order_id: str) -> str:
            raise gw.BrokerOrderRejectedError("no tick available")

        captured_id: str | None = None

        def fake_submit_wrapper(client_order_id: str) -> str:
            nonlocal captured_id
            captured_id = client_order_id
            return failing_submit(client_order_id)

        with pytest.raises(gw.BrokerOrderRejectedError, match="no tick available"):
            orchestrator.submit_with_pre_flight_ledger(
                state_manager, fake_submit_wrapper, {"symbol": "XAUUSD"}, OrderLifecycleState.FILLED
            )

        assert captured_id is not None
        events = state_manager.get_order_events(captured_id)
        assert [e.event_type for e in events] == [
            OrderLifecycleState.REQUESTED,
            OrderLifecycleState.REJECTED,
        ]
        assert events[1].metadata["error"] == "no tick available"
        assert state_manager.get_order_ledger_state(captured_id) == "REJECTED"


class TestProcessingCap:
    def test_under_cap(self) -> None:
        result = orchestrator.evaluate_processing_time(0.05)
        assert result.duration_ms == 50.0
        assert result.exceeded_cap is False

    def test_over_cap(self) -> None:
        result = orchestrator.evaluate_processing_time(0.25)
        assert result.duration_ms == 250.0
        assert result.exceeded_cap is True

    def test_negative_duration_raises(self) -> None:
        with pytest.raises(ValueError, match="must be >= 0"):
            orchestrator.evaluate_processing_time(-0.01)


class TestClassifyDrawdownEvent:
    """`risk/drawdown_fsm.py`'s numeric-to-symbolic boundary
    (docs/PRODUCTION_SPEC.md §6). All fixture baselines are equal
    (10,000 each) unless a test needs to isolate one tier, in which case
    the other two are set to `current_equity` (0% drawdown) so only the
    tier under test can drive the classification."""

    @pytest.fixture
    def baselines(self) -> EquityBaselines:
        return EquityBaselines(
            daily_start_equity=10_000.0,
            weekly_start_equity=10_000.0,
            monthly_start_equity=10_000.0,
        )

    def test_within_tolerance(self, baselines: EquityBaselines) -> None:
        result = classify_drawdown_event(9_900.0, baselines)  # 1% down
        assert result.event == DrawdownEvent.WITHIN_TOLERANCE

    def test_warning_threshold(self, baselines: EquityBaselines) -> None:
        # WARNING_RATIO_OF_SOFT_LOCK (0.6) * daily SOFT_LOCK (0.05) = 3%.
        result = classify_drawdown_event(9_700.0, baselines)  # 3% down
        assert result.event == DrawdownEvent.WARNING_THRESHOLD_BREACHED

    def test_soft_lock_threshold_daily(self, baselines: EquityBaselines) -> None:
        result = classify_drawdown_event(9_500.0, baselines)  # 5% down
        assert result.event == DrawdownEvent.SOFT_LOCK_THRESHOLD_BREACHED

    def test_soft_lock_threshold_weekly_isolated(self) -> None:
        baselines = EquityBaselines(
            daily_start_equity=9_000.0, weekly_start_equity=10_000.0, monthly_start_equity=9_000.0
        )
        result = classify_drawdown_event(9_000.0, baselines)  # weekly: 10% down, others: 0%
        assert result.event == DrawdownEvent.SOFT_LOCK_THRESHOLD_BREACHED
        assert result.weekly_drawdown_pct == pytest.approx(0.10)

    def test_soft_lock_threshold_monthly_isolated(self) -> None:
        baselines = EquityBaselines(
            daily_start_equity=8_000.0, weekly_start_equity=8_000.0, monthly_start_equity=10_000.0
        )
        result = classify_drawdown_event(8_000.0, baselines)  # monthly: 20% down, others: 0%
        assert result.event == DrawdownEvent.SOFT_LOCK_THRESHOLD_BREACHED
        assert result.monthly_drawdown_pct == pytest.approx(0.20)

    def test_hard_lock_threshold_daily(self, baselines: EquityBaselines) -> None:
        result = classify_drawdown_event(9_000.0, baselines)  # 10% down (uniform baselines)
        assert result.event == DrawdownEvent.HARD_LOCK_THRESHOLD_BREACHED

    def test_invalid_baseline_raises(self) -> None:
        bad_baselines = EquityBaselines(0.0, 10_000.0, 10_000.0)
        with pytest.raises(ValueError, match="daily_start_equity"):
            classify_drawdown_event(9_000.0, bad_baselines)

    def test_negative_equity_raises(self, baselines: EquityBaselines) -> None:
        with pytest.raises(ValueError, match="current_equity"):
            classify_drawdown_event(-1.0, baselines)


class TestEquityBaselineRollover:
    """`seed_equity_baselines()`/`roll_equity_baselines()`
    (docs/ARCHITECTURE_SUMMARY.md §5's equity-baseline rollover gap):
    `main()` re-seeds each tier independently the first bar-close cycle
    that crosses its UTC-day/ISO-week/calendar-month boundary. Fixture
    dates: 2026-07-01 (Wed) / 07-02 (Thu) share both ISO week and month;
    2026-07-05 (Sun) / 07-06 (Mon) share month but cross an ISO week
    boundary; 2026-07-31 (Fri) / 2026-08-01 (Sat) share an ISO week but
    cross a month boundary.
    """

    def test_seed_sets_all_three_tiers_to_current_equity(self) -> None:
        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        baselines, epoch = seed_equity_baselines(10_000.0, now)
        assert baselines == EquityBaselines(10_000.0, 10_000.0, 10_000.0)
        assert epoch == BaselineEpoch(
            daily_date=now.date(),
            weekly_iso_year_week=now.isocalendar()[:2],
            monthly_year_month=(2026, 7),
        )

    def test_seed_naive_datetime_raises(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            seed_equity_baselines(10_000.0, datetime(2026, 7, 1, 12, 0))

    def test_roll_no_boundary_crossed_is_unchanged(self) -> None:
        baselines, epoch = seed_equity_baselines(
            10_000.0, datetime(2026, 7, 1, 0, 5, tzinfo=timezone.utc)
        )
        later_same_day = datetime(2026, 7, 1, 23, 0, tzinfo=timezone.utc)
        rolled, new_epoch = roll_equity_baselines(baselines, epoch, 9_000.0, later_same_day)
        # A same-period equity dip must NOT reset the baseline.
        assert rolled == baselines
        assert new_epoch == epoch

    def test_roll_daily_boundary_only(self) -> None:
        baselines, epoch = seed_equity_baselines(
            10_000.0, datetime(2026, 7, 1, 23, 0, tzinfo=timezone.utc)
        )
        next_day = datetime(2026, 7, 2, 0, 5, tzinfo=timezone.utc)
        rolled, new_epoch = roll_equity_baselines(baselines, epoch, 9_500.0, next_day)
        assert rolled.daily_start_equity == 9_500.0
        assert rolled.weekly_start_equity == 10_000.0
        assert rolled.monthly_start_equity == 10_000.0
        assert new_epoch.daily_date == next_day.date()
        assert new_epoch.weekly_iso_year_week == epoch.weekly_iso_year_week
        assert new_epoch.monthly_year_month == epoch.monthly_year_month

    def test_roll_weekly_boundary_rolls_daily_too_monthly_unchanged(self) -> None:
        baselines, epoch = seed_equity_baselines(
            10_000.0, datetime(2026, 7, 5, 23, 0, tzinfo=timezone.utc)
        )
        monday = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)
        rolled, new_epoch = roll_equity_baselines(baselines, epoch, 9_200.0, monday)
        assert rolled.daily_start_equity == 9_200.0
        assert rolled.weekly_start_equity == 9_200.0
        assert rolled.monthly_start_equity == 10_000.0
        assert new_epoch.weekly_iso_year_week != epoch.weekly_iso_year_week
        assert new_epoch.monthly_year_month == epoch.monthly_year_month

    def test_roll_monthly_boundary_rolls_daily_too_weekly_unchanged(self) -> None:
        baselines, epoch = seed_equity_baselines(
            10_000.0, datetime(2026, 7, 31, 23, 0, tzinfo=timezone.utc)
        )
        aug_1 = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)
        rolled, new_epoch = roll_equity_baselines(baselines, epoch, 8_800.0, aug_1)
        assert rolled.daily_start_equity == 8_800.0
        assert rolled.weekly_start_equity == 10_000.0
        assert rolled.monthly_start_equity == 8_800.0
        assert new_epoch.weekly_iso_year_week == epoch.weekly_iso_year_week
        assert new_epoch.monthly_year_month != epoch.monthly_year_month

    def test_roll_naive_datetime_raises(self) -> None:
        baselines, epoch = seed_equity_baselines(
            10_000.0, datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        )
        with pytest.raises(ValueError, match="timezone-aware"):
            roll_equity_baselines(baselines, epoch, 9_000.0, datetime(2026, 7, 2, 12, 0))


class TestTransitionDrawdownState:
    """`risk/drawdown_fsm.py`'s pure FSM reducer: `(current_state, event)
    -> new_state` (docs/PRODUCTION_SPEC.md §6)."""

    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            (DrawdownEvent.WITHIN_TOLERANCE, DrawdownState.ACTIVE),
            (DrawdownEvent.WARNING_THRESHOLD_BREACHED, DrawdownState.WARNING),
            (DrawdownEvent.SOFT_LOCK_THRESHOLD_BREACHED, DrawdownState.SOFT_LOCK),
            (DrawdownEvent.HARD_LOCK_THRESHOLD_BREACHED, DrawdownState.HARD_LOCK),
        ],
    )
    def test_from_active(self, event: DrawdownEvent, expected: DrawdownState) -> None:
        assert transition_drawdown_state(DrawdownState.ACTIVE, event) == expected

    def test_soft_lock_recovers_to_active_on_within_tolerance(self) -> None:
        result = transition_drawdown_state(DrawdownState.SOFT_LOCK, DrawdownEvent.WITHIN_TOLERANCE)
        assert result == DrawdownState.ACTIVE

    def test_warning_recovers_to_active_on_within_tolerance(self) -> None:
        result = transition_drawdown_state(DrawdownState.WARNING, DrawdownEvent.WITHIN_TOLERANCE)
        assert result == DrawdownState.ACTIVE

    def test_soft_lock_escalates_to_hard_lock(self) -> None:
        result = transition_drawdown_state(
            DrawdownState.SOFT_LOCK, DrawdownEvent.HARD_LOCK_THRESHOLD_BREACHED
        )
        assert result == DrawdownState.HARD_LOCK

    @pytest.mark.parametrize(
        "event",
        [e for e in DrawdownEvent if e != DrawdownEvent.MANUAL_RESET_CONFIRMED],
    )
    def test_hard_lock_always_advances_to_manual_reset_required(self, event: DrawdownEvent) -> None:
        result = transition_drawdown_state(DrawdownState.HARD_LOCK, event)
        assert result == DrawdownState.MANUAL_RESET_REQUIRED

    @pytest.mark.parametrize(
        "event",
        [e for e in DrawdownEvent if e != DrawdownEvent.MANUAL_RESET_CONFIRMED],
    )
    def test_manual_reset_required_is_sticky(self, event: DrawdownEvent) -> None:
        result = transition_drawdown_state(DrawdownState.MANUAL_RESET_REQUIRED, event)
        assert result == DrawdownState.MANUAL_RESET_REQUIRED

    @pytest.mark.parametrize("current_state", list(DrawdownState))
    def test_manual_reset_confirmed_always_returns_to_active(
        self, current_state: DrawdownState
    ) -> None:
        result = transition_drawdown_state(current_state, DrawdownEvent.MANUAL_RESET_CONFIRMED)
        assert result == DrawdownState.ACTIVE


class TestDrawdownStatePredicates:
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (DrawdownState.ACTIVE, False),
            (DrawdownState.WARNING, False),
            (DrawdownState.SOFT_LOCK, True),
            (DrawdownState.HARD_LOCK, True),
            (DrawdownState.MANUAL_RESET_REQUIRED, True),
        ],
    )
    def test_blocks_new_entries(self, state: DrawdownState, expected: bool) -> None:
        assert blocks_new_entries(state) is expected

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (DrawdownState.ACTIVE, False),
            (DrawdownState.WARNING, False),
            (DrawdownState.SOFT_LOCK, False),
            (DrawdownState.HARD_LOCK, True),
            (DrawdownState.MANUAL_RESET_REQUIRED, True),
        ],
    )
    def test_blocks_position_management(self, state: DrawdownState, expected: bool) -> None:
        assert blocks_position_management(state) is expected


class TestDecideHardLockResponse:
    @pytest.mark.parametrize("state", [s for s in DrawdownState if s != DrawdownState.HARD_LOCK])
    def test_none_when_not_freshly_entering_hard_lock(self, state: DrawdownState) -> None:
        assert decide_hard_lock_response(state, liquidate_on_hard_lock=True) is None
        assert decide_hard_lock_response(state, liquidate_on_hard_lock=False) is None

    def test_liquidate_true_yields_should_liquidate(self) -> None:
        response = decide_hard_lock_response(DrawdownState.HARD_LOCK, liquidate_on_hard_lock=True)
        assert response is not None
        assert response.should_liquidate is True

    def test_liquidate_false_yields_freeze(self) -> None:
        response = decide_hard_lock_response(DrawdownState.HARD_LOCK, liquidate_on_hard_lock=False)
        assert response is not None
        assert response.should_liquidate is False


# ---------------------------------------------------------------------------
# config/feature_flags.py (docs/PRODUCTION_SPEC.md §6)
# ---------------------------------------------------------------------------


class TestFeatureFlags:
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FLAG_LIQUIDATE_ON_HARD_LOCK", raising=False)

    def test_default_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        assert FeatureFlags.from_env().liquidate_on_hard_lock is False

    @pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on"])
    def test_truthy_values(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("FLAG_LIQUIDATE_ON_HARD_LOCK", raw)
        assert FeatureFlags.from_env().liquidate_on_hard_lock is True

    @pytest.mark.parametrize("raw", ["false", "FALSE", "0", "no", "off"])
    def test_falsy_values(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("FLAG_LIQUIDATE_ON_HARD_LOCK", raw)
        assert FeatureFlags.from_env().liquidate_on_hard_lock is False

    def test_invalid_value_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("FLAG_LIQUIDATE_ON_HARD_LOCK", "maybe")
        with pytest.raises(ConfigurationError, match="FLAG_LIQUIDATE_ON_HARD_LOCK"):
            FeatureFlags.from_env()


class TestFeatureFlagManager:
    def test_exposes_liquidate_on_hard_lock(self) -> None:
        manager = FeatureFlagManager(FeatureFlags(liquidate_on_hard_lock=True))
        assert manager.liquidate_on_hard_lock is True


# ---------------------------------------------------------------------------
# resilience/backoff.py (docs/PRODUCTION_SPEC.md §7)
# ---------------------------------------------------------------------------


class TestComputeBackoffDelays:
    def test_default_matches_spec_exact_sequence(self) -> None:
        assert backoff.compute_backoff_delays() == (2.0, 4.0, 8.0, 16.0, 32.0)

    def test_custom_max_attempts_and_initial_delay(self) -> None:
        assert backoff.compute_backoff_delays(max_attempts=3, initial_delay_seconds=1.0) == (
            1.0,
            2.0,
            4.0,
        )

    def test_zero_max_attempts_raises(self) -> None:
        with pytest.raises(ValueError, match="max_attempts"):
            backoff.compute_backoff_delays(max_attempts=0)

    def test_non_positive_initial_delay_raises(self) -> None:
        with pytest.raises(ValueError, match="initial_delay_seconds"):
            backoff.compute_backoff_delays(initial_delay_seconds=0.0)


class TestRetryWithBackoff:
    def test_succeeds_on_first_attempt_without_sleeping(self) -> None:
        sleep_calls: list[float] = []
        result = backoff.retry_with_backoff(
            lambda: "ok", sleep=sleep_calls.append, retryable_exceptions=(ValueError,)
        )
        assert result == "ok"
        assert sleep_calls == []

    def test_succeeds_after_transient_failures_with_correct_delays(self) -> None:
        sleep_calls: list[float] = []
        attempts = {"count": 0}

        def flaky() -> str:
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise ValueError("transient")
            return "ok"

        result = backoff.retry_with_backoff(
            flaky,
            max_attempts=5,
            initial_delay_seconds=2.0,
            sleep=sleep_calls.append,
            retryable_exceptions=(ValueError,),
        )
        assert result == "ok"
        assert sleep_calls == [2.0, 4.0]  # two failures before the third (successful) attempt

    def test_exhausts_retry_budget_and_raises_chained(self) -> None:
        sleep_calls: list[float] = []

        def always_fails() -> str:
            raise ValueError("persistent")

        with pytest.raises(backoff.RetryBudgetExhaustedError) as exc_info:
            backoff.retry_with_backoff(
                always_fails,
                max_attempts=2,
                initial_delay_seconds=1.0,
                sleep=sleep_calls.append,
                retryable_exceptions=(ValueError,),
            )
        assert sleep_calls == [1.0, 2.0]  # slept before both retries, none after the last attempt
        assert isinstance(exc_info.value.__cause__, ValueError)

    def test_non_retryable_exception_propagates_immediately(self) -> None:
        sleep_calls: list[float] = []

        def raises_type_error() -> str:
            raise TypeError("not retryable")

        with pytest.raises(TypeError, match="not retryable"):
            backoff.retry_with_backoff(
                raises_type_error, sleep=sleep_calls.append, retryable_exceptions=(ValueError,)
            )
        assert sleep_calls == []


# ---------------------------------------------------------------------------
# storage/db_engine.py's busy_timeout (docs/PRODUCTION_SPEC.md §7) and the
# "SQLite operations are barred from sleep-based retries" static guarantee
# ---------------------------------------------------------------------------


class TestBusyTimeout:
    def test_default_busy_timeout_applied(self, tmp_path: Path) -> None:
        connection = connect(tmp_path / "busy.db")
        try:
            value = connection.execute("PRAGMA busy_timeout;").fetchone()[0]
            assert value == DEFAULT_BUSY_TIMEOUT_MS
        finally:
            connection.close()

    def test_custom_busy_timeout_applied(self, tmp_path: Path) -> None:
        connection = connect(tmp_path / "busy_custom.db", busy_timeout_ms=250)
        try:
            value = connection.execute("PRAGMA busy_timeout;").fetchone()[0]
            assert value == 250
        finally:
            connection.close()


class TestStorageNeverSleeps:
    """Statically enforces docs/PRODUCTION_SPEC.md §7's "SQLite operations
    are barred from sleep-based retries" rule: greps every storage/*.py
    source file for an actual `sleep(...)` call expression (via `ast`, so
    prose mentioning "sleep" in a docstring/comment — like this module's
    own explanatory text — can never produce a false positive). A real
    regression guard, not just a documentation comment — see
    storage/db_engine.py's module docstring."""

    def test_no_sleep_call_anywhere_in_storage_package(self) -> None:
        storage_dir = Path(__file__).resolve().parent.parent.parent / "storage"
        offending: list[str] = []
        for path in glob.glob(str(storage_dir / "*.py")):
            tree = ast.parse(Path(path).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name == "sleep":
                    offending.append(os.path.basename(path))
        assert offending == []


# ---------------------------------------------------------------------------
# storage/state_manager.py's Audit Trail (docs/PRODUCTION_SPEC.md §7)
# ---------------------------------------------------------------------------


class TestAuditTrail:
    def test_record_and_read_back_audit_event(self, state_manager: StateManager) -> None:
        state_manager.record_audit_event(
            actor="operator:jane",
            action_type=AuditActionType.MANUAL_OVERRIDE.value,
            parameter_name="drawdown_state",
            old_value="HARD_LOCK",
            new_value="ACTIVE",
            metadata={"reason": "reviewed and cleared"},
        )
        trail = state_manager.get_audit_trail()
        assert len(trail) == 1
        event = trail[0]
        assert event.action_type == "MANUAL_OVERRIDE"
        assert event.parameter_name == "drawdown_state"
        assert event.old_value == "HARD_LOCK"
        assert event.new_value == "ACTIVE"
        assert event.metadata == {"reason": "reviewed and cleared"}

    def test_actor_is_hashed_not_stored_raw(self, state_manager: StateManager) -> None:
        state_manager.record_audit_event(
            actor="super-secret-operator-name",
            action_type=AuditActionType.CIRCUIT_BREAKER_RESET.value,
            parameter_name="x",
            old_value=None,
            new_value=None,
        )
        event = state_manager.get_audit_trail()[0]
        assert "super-secret-operator-name" not in event.actor_signature
        assert len(event.actor_signature) == 64  # SHA-256 hex digest length

    def test_missing_metadata_defaults_to_empty_dict(self, state_manager: StateManager) -> None:
        state_manager.record_audit_event(
            actor="system", action_type="X", parameter_name="y", old_value=1, new_value=2
        )
        assert state_manager.get_audit_trail()[0].metadata == {}

    def test_filter_by_parameter_name(self, state_manager: StateManager) -> None:
        state_manager.record_audit_event("system", "A", "param-1", None, None)
        state_manager.record_audit_event("system", "B", "param-2", None, None)
        filtered = state_manager.get_audit_trail(parameter_name="param-2")
        assert len(filtered) == 1
        assert filtered[0].action_type == "B"

    def test_audit_trail_is_structurally_append_only(self, state_manager: StateManager) -> None:
        state_manager.record_audit_event("system", "A", "param-1", None, None)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            state_manager._connection.execute("UPDATE audit_trail SET action_type = 'B'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            state_manager._connection.execute("DELETE FROM audit_trail")


# ---------------------------------------------------------------------------
# broker/mt5_gateway.py's Disaster Recovery reconciliation
# (docs/PRODUCTION_SPEC.md §7)
# ---------------------------------------------------------------------------


class TestResolvePositionAudit:
    def _broker_position(self, ticket: int) -> gw.BrokerPosition:
        return gw.BrokerPosition(
            ticket=ticket,
            symbol="XAUUSD",
            side="BUY",
            volume=0.10,
            price_open=2000.0,
            price_current=2005.0,
            stop_loss=1990.0,
            take_profit=2020.0,
            profit=50.0,
            magic=555,
            opened_at_utc=datetime(2026, 7, 4, 10, 0, tzinfo=timezone.utc),
        )

    def _ledger_only_entry(self, client_order_id: str) -> TradeLedgerEntry:
        return TradeLedgerEntry(
            client_order_id=client_order_id,
            symbol="XAUUSD",
            side="BUY",
            volume_lots=0.1,
            status="OPEN",
            opened_at_utc="2026-07-04T09:00:00Z",
            magic_number=555,
            broker_ticket=999,
        )

    def test_clean_report_requires_no_review_and_no_upserts(self) -> None:
        report = gw.PositionAuditReport(
            reconciled_tickets=(100,), broker_only_positions=(), ledger_only_entries=()
        )
        plan = gw.resolve_position_audit(report)
        assert plan.ledger_upserts == ()
        assert plan.requires_manual_review is False

    def test_broker_only_position_reconciled_into_new_open_ledger_row(self) -> None:
        report = gw.PositionAuditReport(
            reconciled_tickets=(),
            broker_only_positions=(self._broker_position(150),),
            ledger_only_entries=(),
        )
        plan = gw.resolve_position_audit(report)
        assert plan.requires_manual_review is True
        assert len(plan.ledger_upserts) == 1
        upsert = plan.ledger_upserts[0]
        assert upsert.client_order_id == "disaster-recovery-150"
        assert upsert.status == "OPEN"
        assert upsert.broker_ticket == 150
        assert upsert.volume_lots == 0.10

    def test_ledger_only_entry_reconciled_into_closed_row(self) -> None:
        entry = self._ledger_only_entry("co-2")
        report = gw.PositionAuditReport(
            reconciled_tickets=(), broker_only_positions=(), ledger_only_entries=(entry,)
        )
        reconciled_at = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
        plan = gw.resolve_position_audit(report, reconciled_at_utc=reconciled_at)
        assert plan.requires_manual_review is True
        assert len(plan.ledger_upserts) == 1
        upsert = plan.ledger_upserts[0]
        assert upsert.client_order_id == "co-2"
        assert upsert.status == "CLOSED_RECONCILED"
        assert upsert.closed_at_utc == reconciled_at.isoformat()

    def test_reconciliation_is_idempotent_across_repeated_runs(self) -> None:
        report = gw.PositionAuditReport(
            reconciled_tickets=(),
            broker_only_positions=(self._broker_position(150),),
            ledger_only_entries=(),
        )
        plan_a = gw.resolve_position_audit(report)
        plan_b = gw.resolve_position_audit(report)
        assert plan_a.ledger_upserts[0].client_order_id == plan_b.ledger_upserts[0].client_order_id


# ---------------------------------------------------------------------------
# execution/position_manager.py's emergency liquidation action (Phase 11d)
# ---------------------------------------------------------------------------


class TestBuildEmergencyLiquidationAction:
    def test_full_volume_close_deal(self) -> None:
        position = PositionState(
            ticket=42,
            symbol="XAUUSD",
            side="BUY",
            volume=0.25,
            entry_price=2000.0,
            stop_loss=1990.0,
            magic_number=555,
            partial_closed=True,
            breakeven_set=True,
        )
        action = build_emergency_liquidation_action(position)
        assert action.action == "TRADE_ACTION_DEAL"
        assert action.position_ticket == 42
        assert action.symbol == "XAUUSD"
        assert action.magic == 555
        assert action.volume == 0.25
        assert action.comment == EMERGENCY_LIQUIDATION_COMMENT


class TestBuildShortTermLiquidationAction:
    def test_full_volume_close_deal_from_scalar_fields(self) -> None:
        action = build_short_term_liquidation_action(
            ticket=99, symbol="XAUUSD", magic_number=777, volume=0.01
        )
        assert action.action == "TRADE_ACTION_DEAL"
        assert action.position_ticket == 99
        assert action.symbol == "XAUUSD"
        assert action.magic == 777
        assert action.volume == 0.01
        assert action.comment == EMERGENCY_LIQUIDATION_COMMENT


class TestEntryDecision:
    def _trend(self, direction: str, adx_confirmed: bool = True) -> TrendAlignment:
        return TrendAlignment(
            direction=direction,  # type: ignore[arg-type]
            d1_bullish=direction == "BULLISH",
            h4_bullish=direction == "BULLISH",
            h1_bullish=direction == "BULLISH",
            adx_value=30.0 if adx_confirmed else 10.0,
            adx_confirmed=adx_confirmed,
        )

    def _no_signal(self) -> tuple[BreakoutSignal, PullbackSignal, WickFillResult]:
        breakout = BreakoutSignal("NONE", 0.0, False)
        pullback = PullbackSignal("NONE", 0.0)
        wick_fill = WickFillResult(0.0, 0.0, "NONE")
        return breakout, pullback, wick_fill

    def test_news_locked_blocks_entry(self) -> None:
        breakout, pullback, wick_fill = self._no_signal()
        decision = orchestrator.decide_entry_signal(
            self._trend("BULLISH"), breakout, pullback, wick_fill, news_locked=True
        )
        assert decision.direction == "NONE"
        assert "news" in decision.reason

    def test_invalid_trend_blocks_entry(self) -> None:
        breakout, pullback, wick_fill = self._no_signal()
        decision = orchestrator.decide_entry_signal(
            self._trend("BULLISH", adx_confirmed=False),
            breakout,
            pullback,
            wick_fill,
            news_locked=False,
        )
        assert decision.direction == "NONE"

    def test_breakout_confirms_bullish_trend(self) -> None:
        breakout = BreakoutSignal("BUY", 60.0, True)
        _, pullback, wick_fill = self._no_signal()
        decision = orchestrator.decide_entry_signal(
            self._trend("BULLISH"), breakout, pullback, wick_fill, news_locked=False
        )
        assert decision.direction == "BUY"
        assert "breakout" in decision.reason

    def test_pullback_confirms_bearish_trend(self) -> None:
        pullback = PullbackSignal("SELL", 2000.0)
        breakout, _, wick_fill = self._no_signal()
        decision = orchestrator.decide_entry_signal(
            self._trend("BEARISH"), breakout, pullback, wick_fill, news_locked=False
        )
        assert decision.direction == "SELL"
        assert "pullback" in decision.reason

    def test_wick_fill_confirms_bullish_trend(self) -> None:
        wick_fill = WickFillResult(0.1, 0.8, "BUY")
        breakout, pullback, _ = self._no_signal()
        decision = orchestrator.decide_entry_signal(
            self._trend("BULLISH"), breakout, pullback, wick_fill, news_locked=False
        )
        assert decision.direction == "BUY"
        assert "wick-fill" in decision.reason

    def test_no_agreeing_trigger_blocks_entry(self) -> None:
        breakout, pullback, wick_fill = self._no_signal()
        decision = orchestrator.decide_entry_signal(
            self._trend("BULLISH"), breakout, pullback, wick_fill, news_locked=False
        )
        assert decision.direction == "NONE"

    def test_conflicting_signal_direction_ignored(self) -> None:
        # A SELL breakout while the trend is BULLISH must not trigger an entry.
        breakout = BreakoutSignal("SELL", 60.0, True)
        _, pullback, wick_fill = self._no_signal()
        decision = orchestrator.decide_entry_signal(
            self._trend("BULLISH"), breakout, pullback, wick_fill, news_locked=False
        )
        assert decision.direction == "NONE"


class TestShortTermEntrySignal:
    """`decide_short_term_entry_signal()` — structurally identical to
    `decide_entry_signal()` (same `_select_trigger_signal()` tail), gated
    on `ShortTermTrendAlignment.is_valid` instead."""

    def _short_trend(self, direction: str, adx_confirmed: bool = True) -> ShortTermTrendAlignment:
        return ShortTermTrendAlignment(
            direction=direction,  # type: ignore[arg-type]
            h1_bullish=direction == "BULLISH",
            h1_bearish=direction == "BEARISH",
            adx_value=20.0 if adx_confirmed else 5.0,
            adx_confirmed=adx_confirmed,
        )

    def _no_signal(self) -> tuple[BreakoutSignal, PullbackSignal, WickFillResult]:
        breakout = BreakoutSignal("NONE", 0.0, False)
        pullback = PullbackSignal("NONE", 0.0)
        wick_fill = WickFillResult(0.0, 0.0, "NONE")
        return breakout, pullback, wick_fill

    def test_news_locked_blocks_entry(self) -> None:
        breakout, pullback, wick_fill = self._no_signal()
        decision = orchestrator.decide_short_term_entry_signal(
            self._short_trend("BULLISH"), breakout, pullback, wick_fill, news_locked=True
        )
        assert decision.direction == "NONE"
        assert "news" in decision.reason

    def test_invalid_trend_blocks_entry(self) -> None:
        breakout, pullback, wick_fill = self._no_signal()
        decision = orchestrator.decide_short_term_entry_signal(
            self._short_trend("BULLISH", adx_confirmed=False),
            breakout,
            pullback,
            wick_fill,
            news_locked=False,
        )
        assert decision.direction == "NONE"

    def test_breakout_confirms_bullish_short_term_trend(self) -> None:
        breakout = BreakoutSignal("BUY", 60.0, True)
        _, pullback, wick_fill = self._no_signal()
        decision = orchestrator.decide_short_term_entry_signal(
            self._short_trend("BULLISH"), breakout, pullback, wick_fill, news_locked=False
        )
        assert decision.direction == "BUY"
        assert "breakout" in decision.reason

    def test_pullback_confirms_bearish_short_term_trend(self) -> None:
        pullback = PullbackSignal("SELL", 2000.0)
        breakout, _, wick_fill = self._no_signal()
        decision = orchestrator.decide_short_term_entry_signal(
            self._short_trend("BEARISH"), breakout, pullback, wick_fill, news_locked=False
        )
        assert decision.direction == "SELL"
        assert "pullback" in decision.reason

    def test_wick_fill_confirms_bullish_short_term_trend(self) -> None:
        wick_fill = WickFillResult(0.1, 0.8, "BUY")
        breakout, pullback, _ = self._no_signal()
        decision = orchestrator.decide_short_term_entry_signal(
            self._short_trend("BULLISH"), breakout, pullback, wick_fill, news_locked=False
        )
        assert decision.direction == "BUY"
        assert "wick-fill" in decision.reason

    def test_no_agreeing_trigger_blocks_entry(self) -> None:
        breakout, pullback, wick_fill = self._no_signal()
        decision = orchestrator.decide_short_term_entry_signal(
            self._short_trend("BULLISH"), breakout, pullback, wick_fill, news_locked=False
        )
        assert decision.direction == "NONE"


class _BarCloseCycleHelpers:
    """Shared fixtures/helpers for `TestBarCloseCycle` and
    `TestBarCloseCycleShortTermMode` — deliberately NOT `Test`-prefixed so
    pytest doesn't collect it as its own (empty) test class, and neither
    subclass re-runs the other's tests via inheritance."""

    @pytest.fixture
    def baselines(self) -> EquityBaselines:
        return EquityBaselines(10_000.0, 10_000.0, 10_000.0)

    @pytest.fixture
    def constraints(self) -> orchestrator.SymbolConstraints:
        return orchestrator.SymbolConstraints(
            point=0.01, volume_min=0.01, volume_max=100.0, volume_step=0.01, magic_number=555
        )

    @pytest.fixture
    def feature_flags(self) -> FeatureFlagManager:
        return FeatureFlagManager(FeatureFlags(liquidate_on_hard_lock=False))

    def _idle_context(self) -> orchestrator.FSMContext:
        return orchestrator.FSMContext(
            state=orchestrator.TradingState.IDLE,
            position=None,
            drawdown_state=DrawdownState.ACTIVE,
            drawdown_reason=None,
        )

    def _in_position_context(
        self, position: PositionState, drawdown_state: DrawdownState = DrawdownState.ACTIVE
    ) -> orchestrator.FSMContext:
        return orchestrator.FSMContext(
            state=orchestrator.TradingState.IN_POSITION,
            position=position,
            drawdown_state=drawdown_state,
            drawdown_reason=None,
        )

    def _position(self, **overrides: object) -> PositionState:
        defaults: dict[str, object] = dict(
            ticket=1,
            symbol="XAUUSD",
            side="BUY",
            volume=0.10,
            entry_price=2000.0,
            stop_loss=1990.0,
            magic_number=555,
            partial_closed=False,
            breakeven_set=False,
        )
        defaults.update(overrides)
        return PositionState(**defaults)  # type: ignore[arg-type]

    def _account_state(self, equity: float) -> gw.AccountState:
        return gw.AccountState(
            balance=equity,
            equity=equity,
            margin_used=0.0,
            margin_free=equity,
            as_of_utc=datetime(2026, 7, 4, 12, 5, tzinfo=timezone.utc),
        )

    def _snapshot(
        self,
        equity: float = 10_000.0,
        trend_direction: str = "BULLISH",
        current_price: float | None = 2010.0,
        short_term_trend_direction: str | None = None,
    ) -> orchestrator.MarketSnapshot:
        breakout = BreakoutSignal("BUY", 60.0, True)
        pullback = PullbackSignal("NONE", 0.0)
        wick_fill = WickFillResult(0.0, 0.0, "NONE")
        trend = TrendAlignment(
            direction=trend_direction,  # type: ignore[arg-type]
            d1_bullish=True,
            h4_bullish=True,
            h1_bullish=True,
            adx_value=30.0,
            adx_confirmed=True,
        )
        short_term_trend = None
        short_term_pullback = None
        if short_term_trend_direction is not None:
            short_term_trend = ShortTermTrendAlignment(
                direction=short_term_trend_direction,  # type: ignore[arg-type]
                h1_bullish=short_term_trend_direction == "BULLISH",
                h1_bearish=short_term_trend_direction == "BEARISH",
                adx_value=20.0,
                adx_confirmed=True,
            )
            short_term_pullback = PullbackSignal("NONE", 0.0)
        return orchestrator.MarketSnapshot(
            now_utc=datetime(2026, 7, 4, 12, 5, tzinfo=timezone.utc),
            account_state=self._account_state(equity),
            current_price=current_price,
            atr_value=5.0,
            trend=trend,
            breakout=breakout,
            pullback=pullback,
            wick_fill=wick_fill,
            news_events=[],
            short_term_trend=short_term_trend,
            short_term_pullback=short_term_pullback,
        )

    def _broker_position(self, **overrides: object) -> gw.BrokerPosition:
        defaults: dict[str, object] = dict(
            ticket=99,
            symbol="XAUUSD",
            side="BUY",
            volume=0.01,
            price_open=2000.0,
            price_current=2000.0,
            stop_loss=1990.0,
            take_profit=2010.0,
            profit=0.0,
            magic=777,
            opened_at_utc=datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc),
        )
        defaults.update(overrides)
        return gw.BrokerPosition(**defaults)  # type: ignore[arg-type]


class TestBarCloseCycle(_BarCloseCycleHelpers):
    def test_soft_lock_blocks_new_entries_while_flat(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        context = self._idle_context()
        snapshot = self._snapshot(equity=9_300.0)  # 7% down: SOFT_LOCK, not HARD_LOCK
        result = orchestrator.run_bar_close_cycle(
            context, snapshot, baselines, constraints, feature_flags, cycle_duration_seconds=0.05
        )
        assert result.context.drawdown_state == DrawdownState.SOFT_LOCK
        assert result.context.drawdown_reason is not None
        assert result.entry_decision is None
        assert result.position_actions == ()

    def test_soft_lock_still_allows_position_management(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        # Already partial-closed + breakeven-set so the trailing-stop path
        # fires (matches test_in_position_after_breakeven_trails_stop).
        position = self._position(
            volume=0.05, stop_loss=2000.0, partial_closed=True, breakeven_set=True
        )
        context = self._in_position_context(position)
        snapshot = self._snapshot(
            equity=9_300.0, current_price=2020.0
        )  # SOFT_LOCK + rallying price
        result = orchestrator.run_bar_close_cycle(
            context, snapshot, baselines, constraints, feature_flags, cycle_duration_seconds=0.05
        )
        assert result.context.drawdown_state == DrawdownState.SOFT_LOCK
        assert len(result.position_actions) == 1
        assert result.position_actions[0].action == "TRADE_ACTION_SLTP"

    def test_warning_state_is_advisory_only(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        context = self._idle_context()
        snapshot = self._snapshot(equity=9_700.0)  # 3% down: WARNING, nothing restricted
        with caplog.at_level("WARNING"):
            result = orchestrator.run_bar_close_cycle(
                context,
                snapshot,
                baselines,
                constraints,
                feature_flags,
                cycle_duration_seconds=0.05,
            )
        assert result.context.drawdown_state == DrawdownState.WARNING
        assert "Drawdown WARNING" in caplog.text
        assert result.entry_decision is not None
        assert result.entry_decision.direction == "BUY"

    def test_hard_lock_recovers_no_further_than_manual_reset_required(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        context = self._idle_context()
        snapshot = self._snapshot(equity=8_000.0)  # 20% down: HARD_LOCK (uniform baselines)
        result = orchestrator.run_bar_close_cycle(
            context, snapshot, baselines, constraints, feature_flags, cycle_duration_seconds=0.05
        )
        assert result.context.drawdown_state == DrawdownState.HARD_LOCK
        assert result.position_actions == ()  # flat: nothing to liquidate

    def test_hard_lock_with_liquidate_flag_true_emits_liquidation_action(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
    ) -> None:
        liquidating_flags = FeatureFlagManager(FeatureFlags(liquidate_on_hard_lock=True))
        position = self._position(volume=0.10)
        context = self._in_position_context(position)
        snapshot = self._snapshot(equity=8_000.0)  # 20% down: HARD_LOCK
        result = orchestrator.run_bar_close_cycle(
            context,
            snapshot,
            baselines,
            constraints,
            liquidating_flags,
            cycle_duration_seconds=0.05,
        )
        assert result.context.drawdown_state == DrawdownState.HARD_LOCK
        assert len(result.position_actions) == 1
        liquidation = result.position_actions[0]
        assert liquidation.action == "TRADE_ACTION_DEAL"
        assert liquidation.volume == 0.10  # full remaining volume, not a partial close
        assert liquidation.comment == EMERGENCY_LIQUIDATION_COMMENT

    def test_hard_lock_with_liquidate_flag_false_freezes_everything(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        position = self._position()
        context = self._in_position_context(position)
        snapshot = self._snapshot(equity=8_000.0, current_price=2010.0)  # would trigger Base_TP too
        result = orchestrator.run_bar_close_cycle(
            context, snapshot, baselines, constraints, feature_flags, cycle_duration_seconds=0.05
        )
        assert result.context.drawdown_state == DrawdownState.HARD_LOCK
        assert result.position_actions == ()  # frozen: no partial-close/trailing either

    def test_manual_reset_required_stays_frozen_regardless_of_recovery(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        context = orchestrator.FSMContext(
            state=orchestrator.TradingState.IDLE,
            position=None,
            drawdown_state=DrawdownState.MANUAL_RESET_REQUIRED,
            drawdown_reason="prior HARD_LOCK",
        )
        snapshot = self._snapshot(equity=10_000.0)  # fully recovered
        result = orchestrator.run_bar_close_cycle(
            context, snapshot, baselines, constraints, feature_flags, cycle_duration_seconds=0.05
        )
        assert result.context.drawdown_state == DrawdownState.MANUAL_RESET_REQUIRED
        assert result.entry_decision is None

    def test_manual_reset_confirmed_clears_the_lock(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        context = orchestrator.FSMContext(
            state=orchestrator.TradingState.IDLE,
            position=None,
            drawdown_state=DrawdownState.MANUAL_RESET_REQUIRED,
            drawdown_reason="prior HARD_LOCK",
        )
        snapshot = self._snapshot(equity=10_000.0)
        result = orchestrator.run_bar_close_cycle(
            context,
            snapshot,
            baselines,
            constraints,
            feature_flags,
            cycle_duration_seconds=0.05,
            manual_reset_confirmed=True,
        )
        assert result.context.drawdown_state == DrawdownState.ACTIVE
        assert result.context.drawdown_reason is None

    def test_idle_with_confirmed_entry_proposes_order(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        context = self._idle_context()
        snapshot = self._snapshot()
        result = orchestrator.run_bar_close_cycle(
            context, snapshot, baselines, constraints, feature_flags, cycle_duration_seconds=0.05
        )
        assert result.entry_decision is not None
        assert result.entry_decision.direction == "BUY"
        assert result.entry_stop_loss is not None
        assert result.entry_stop_loss < snapshot.current_price  # type: ignore[operator]
        assert result.entry_volume is not None
        assert result.position_actions == ()

    def test_in_position_triggers_partial_close_at_base_tp(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        position = self._position()
        context = self._in_position_context(position)
        # Base_TP = 2000 + 5*2 = 2010, current_price=2010 in the snapshot -> triggers.
        snapshot = self._snapshot(current_price=2010.0)
        result = orchestrator.run_bar_close_cycle(
            context, snapshot, baselines, constraints, feature_flags, cycle_duration_seconds=0.05
        )
        assert len(result.position_actions) == 2
        assert result.position_actions[0].action == "TRADE_ACTION_DEAL"
        assert result.entry_decision is None

    def test_in_position_after_breakeven_trails_stop(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        # Already partial-closed + breakeven-set, so evaluate_partial_close_and_breakeven
        # returns no actions and calculate_trailing_stop's fallback branch fires.
        position = self._position(
            volume=0.05, stop_loss=2000.0, partial_closed=True, breakeven_set=True
        )
        context = self._in_position_context(position)
        # price rallied to 2020, ATR=5 -> candidate SL = 2020 - 1.5*5 = 2012.5 > 2000.
        snapshot = self._snapshot(current_price=2020.0)
        result = orchestrator.run_bar_close_cycle(
            context, snapshot, baselines, constraints, feature_flags, cycle_duration_seconds=0.05
        )
        assert len(result.position_actions) == 1
        assert result.position_actions[0].action == "TRADE_ACTION_SLTP"
        assert result.position_actions[0].stop_loss == 2012.5

    def test_no_current_price_yields_no_action_while_in_position(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        position = self._position(partial_closed=True, breakeven_set=True)
        context = self._in_position_context(position)
        snapshot = self._snapshot(current_price=None)
        result = orchestrator.run_bar_close_cycle(
            context, snapshot, baselines, constraints, feature_flags, cycle_duration_seconds=0.05
        )
        assert result.position_actions == ()

    def test_processing_cap_breach_is_logged(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        context = self._idle_context()
        snapshot = self._snapshot()
        with caplog.at_level("WARNING"):
            result = orchestrator.run_bar_close_cycle(
                context,
                snapshot,
                baselines,
                constraints,
                feature_flags,
                cycle_duration_seconds=0.25,
            )
        assert result.processing.exceeded_cap is True
        assert "200ms" in caplog.text

    def test_idle_signal_fires_but_no_price_yields_no_order_sizing(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        context = self._idle_context()
        snapshot = self._snapshot(current_price=None)
        result = orchestrator.run_bar_close_cycle(
            context, snapshot, baselines, constraints, feature_flags, cycle_duration_seconds=0.05
        )
        assert result.entry_decision is not None
        assert result.entry_decision.direction == "BUY"  # signal fires...
        assert result.entry_stop_loss is None  # ...but no current_price to size against
        assert result.entry_volume is None


class TestBarCloseCycleShortTermMode(_BarCloseCycleHelpers):
    """`trading_mode`/`short_term_position` — the short-term mode's entry
    evaluation, orthogonal to the regular mode's `context.position`
    branch. Shares `TestBarCloseCycle`'s fixtures/helpers via
    `_BarCloseCycleHelpers`, not by subclassing it."""

    def test_wait_for_conditions_mode_ignores_short_term_fields_even_with_valid_signal(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        # Default trading_mode; short_term_trend populated but must be
        # ignored entirely — this is the "existing tests keep passing
        # unmodified" regression this design depends on.
        context = self._idle_context()
        snapshot = self._snapshot(short_term_trend_direction="BULLISH")
        result = orchestrator.run_bar_close_cycle(
            context, snapshot, baselines, constraints, feature_flags, cycle_duration_seconds=0.05
        )
        assert result.short_term_entry_decision is None
        assert result.short_term_entry_stop_loss is None
        assert result.short_term_entry_take_profit is None
        assert result.short_term_entry_volume is None

    def test_short_term_mode_proposes_entry_independent_of_regular_position(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        # Regular mode is IN_POSITION (mismatched trend so it proposes no
        # new regular entry) while short-term mode is flat and has a
        # valid signal — both must be evaluated independently.
        position = self._position()
        context = self._in_position_context(position)
        snapshot = self._snapshot(trend_direction="BEARISH", short_term_trend_direction="BULLISH")
        result = orchestrator.run_bar_close_cycle(
            context,
            snapshot,
            baselines,
            constraints,
            feature_flags,
            cycle_duration_seconds=0.05,
            trading_mode="SHORT_TERM",
        )
        assert result.short_term_entry_decision is not None
        assert result.short_term_entry_decision.direction == "BUY"
        assert result.short_term_entry_stop_loss == pytest.approx(2010.0 - 5.0)
        assert result.short_term_entry_take_profit == pytest.approx(2010.0 + 5.0)
        assert result.short_term_entry_volume == constraints.volume_min

    def test_both_mode_regular_in_position_short_term_flat_no_collision(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        position = self._position()
        context = self._in_position_context(position)
        snapshot = self._snapshot(short_term_trend_direction="BULLISH")
        result = orchestrator.run_bar_close_cycle(
            context,
            snapshot,
            baselines,
            constraints,
            feature_flags,
            cycle_duration_seconds=0.05,
            trading_mode="BOTH",
        )
        # Regular mode still manages its own position...
        assert result.entry_decision is None
        # ...while short-term independently proposes its own entry.
        assert result.short_term_entry_decision is not None
        assert result.short_term_entry_decision.direction == "BUY"

    def test_both_mode_short_term_position_already_open_skips_new_entry(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        context = self._idle_context()
        snapshot = self._snapshot(short_term_trend_direction="BULLISH")
        result = orchestrator.run_bar_close_cycle(
            context,
            snapshot,
            baselines,
            constraints,
            feature_flags,
            cycle_duration_seconds=0.05,
            trading_mode="BOTH",
            short_term_position=self._broker_position(),
        )
        assert result.short_term_entry_decision is None

    def test_soft_lock_blocks_short_term_new_entries_too(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        context = self._idle_context()
        snapshot = self._snapshot(
            equity=9_300.0, short_term_trend_direction="BULLISH"
        )  # 7% down: SOFT_LOCK
        result = orchestrator.run_bar_close_cycle(
            context,
            snapshot,
            baselines,
            constraints,
            feature_flags,
            cycle_duration_seconds=0.05,
            trading_mode="BOTH",
        )
        assert result.context.drawdown_state == DrawdownState.SOFT_LOCK
        assert result.short_term_entry_decision is None

    def test_news_lock_blocks_short_term_entries(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        context = self._idle_context()
        snapshot = self._snapshot(short_term_trend_direction="BULLISH")
        news_event = EconomicEvent(
            title="Non-Farm Payrolls",
            country="US",
            impact="HIGH",
            scheduled_at_utc=datetime(2026, 7, 4, 12, 5, tzinfo=timezone.utc),
        )
        snapshot = orchestrator.MarketSnapshot(
            now_utc=snapshot.now_utc,
            account_state=snapshot.account_state,
            current_price=snapshot.current_price,
            atr_value=snapshot.atr_value,
            trend=snapshot.trend,
            breakout=snapshot.breakout,
            pullback=snapshot.pullback,
            wick_fill=snapshot.wick_fill,
            news_events=[news_event],
            short_term_trend=snapshot.short_term_trend,
            short_term_pullback=snapshot.short_term_pullback,
        )
        result = orchestrator.run_bar_close_cycle(
            context,
            snapshot,
            baselines,
            constraints,
            feature_flags,
            cycle_duration_seconds=0.05,
            trading_mode="SHORT_TERM",
        )
        assert result.short_term_entry_decision is not None
        assert result.short_term_entry_decision.direction == "NONE"

    def test_hard_lock_liquidates_both_positions_when_flag_true(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
    ) -> None:
        feature_flags = FeatureFlagManager(FeatureFlags(liquidate_on_hard_lock=True))
        position = self._position()
        context = self._in_position_context(position)
        snapshot = self._snapshot(equity=8_500.0)  # 15% down: HARD_LOCK
        short_term_position = self._broker_position(ticket=888, magic=777, volume=0.02)
        result = orchestrator.run_bar_close_cycle(
            context,
            snapshot,
            baselines,
            constraints,
            feature_flags,
            cycle_duration_seconds=0.05,
            trading_mode="BOTH",
            short_term_position=short_term_position,
        )
        assert result.context.drawdown_state == DrawdownState.HARD_LOCK
        assert len(result.position_actions) == 2
        tickets = {action.position_ticket for action in result.position_actions}
        assert tickets == {position.ticket, short_term_position.ticket}
        magics = {action.magic for action in result.position_actions}
        assert magics == {position.magic_number, short_term_position.magic}

    def test_hard_lock_freezes_both_positions_when_flag_false(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        position = self._position()
        context = self._in_position_context(position)
        snapshot = self._snapshot(equity=8_500.0)  # 15% down: HARD_LOCK
        short_term_position = self._broker_position()
        result = orchestrator.run_bar_close_cycle(
            context,
            snapshot,
            baselines,
            constraints,
            feature_flags,  # liquidate_on_hard_lock=False by default fixture
            cycle_duration_seconds=0.05,
            trading_mode="BOTH",
            short_term_position=short_term_position,
        )
        assert result.context.drawdown_state == DrawdownState.HARD_LOCK
        assert result.position_actions == ()


class TestUpdateShortTermPeakPrice:
    """`_update_short_term_peak_price()` — pure peak tracking behind the
    short-term mode's profit-peak lock."""

    def _position(self, side: str, price_current: float) -> gw.BrokerPosition:
        return gw.BrokerPosition(
            ticket=1,
            symbol="XAUUSD",
            side=side,
            volume=0.01,
            price_open=2000.0,
            price_current=price_current,
            stop_loss=1990.0,
            take_profit=2010.0,
            profit=0.0,
            magic=777,
            opened_at_utc=datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc),
        )

    def test_flat_position_resets_to_none(self) -> None:
        assert orchestrator._update_short_term_peak_price(None, 2050.0) is None

    def test_fresh_position_initializes_to_current_price(self) -> None:
        position = self._position("BUY", 2005.0)
        assert orchestrator._update_short_term_peak_price(position, None) == 2005.0

    def test_buy_tracks_running_maximum(self) -> None:
        position = self._position("BUY", 2010.0)
        assert orchestrator._update_short_term_peak_price(position, 2005.0) == 2010.0

    def test_buy_does_not_lower_peak_on_retracement(self) -> None:
        position = self._position("BUY", 2003.0)
        assert orchestrator._update_short_term_peak_price(position, 2010.0) == 2010.0

    def test_sell_tracks_running_minimum(self) -> None:
        position = self._position("SELL", 1990.0)
        assert orchestrator._update_short_term_peak_price(position, 1995.0) == 1990.0

    def test_sell_does_not_raise_peak_on_retracement(self) -> None:
        position = self._position("SELL", 1998.0)
        assert orchestrator._update_short_term_peak_price(position, 1990.0) == 1990.0


class TestDecideShortTermProfitLock:
    """`decide_short_term_profit_lock()` — the profit-peak retracement rule."""

    def _position(self, side: str) -> gw.BrokerPosition:
        return gw.BrokerPosition(
            ticket=1,
            symbol="XAUUSD",
            side=side,
            volume=0.01,
            price_open=2000.0,
            price_current=2000.0,
            stop_loss=1990.0,
            take_profit=2010.0,
            profit=0.0,
            magic=777,
            opened_at_utc=datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc),
        )

    def test_buy_locks_when_retraced_past_threshold(self) -> None:
        # Peak 2010, ATR 5.0, default multiplier 0.5 -> trigger at >= 2.5
        # retracement. Current 2007.0 is 3.0 below peak: fires.
        position = self._position("BUY")
        assert (
            orchestrator.decide_short_term_profit_lock(
                position, current_price=2007.0, atr_value=5.0, peak_price=2010.0
            )
            is True
        )

    def test_buy_does_not_lock_within_threshold(self) -> None:
        # Only 1.0 retraced from the peak: below the 2.5 trigger.
        position = self._position("BUY")
        assert (
            orchestrator.decide_short_term_profit_lock(
                position, current_price=2009.0, atr_value=5.0, peak_price=2010.0
            )
            is False
        )

    def test_buy_underwater_never_locks_even_with_large_retracement(self) -> None:
        # current_price below entry: not "in profit" at all, regardless of
        # how far it retraced from a peak that was itself barely above entry.
        position = self._position("BUY")
        assert (
            orchestrator.decide_short_term_profit_lock(
                position, current_price=1990.0, atr_value=1.0, peak_price=2001.0
            )
            is False
        )

    def test_sell_locks_when_retraced_past_threshold(self) -> None:
        position = self._position("SELL")
        assert (
            orchestrator.decide_short_term_profit_lock(
                position, current_price=1993.0, atr_value=5.0, peak_price=1990.0
            )
            is True
        )

    def test_sell_does_not_lock_within_threshold(self) -> None:
        position = self._position("SELL")
        assert (
            orchestrator.decide_short_term_profit_lock(
                position, current_price=1991.0, atr_value=5.0, peak_price=1990.0
            )
            is False
        )


class TestEvaluateShortTermPositionManagement:
    """`_evaluate_short_term_position_management()` — the "already open"
    complement to `_evaluate_short_term_entry()`."""

    def _position(self, **overrides: object) -> gw.BrokerPosition:
        defaults: dict[str, object] = dict(
            ticket=42,
            symbol="XAUUSD",
            side="BUY",
            volume=0.01,
            price_open=2000.0,
            price_current=2000.0,
            stop_loss=1990.0,
            take_profit=2010.0,
            profit=0.0,
            magic=777,
            opened_at_utc=datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc),
        )
        defaults.update(overrides)
        return gw.BrokerPosition(**defaults)  # type: ignore[arg-type]

    def test_flat_yields_no_peak_and_no_action(self) -> None:
        peak, action = orchestrator._evaluate_short_term_position_management(None, 2005.0, 5.0)
        assert peak is None
        assert action is None

    def test_fresh_position_initializes_peak_without_closing(self) -> None:
        position = self._position(price_current=2003.0)
        peak, action = orchestrator._evaluate_short_term_position_management(position, None, 5.0)
        assert peak == 2003.0
        assert action is None

    def test_retraced_position_closes_with_correct_payload(self) -> None:
        position = self._position(ticket=555, volume=0.02, price_current=2007.0, magic=999)
        peak, action = orchestrator._evaluate_short_term_position_management(position, 2010.0, 5.0)
        assert peak == 2010.0  # retracement doesn't lower the tracked peak
        assert action is not None
        assert action.action == "TRADE_ACTION_DEAL"
        assert action.position_ticket == 555
        assert action.volume == 0.02
        assert action.magic == 999
        assert action.comment == orchestrator.SHORT_TERM_PROFIT_LOCK_COMMENT

    def test_not_yet_retraced_continues_tracking_no_close(self) -> None:
        position = self._position(price_current=2011.0)
        peak, action = orchestrator._evaluate_short_term_position_management(position, 2010.0, 5.0)
        assert peak == 2011.0  # new high, peak extends further
        assert action is None


class TestBarCloseCycleProfitLock(_BarCloseCycleHelpers):
    """`run_bar_close_cycle()`'s end-to-end wiring of the short-term
    profit-peak lock, including its isolation from the regular position's
    `FSMContext` via a distinct action comment."""

    def test_profit_lock_closes_short_term_without_touching_regular_position(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        # Regular mode holds its own, unrelated position this cycle.
        regular_position = self._position()
        context = self._in_position_context(regular_position)
        snapshot = self._snapshot(current_price=2000.0)  # regular's own ATR/price context
        short_term_position = self._broker_position(
            ticket=888, side="BUY", price_open=2000.0, price_current=2007.0, volume=0.02
        )
        result = orchestrator.run_bar_close_cycle(
            context,
            snapshot,
            baselines,
            constraints,
            feature_flags,
            cycle_duration_seconds=0.05,
            trading_mode="SHORT_TERM",
            short_term_position=short_term_position,
            # ATR is 5.0 in the fixture -> retraced 3.0 >= the 2.5 trigger.
            short_term_peak_price=2010.0,
        )
        short_term_closes = [
            a
            for a in result.position_actions
            if a.comment == orchestrator.SHORT_TERM_PROFIT_LOCK_COMMENT
        ]
        assert len(short_term_closes) == 1
        assert short_term_closes[0].position_ticket == 888
        assert result.short_term_peak_price == 2010.0
        # The regular position's own management action (if any) never
        # carries the profit-lock comment, and EMERGENCY_LIQUIDATION_COMMENT
        # never appears at all — nothing here resembles a HARD_LOCK.
        assert all(a.comment != EMERGENCY_LIQUIDATION_COMMENT for a in result.position_actions)

    def test_peak_price_echoed_back_when_no_lock_triggered(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        context = self._idle_context()
        snapshot = self._snapshot()
        short_term_position = self._broker_position(price_current=2001.0)
        result = orchestrator.run_bar_close_cycle(
            context,
            snapshot,
            baselines,
            constraints,
            feature_flags,
            cycle_duration_seconds=0.05,
            trading_mode="SHORT_TERM",
            short_term_position=short_term_position,
            short_term_peak_price=None,
        )
        assert result.short_term_peak_price == 2001.0
        assert result.position_actions == ()
