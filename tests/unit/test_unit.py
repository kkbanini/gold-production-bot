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
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import backtester.walk_forward as wf
import broker.clock_provider as cp
import broker.mt5_gateway as gw
import main as orchestrator
import monitoring.notifier as notifier_module
import monitoring.telegram_bot as telegram_bot
import news.calendar_provider as calp
import resilience.backoff as backoff
from analytics.performance import (
    cagr,
    compute_returns,
    deflated_sharpe_ratio,
    infer_periods_per_year,
    kurtosis,
    mar_ratio,
    max_drawdown,
    max_drawdown_duration,
    profit_factor,
    sharpe_ratio,
    skewness,
    sortino_ratio,
    win_rate,
)
from backtester.historical_data import audit_bar_series
from backtester.ml_signal_model import (
    LogisticRegressionEvaluation,
    MLValidationReport,
    ModelEvaluation,
    binomial_ci_lower_bound,
    build_feature_matrix,
    evaluate_promotion_bar,
    prepare_training_data,
)
from backtester.replay_gateway import HistoricalReplayGateway
from backtester.signal_validation import (
    SignalPrediction,
    SignalValidationResult,
    build_forward_labels,
    validate_signal,
)
from backtester.walk_forward import generate_folds, generate_parameter_grid
from config.calendar_config import CalendarConfig
from config.config_manager import ConfigManager, ConfigurationError, ConfigValidator
from config.feature_flags import FeatureFlagManager, FeatureFlags
from config.secret_redaction import SecretRedactingFilter
from config.telegram_config import TelegramConfig
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
from indicators.math_engine import adx, atr, bollinger_bands, ema, macd, rsi, sma
from monitoring.telegram_bot import (
    HEARTBEAT_STALE_AFTER,
    format_account_message,
    format_condition_message,
    format_indicator_message,
    format_order_history_message,
    format_status_message,
    summarize_indicator_signal,
)
from news.news_engine import (
    MACRO_BLACKOUT_WINDOW,
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
from risk.risk_manager import (
    calculate_compounded_lot_size,
    calculate_price_distance_for_target_profit,
    clamp_lot_size,
    normalize_cent_denominated_equity,
)
from storage.db_engine import DEFAULT_BUSY_TIMEOUT_MS, checkpoint_wal, connect, initialize_schema
from storage.migrations import MIGRATIONS, apply_pending_migrations, get_applied_migrations
from storage.state_manager import (
    AuditActionType,
    HeartbeatInfo,
    OrderEvent,
    OrderLifecycleState,
    StateManager,
    TradeLedgerEntry,
)
from strategy.execution_triggers import (
    BreakoutSignal,
    Direction,
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
        # Drawdown lock thresholds default to RQ-022's own percentages when
        # the optional override env vars are unset.
        assert cfg.daily_soft_lock_limit == pytest.approx(0.05)
        assert cfg.weekly_soft_lock_limit == pytest.approx(0.10)
        assert cfg.monthly_soft_lock_limit == pytest.approx(0.20)
        assert cfg.daily_hard_lock_limit == pytest.approx(0.10)
        assert cfg.weekly_hard_lock_limit == pytest.approx(0.20)
        assert cfg.monthly_hard_lock_limit == pytest.approx(0.40)

    def test_drawdown_lock_limit_override_applies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("DAILY_SOFT_LOCK_LIMIT", "0.50")
        monkeypatch.setenv("DAILY_HARD_LOCK_LIMIT", "0.70")
        cfg = ConfigManager.load(env_file="nonexistent.env")
        assert cfg.daily_soft_lock_limit == pytest.approx(0.50)
        assert cfg.daily_hard_lock_limit == pytest.approx(0.70)
        # Untouched tiers keep the RQ-022 default.
        assert cfg.weekly_soft_lock_limit == pytest.approx(0.10)

    def test_non_numeric_drawdown_lock_limit_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("DAILY_SOFT_LOCK_LIMIT", "not-a-number")
        with pytest.raises(ConfigurationError, match="DAILY_SOFT_LOCK_LIMIT"):
            ConfigManager.load(env_file="nonexistent.env")

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
# config/telegram_config.py (monitoring/telegram_bot.py's /check command)
# ---------------------------------------------------------------------------


class TestTelegramConfig:
    TELEGRAM_ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_ID")

    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in self.TELEGRAM_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)

    def test_missing_bot_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "12345")
        with pytest.raises(ConfigurationError, match="TELEGRAM_BOT_TOKEN"):
            TelegramConfig.from_env(env_file="nonexistent.env")

    def test_missing_chat_id_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        with pytest.raises(ConfigurationError, match="TELEGRAM_ALLOWED_CHAT_ID"):
            TelegramConfig.from_env(env_file="nonexistent.env")

    def test_non_integer_chat_id_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "not-a-number")
        with pytest.raises(ConfigurationError, match="TELEGRAM_ALLOWED_CHAT_ID"):
            TelegramConfig.from_env(env_file="nonexistent.env")

    def test_valid_config_parses_multiple_chat_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "111, 222,333")
        config = TelegramConfig.from_env(env_file="nonexistent.env")
        assert config.bot_token == "123:ABC"
        assert config.allowed_chat_ids == (111, 222, 333)


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


def _ref_rsi(values: list[float], period: int) -> list[float]:
    n = len(values)
    result = [float("nan")] * n
    deltas = [values[i] - values[i - 1] for i in range(1, n)]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result[i + 1] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return result


def _ref_macd(
    values: list[float], fast_period: int, slow_period: int, signal_period: int
) -> tuple[list[float], list[float], list[float]]:
    n = len(values)
    fast = _ref_ema(values, fast_period)
    slow = _ref_ema(values, slow_period)
    macd_line = [
        (f - s) if (f == f and s == s) else float("nan") for f, s in zip(fast, slow, strict=True)
    ]
    start = slow_period - 1
    alpha = 2.0 / (signal_period + 1.0)
    seed_index = start + signal_period - 1
    signal_line = [float("nan")] * n
    signal_line[seed_index] = sum(macd_line[start : seed_index + 1]) / signal_period
    for i in range(seed_index + 1, n):
        signal_line[i] = alpha * macd_line[i] + (1.0 - alpha) * signal_line[i - 1]
    histogram = [
        (m - s) if (m == m and s == s) else float("nan")
        for m, s in zip(macd_line, signal_line, strict=True)
    ]
    return macd_line, signal_line, histogram


def _ref_bollinger_bands(
    values: list[float], period: int, num_std: float
) -> tuple[list[float], list[float], list[float]]:
    n = len(values)
    upper = [float("nan")] * n
    middle = [float("nan")] * n
    lower = [float("nan")] * n
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        std = variance**0.5
        middle[i] = mean
        upper[i] = mean + num_std * std
        lower[i] = mean - num_std * std
    return upper, middle, lower


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

    def test_rsi_matches_reference(self) -> None:
        rng = np.random.default_rng(3)
        values = 1950 + np.cumsum(rng.normal(0, 2, size=60))
        result = rsi(values, 14)
        reference = _ref_rsi(values.tolist(), 14)
        np.testing.assert_allclose(result[14:], reference[14:], rtol=1e-9)

    def test_rsi_stays_within_0_100_bounds(self) -> None:
        rng = np.random.default_rng(3)
        values = 1950 + np.cumsum(rng.normal(0, 2, size=60))
        result = rsi(values, 14)
        valid = result[14:]
        assert np.all(valid >= 0.0)
        assert np.all(valid <= 100.0)

    def test_rsi_unbroken_gains_reads_100(self) -> None:
        values = np.arange(1900.0, 1930.0)
        result = rsi(values, 14)
        assert result[-1] == 100.0

    def test_rsi_insufficient_data_raises(self) -> None:
        with pytest.raises(ValueError, match="need at least"):
            rsi(np.ones(10), 14)

    def test_macd_matches_reference(self) -> None:
        rng = np.random.default_rng(5)
        values = 1950 + np.cumsum(rng.normal(0, 2, size=60))
        macd_line, signal_line, histogram = macd(
            values, fast_period=12, slow_period=26, signal_period=9
        )
        ref_macd_line, ref_signal_line, ref_histogram = _ref_macd(values.tolist(), 12, 26, 9)
        np.testing.assert_allclose(macd_line[25:], ref_macd_line[25:], rtol=1e-9)
        np.testing.assert_allclose(signal_line[33:], ref_signal_line[33:], rtol=1e-9)
        np.testing.assert_allclose(histogram[33:], ref_histogram[33:], rtol=1e-9)

    def test_macd_histogram_equals_line_minus_signal(self) -> None:
        rng = np.random.default_rng(5)
        values = 1950 + np.cumsum(rng.normal(0, 2, size=60))
        macd_line, signal_line, histogram = macd(values)
        valid = ~np.isnan(histogram)
        np.testing.assert_allclose(histogram[valid], (macd_line - signal_line)[valid], rtol=1e-12)

    def test_macd_fast_period_must_be_less_than_slow_period(self) -> None:
        with pytest.raises(ValueError, match="fast_period must be < slow_period"):
            macd(np.ones(60), fast_period=26, slow_period=12, signal_period=9)

    def test_macd_insufficient_data_raises(self) -> None:
        with pytest.raises(ValueError, match="need at least"):
            macd(np.ones(20), fast_period=12, slow_period=26, signal_period=9)

    def test_bollinger_bands_matches_reference(self) -> None:
        rng = np.random.default_rng(9)
        values = 1950 + np.cumsum(rng.normal(0, 2, size=60))
        upper, middle, lower = bollinger_bands(values, period=20, num_std=2.0)
        ref_upper, ref_middle, ref_lower = _ref_bollinger_bands(values.tolist(), 20, 2.0)
        np.testing.assert_allclose(upper[19:], ref_upper[19:], rtol=1e-9)
        np.testing.assert_allclose(middle[19:], ref_middle[19:], rtol=1e-9)
        np.testing.assert_allclose(lower[19:], ref_lower[19:], rtol=1e-9)

    def test_bollinger_bands_upper_above_lower(self) -> None:
        rng = np.random.default_rng(9)
        values = 1950 + np.cumsum(rng.normal(0, 2, size=60))
        upper, middle, lower = bollinger_bands(values, period=20, num_std=2.0)
        valid = ~np.isnan(middle)
        assert np.all(upper[valid] >= middle[valid])
        assert np.all(middle[valid] >= lower[valid])

    def test_bollinger_bands_constant_series_has_zero_width(self) -> None:
        upper, middle, lower = bollinger_bands(np.full(30, 100.0), period=20, num_std=2.0)
        valid = ~np.isnan(middle)
        np.testing.assert_allclose(upper[valid], middle[valid])
        np.testing.assert_allclose(lower[valid], middle[valid])

    def test_bollinger_bands_insufficient_data_raises(self) -> None:
        with pytest.raises(ValueError, match="need at least"):
            bollinger_bands(np.ones(10), period=20)


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

    def test_normalize_cent_denominated_equity_divides_by_100_for_usc(self) -> None:
        # Exness Cent account: 1 USD = 100 USC, so 1000 USC of "equity"
        # is only $10 of real capital.
        assert normalize_cent_denominated_equity(1000.0, "USC") == pytest.approx(10.0)

    def test_normalize_cent_denominated_equity_is_case_insensitive(self) -> None:
        assert normalize_cent_denominated_equity(1000.0, "usc") == pytest.approx(10.0)

    def test_normalize_cent_denominated_equity_leaves_usd_unchanged(self) -> None:
        assert normalize_cent_denominated_equity(1000.0, "USD") == pytest.approx(1000.0)

    def test_normalize_cent_denominated_equity_leaves_unknown_currency_unchanged(self) -> None:
        # Safe failure mode: an unrecognized code falls back to the
        # pre-existing USD-assuming behavior rather than guessing.
        assert normalize_cent_denominated_equity(1000.0, "EUR") == pytest.approx(1000.0)

    def test_normalize_then_compound_matches_real_capital_tier(self) -> None:
        # The end-to-end point of this fix: 1000 USC (really $10) must
        # NOT reach the first $1000 compounding tier the way raw 1000.0
        # equity would.
        cent_equity = 1000.0
        real_usd_equivalent = normalize_cent_denominated_equity(cent_equity, "USC")
        assert calculate_compounded_lot_size(real_usd_equivalent, 0.01, 100.0, 0.01) == 0.01
        # Sanity: the un-normalized value WOULD have reached tier 1.
        assert calculate_compounded_lot_size(cent_equity, 0.01, 100.0, 0.01) == 0.02

    def test_price_distance_for_target_profit_basic(self) -> None:
        # tick_value=$1.00 per 0.01 tick per 1.0 lot, 0.01 lots, $5 target
        # -> distance = 5 * 0.01 / (1.00 * 0.01) = 5.0 price units.
        distance = calculate_price_distance_for_target_profit(5.0, 0.01, 1.0, 0.01)
        assert distance == pytest.approx(5.0)

    def test_price_distance_scales_inversely_with_volume(self) -> None:
        # 10x the volume needs 1/10th the price distance for the same
        # dollar target.
        distance = calculate_price_distance_for_target_profit(5.0, 0.10, 1.0, 0.01)
        assert distance == pytest.approx(0.5)

    def test_price_distance_scales_with_tick_value(self) -> None:
        # Doubling tick_value halves the required distance.
        distance = calculate_price_distance_for_target_profit(5.0, 0.01, 2.0, 0.01)
        assert distance == pytest.approx(2.5)

    def test_price_distance_non_positive_target_raises(self) -> None:
        with pytest.raises(ValueError, match="target_profit_usd must be"):
            calculate_price_distance_for_target_profit(0.0, 0.01, 1.0, 0.01)

    def test_price_distance_non_positive_volume_raises(self) -> None:
        with pytest.raises(ValueError, match="volume must be"):
            calculate_price_distance_for_target_profit(5.0, 0.0, 1.0, 0.01)

    def test_price_distance_non_positive_tick_fields_raise(self) -> None:
        with pytest.raises(ValueError, match="tick_value and tick_size"):
            calculate_price_distance_for_target_profit(5.0, 0.01, 0.0, 0.01)
        with pytest.raises(ValueError, match="tick_value and tick_size"):
            calculate_price_distance_for_target_profit(5.0, 0.01, 1.0, 0.0)


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

    def test_pre_breakeven_trailing_uses_wider_multiplier(self) -> None:
        # Regression test: before breakeven, the stop must still trail
        # (using PRE_BREAKEVEN_TRAILING_ATR_MULTIPLIER, wider than the
        # post-breakeven one) rather than staying frozen at the initial
        # SL no matter how far price runs in favor — previously this
        # returned None unconditionally, letting a position give back
        # 100% of a large unrealized gain with zero protection before
        # ever reaching Base_TP.
        position = PositionState(
            ticket=1,
            symbol="XAUUSD",
            side="BUY",
            volume=0.05,
            entry_price=2000.0,
            stop_loss=1990.0,
            magic_number=555,
            partial_closed=False,
            breakeven_set=False,
        )
        # default pre-breakeven multiplier 2.5 -> candidate = 2050 - 2.5*5
        # = 2037.5, well above the initial SL of 1990: fires.
        action = calculate_trailing_stop(position, current_price=2050.0, atr_value=5.0)
        assert action is not None
        assert action.stop_loss == 2037.5

    def test_pre_breakeven_trailing_does_not_fire_before_meaningful_move(self) -> None:
        # Price is still right at entry: the wide pre-breakeven candidate
        # (2000 - 2.5*5 = 1987.5) doesn't beat the initial SL of 1990, so
        # nothing fires — this is what keeps it from competing with
        # Base_TP on every ordinary fluctuation right after entry.
        position = PositionState(
            ticket=1,
            symbol="XAUUSD",
            side="BUY",
            volume=0.05,
            entry_price=2000.0,
            stop_loss=1990.0,
            magic_number=555,
            partial_closed=False,
            breakeven_set=False,
        )
        action = calculate_trailing_stop(position, current_price=2000.0, atr_value=5.0)
        assert action is None

    def test_pre_breakeven_trailing_for_sell(self) -> None:
        position = PositionState(
            ticket=1,
            symbol="XAUUSD",
            side="SELL",
            volume=0.05,
            entry_price=2000.0,
            stop_loss=2010.0,
            magic_number=555,
            partial_closed=False,
            breakeven_set=False,
        )
        # candidate = 1950 + 2.5*5 = 1962.5, well below the initial SL of
        # 2010: fires.
        action = calculate_trailing_stop(position, current_price=1950.0, atr_value=5.0)
        assert action is not None
        assert action.stop_loss == 1962.5

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

    def test_soft_lock_override_suppresses_default_threshold_breach(
        self, baselines: EquityBaselines
    ) -> None:
        # 5% down would breach the default DAILY_SOFT_LOCK_LIMIT (0.05);
        # a wider override (small-account use case) must not fire on it.
        result = classify_drawdown_event(9_500.0, baselines, daily_soft_lock_limit=0.50)
        assert result.event == DrawdownEvent.WITHIN_TOLERANCE

    def test_hard_lock_override_suppresses_default_threshold_breach(self) -> None:
        # Isolate the daily tier (weekly/monthly baselines equal current
        # equity, i.e. 0% drawdown there) so only the daily override is
        # under test. 10% down would breach the default
        # DAILY_HARD_LOCK_LIMIT (0.10); a wider override must fall through
        # to within-tolerance instead.
        baselines = EquityBaselines(
            daily_start_equity=10_000.0, weekly_start_equity=9_000.0, monthly_start_equity=9_000.0
        )
        result = classify_drawdown_event(
            9_000.0, baselines, daily_soft_lock_limit=0.50, daily_hard_lock_limit=0.70
        )
        assert result.event == DrawdownEvent.WITHIN_TOLERANCE

    def test_override_still_breaches_past_the_wider_threshold(
        self, baselines: EquityBaselines
    ) -> None:
        result = classify_drawdown_event(9_000.0, baselines, daily_soft_lock_limit=0.05)
        assert result.event == DrawdownEvent.HARD_LOCK_THRESHOLD_BREACHED


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
            point=0.01,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            magic_number=555,
            tick_value=1.0,
            tick_size=0.01,
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

    def test_partial_close_updates_position_flags_and_state(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        # Regression test for the deeper bug behind the 5.11-lot ->
        # 0.01-lot cascade: the returned context must actually reflect
        # partial_closed=True/breakeven_set=True/the reduced volume/the
        # breakeven stop_loss — previously it silently passed the OLD
        # position through unchanged, so this same Base_TP check re-fired
        # every subsequent cycle for as long as price stayed at or beyond
        # it, repeatedly halving whatever volume remained.
        position = self._position()  # volume=0.10, entry_price=2000.0
        context = self._in_position_context(position)
        snapshot = self._snapshot(current_price=2010.0)  # Base_TP = 2000 + 5*2
        result = orchestrator.run_bar_close_cycle(
            context, snapshot, baselines, constraints, feature_flags, cycle_duration_seconds=0.05
        )
        assert result.context.position is not None
        assert result.context.position.partial_closed is True
        assert result.context.position.breakeven_set is True
        assert result.context.position.stop_loss == 2000.0  # entry_price, i.e. breakeven
        assert result.context.position.volume == pytest.approx(0.05)  # 0.10 - 50%

    def test_partial_close_does_not_refire_on_next_cycle(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        # The exact bug this closes: feed the FIRST cycle's returned
        # context back in as a SECOND cycle's input, at the same
        # Base_TP-reached price. Previously this fired an identical
        # partial-close again (and would keep firing every cycle
        # indefinitely); now it must not, since partial_closed is
        # correctly True this time.
        position = self._position()
        context = self._in_position_context(position)
        snapshot = self._snapshot(current_price=2010.0)
        first = orchestrator.run_bar_close_cycle(
            context, snapshot, baselines, constraints, feature_flags, cycle_duration_seconds=0.05
        )
        assert len(first.position_actions) == 2

        second = orchestrator.run_bar_close_cycle(
            first.context,
            snapshot,
            baselines,
            constraints,
            feature_flags,
            cycle_duration_seconds=0.05,
        )
        assert not any(a.action == "TRADE_ACTION_DEAL" for a in second.position_actions)

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
        # Regression test: the returned context must carry the new
        # trailing level forward — previously position.stop_loss in the
        # returned context never advanced, so every later cycle compared
        # a fresh candidate against a stale reference instead of the
        # level actually just set at the broker.
        assert result.context.position is not None
        assert result.context.position.stop_loss == 2012.5

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

    def test_short_term_only_mode_disables_regular_new_entries_even_with_valid_signal(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        # The mirror of the test above: trading_mode="SHORT_TERM" must
        # disable the *regular* strategy's new entries entirely, even
        # though the default _snapshot() fixture is a fully valid regular
        # BUY signal (D1+H4+H1 aligned, ADX-confirmed, breakout agrees) —
        # decide_entry_signal() must never even be reached.
        context = self._idle_context()
        snapshot = self._snapshot()
        result = orchestrator.run_bar_close_cycle(
            context,
            snapshot,
            baselines,
            constraints,
            feature_flags,
            cycle_duration_seconds=0.05,
            trading_mode="SHORT_TERM",
        )
        assert result.entry_decision is None
        assert result.entry_stop_loss is None
        assert result.entry_volume is None

    def test_both_mode_still_allows_regular_new_entries(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        # trading_mode="BOTH" must NOT trip the SHORT_TERM-only gate —
        # the regular strategy keeps evaluating new entries normally.
        context = self._idle_context()
        snapshot = self._snapshot()
        result = orchestrator.run_bar_close_cycle(
            context,
            snapshot,
            baselines,
            constraints,
            feature_flags,
            cycle_duration_seconds=0.05,
            trading_mode="BOTH",
        )
        assert result.entry_decision is not None
        assert result.entry_decision.direction == "BUY"

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
        assert result.short_term_entry_stop_loss == pytest.approx(2010.0 - 5.0)  # 1x ATR
        assert result.short_term_entry_take_profit == pytest.approx(2010.0 + 5.0)  # 1x ATR
        assert result.short_term_entry_volume == constraints.volume_min

    def test_small_equity_caps_stop_loss_tighter_than_atr(
        self,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        # equity=20.0, SHORT_TERM_RISK_FRACTION_OF_EQUITY=0.10 -> equity-based
        # distance = 20.0 * 0.10 = 2.0 (tick_value=1.0/tick_size=0.01/
        # volume_min=0.01 from the `constraints` fixture), tighter than the
        # snapshot's fixed atr_value=5.0 -> 1x ATR = 5.0: the equity cap
        # must win, not the ATR distance. Baselines are set to match this
        # small equity (not the class's shared 10_000.0 fixture) so the
        # drawdown FSM doesn't itself HARD_LOCK on an apparent ~99.8% drop.
        position = self._position()
        context = self._in_position_context(position)
        snapshot = self._snapshot(
            equity=20.0, trend_direction="BEARISH", short_term_trend_direction="BULLISH"
        )
        small_baselines = EquityBaselines(20.0, 20.0, 20.0)
        result = orchestrator.run_bar_close_cycle(
            context,
            snapshot,
            small_baselines,
            constraints,
            feature_flags,
            cycle_duration_seconds=0.05,
            trading_mode="SHORT_TERM",
        )
        assert result.short_term_entry_decision is not None
        assert result.short_term_entry_decision.direction == "BUY"
        assert result.short_term_entry_stop_loss == pytest.approx(2010.0 - 2.0)
        assert result.short_term_entry_take_profit == pytest.approx(2010.0 + 2.0)

    def test_large_equity_leaves_atr_distance_unaffected(
        self,
        baselines: EquityBaselines,
        constraints: orchestrator.SymbolConstraints,
        feature_flags: FeatureFlagManager,
    ) -> None:
        # equity=10_000.0 (the _snapshot() default) -> equity-based distance
        # = 1_000.0, far looser than atr_value=5.0 -> 1x ATR = 5.0: ATR must
        # keep governing, same behavior as before this cap was added.
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
        assert result.short_term_entry_stop_loss == pytest.approx(2010.0 - 5.0)
        assert result.short_term_entry_take_profit == pytest.approx(2010.0 + 5.0)

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


class TestFsmContextToDict:
    """`main._fsm_context_to_dict()` — the serialization half of
    persisting `partial_closed`/`breakeven_set` across a restart
    (docs/ARCHITECTURE_SUMMARY.md §5's now-fixed gap)."""

    def _position(self, **overrides: object) -> orchestrator.PositionState:
        defaults: dict[str, object] = dict(
            ticket=42,
            symbol="XAUUSD",
            side="BUY",
            volume=0.10,
            entry_price=2000.0,
            stop_loss=1990.0,
            magic_number=555,
            partial_closed=True,
            breakeven_set=True,
        )
        defaults.update(overrides)
        return orchestrator.PositionState(**defaults)  # type: ignore[arg-type]

    def test_serializes_position_fields(self) -> None:
        context = orchestrator.FSMContext(
            state=orchestrator.TradingState.IN_POSITION,
            position=self._position(),
            drawdown_state=DrawdownState.ACTIVE,
            drawdown_reason=None,
        )
        result = orchestrator._fsm_context_to_dict(context)
        assert result["state"] == "IN_POSITION"
        assert result["drawdown_state"] == "ACTIVE"
        assert result["drawdown_reason"] is None
        assert result["position"] == {
            "ticket": 42,
            "symbol": "XAUUSD",
            "side": "BUY",
            "volume": 0.10,
            "entry_price": 2000.0,
            "stop_loss": 1990.0,
            "magic_number": 555,
            "partial_closed": True,
            "breakeven_set": True,
        }

    def test_serializes_none_position_as_none(self) -> None:
        context = orchestrator.FSMContext(
            state=orchestrator.TradingState.IDLE,
            position=None,
            drawdown_state=DrawdownState.ACTIVE,
            drawdown_reason=None,
        )
        result = orchestrator._fsm_context_to_dict(context)
        assert result["position"] is None

    def test_result_is_json_serializable(self) -> None:
        import json

        context = orchestrator.FSMContext(
            state=orchestrator.TradingState.IN_POSITION,
            position=self._position(),
            drawdown_state=DrawdownState.SOFT_LOCK,
            drawdown_reason="7% intraday drawdown",
        )
        json.dumps(orchestrator._fsm_context_to_dict(context))  # must not raise


# ---------------------------------------------------------------------------
# monitoring/telegram_bot.py
# ---------------------------------------------------------------------------


class TestFormatStatusMessage:
    def test_none_heartbeat_reports_never_run(self) -> None:
        message = format_status_message(None, now=datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc))
        assert "ยังไม่เคยพบ" in message

    def test_fresh_heartbeat_reports_running(self) -> None:
        now = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
        heartbeat = HeartbeatInfo(
            trading_mode="SHORT_TERM", last_heartbeat_utc="2026-07-09T11:58:00.000000Z"
        )
        message = format_status_message(heartbeat, now=now)
        assert "กำลังทำงาน" in message
        assert "SHORT_TERM" in message

    def test_stale_heartbeat_reports_not_running(self) -> None:
        now = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
        heartbeat = HeartbeatInfo(
            trading_mode="BOTH", last_heartbeat_utc="2026-07-09T11:00:00.000000Z"
        )
        message = format_status_message(heartbeat, now=now)
        assert "หยุดทำงาน" in message
        assert "BOTH" in message

    def test_boundary_exactly_at_stale_after_is_still_alive(self) -> None:
        now = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
        heartbeat = HeartbeatInfo(
            trading_mode="SHORT_TERM",
            last_heartbeat_utc=(now - HEARTBEAT_STALE_AFTER).isoformat().replace("+00:00", "Z"),
        )
        message = format_status_message(heartbeat, now=now)
        assert "กำลังทำงาน" in message

    def test_one_second_past_stale_after_is_not_alive(self) -> None:
        now = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
        heartbeat = HeartbeatInfo(
            trading_mode="SHORT_TERM",
            last_heartbeat_utc=(now - HEARTBEAT_STALE_AFTER - timedelta(seconds=1))
            .isoformat()
            .replace("+00:00", "Z"),
        )
        message = format_status_message(heartbeat, now=now)
        assert "หยุดทำงาน" in message

    def test_process_confirmed_dead_reports_stopped_even_with_fresh_heartbeat(self) -> None:
        # The exact /killbot scenario: PID confirmed gone within seconds,
        # long before the heartbeat itself would ever go stale.
        now = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
        heartbeat = HeartbeatInfo(
            trading_mode="SHORT_TERM", last_heartbeat_utc="2026-07-09T11:59:30.000000Z"
        )
        message = format_status_message(heartbeat, now=now, process_alive=False)
        assert "หยุดทำงาน" in message
        assert "กำลังทำงาน" not in message

    def test_process_confirmed_alive_with_fresh_heartbeat_reports_running(self) -> None:
        now = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
        heartbeat = HeartbeatInfo(
            trading_mode="SHORT_TERM", last_heartbeat_utc="2026-07-09T11:59:30.000000Z"
        )
        message = format_status_message(heartbeat, now=now, process_alive=True)
        assert "กำลังทำงาน" in message

    def test_process_alive_none_falls_back_to_heartbeat_staleness(self) -> None:
        now = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
        heartbeat = HeartbeatInfo(
            trading_mode="SHORT_TERM",
            last_heartbeat_utc=(now - HEARTBEAT_STALE_AFTER - timedelta(seconds=1))
            .isoformat()
            .replace("+00:00", "Z"),
        )
        message = format_status_message(heartbeat, now=now, process_alive=None)
        assert "หยุดทำงาน" in message


class TestIsMainProcessAlive:
    def test_returns_none_on_non_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(telegram_bot.platform, "system", lambda: "Linux")
        assert telegram_bot._is_main_process_alive() is None

    def test_returns_none_when_pid_file_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(telegram_bot.platform, "system", lambda: "Windows")
        monkeypatch.setattr(telegram_bot, "MAIN_PID_PATH", tmp_path / "nonexistent.pid")
        assert telegram_bot._is_main_process_alive() is None

    def test_returns_true_when_pid_matches_main_py(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid_file = tmp_path / "main.pid"
        pid_file.write_text("4242")
        monkeypatch.setattr(telegram_bot.platform, "system", lambda: "Windows")
        monkeypatch.setattr(telegram_bot, "MAIN_PID_PATH", pid_file)
        monkeypatch.setattr(
            telegram_bot,
            "subprocess",
            type(
                "FakeSubprocess",
                (),
                {
                    "run": staticmethod(
                        lambda *a, **k: subprocess.CompletedProcess(
                            args=[], returncode=0, stdout="D:\\...\\python.exe main.py", stderr=""
                        )
                    )
                },
            ),
        )
        assert telegram_bot._is_main_process_alive() is True

    def test_returns_false_when_pid_no_longer_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid_file = tmp_path / "main.pid"
        pid_file.write_text("4242")
        monkeypatch.setattr(telegram_bot.platform, "system", lambda: "Windows")
        monkeypatch.setattr(telegram_bot, "MAIN_PID_PATH", pid_file)
        monkeypatch.setattr(
            telegram_bot,
            "subprocess",
            type(
                "FakeSubprocess",
                (),
                {
                    "run": staticmethod(
                        lambda *a, **k: subprocess.CompletedProcess(
                            args=[], returncode=0, stdout="", stderr=""
                        )
                    )
                },
            ),
        )
        assert telegram_bot._is_main_process_alive() is False


class TestFormatOrderHistoryMessage:
    def _open_trade(self, client_order_id: str = "co-1") -> TradeLedgerEntry:
        return TradeLedgerEntry(
            client_order_id=client_order_id,
            symbol="XAUUSD",
            side="BUY",
            volume_lots=0.01,
            status="OPEN",
            opened_at_utc="2026-07-09T15:10:00.000000Z",
        )

    def _closed_trade(self, client_order_id: str = "co-2", profit: float = 5.1) -> TradeLedgerEntry:
        return TradeLedgerEntry(
            client_order_id=client_order_id,
            symbol="XAUUSD",
            side="SELL",
            volume_lots=0.02,
            status="CLOSED",
            opened_at_utc="2026-07-09T14:32:00.000000Z",
            closed_at_utc="2026-07-09T14:42:00.000000Z",
            close_price=2011.0,
            profit=profit,
        )

    def test_empty_list_reports_no_history(self) -> None:
        message = format_order_history_message([])
        assert "ยังไม่มีประวัติ" in message

    def test_includes_side_volume_symbol(self) -> None:
        message = format_order_history_message([self._open_trade()])
        assert "BUY 0.01 XAUUSD" in message

    def test_open_trade_shows_open_status_no_profit(self) -> None:
        message = format_order_history_message([self._open_trade()])
        assert "OPEN" in message
        assert "กำไร" not in message

    def test_closed_trade_shows_closed_status_and_profit(self) -> None:
        message = format_order_history_message([self._closed_trade(profit=5.1)])
        assert "CLOSED" in message
        assert "+5.10" in message

    def test_negative_profit_shows_minus_sign(self) -> None:
        message = format_order_history_message([self._closed_trade(profit=-2.17)])
        assert "-2.17" in message

    def test_closed_trade_shows_both_opened_and_closed_timestamps(self) -> None:
        message = format_order_history_message([self._closed_trade()])
        assert "2026-07-09 14:32:00 UTC" in message
        assert "2026-07-09 14:42:00 UTC" in message

    def test_open_trade_shows_only_opened_timestamp(self) -> None:
        message = format_order_history_message([self._open_trade()])
        assert "2026-07-09 15:10:00 UTC" in message
        assert "| ปิด:" not in message

    def test_multiple_trades_are_numbered_in_input_order(self) -> None:
        message = format_order_history_message(
            [self._open_trade("co-a"), self._closed_trade("co-b")]
        )
        lines = message.splitlines()
        assert any(line.startswith("1.") for line in lines)
        assert any(line.startswith("2.") for line in lines)

    def test_client_order_id_never_appears_in_output(self) -> None:
        message = format_order_history_message([self._open_trade("secret-internal-id")])
        assert "secret-internal-id" not in message


class TestFormatAccountMessage:
    """`/check`'s account-summary section (`monitoring/telegram_bot.py`)."""

    def _account(self, **overrides: object) -> gw.AccountState:
        defaults: dict[str, object] = dict(
            balance=9.45,
            equity=9.45,
            margin_used=0.0,
            margin_free=9.45,
            as_of_utc=datetime(2026, 7, 16, 7, 25, tzinfo=timezone.utc),
            leverage=1000,
            floating_profit=0.0,
        )
        defaults.update(overrides)
        return gw.AccountState(**defaults)  # type: ignore[arg-type]

    def _position(self, **overrides: object) -> gw.BrokerPosition:
        defaults: dict[str, object] = dict(
            ticket=1,
            symbol="XAUUSD",
            side="SELL",
            volume=0.01,
            price_open=4096.17,
            price_current=4092.9,
            stop_loss=0.0,
            take_profit=0.0,
            profit=3.27,
            magic=0,
            opened_at_utc=datetime(2026, 7, 16, 6, 0, tzinfo=timezone.utc),
        )
        defaults.update(overrides)
        return gw.BrokerPosition(**defaults)  # type: ignore[arg-type]

    def test_reports_balance_equity_margin_leverage(self) -> None:
        message = format_account_message(self._account(), [])
        assert "Balance: $9.45" in message
        assert "Equity: $9.45" in message
        assert "Margin ใช้ไป: $0.00" in message
        assert "Margin ว่าง: $9.45" in message
        assert "Leverage: 1:1000" in message

    def test_no_positions_reports_none(self) -> None:
        message = format_account_message(self._account(), [])
        assert "Position เปิดอยู่: ไม่มี" in message

    def test_positive_floating_profit_shows_plus_sign(self) -> None:
        message = format_account_message(self._account(floating_profit=12.5), [])
        assert "กำไร/ขาดทุนลอย: +$12.50" in message

    def test_negative_floating_profit_shows_minus_sign(self) -> None:
        message = format_account_message(self._account(floating_profit=-3.2), [])
        assert "กำไร/ขาดทุนลอย: -$3.20" in message

    def test_open_positions_are_listed_regardless_of_magic(self) -> None:
        # magic=0 is a manually-opened trade the bot itself never places —
        # /check's account overview must still surface it.
        message = format_account_message(self._account(), [self._position(magic=0)])
        assert "Position เปิดอยู่: มี 1 รายการ" in message
        assert "SELL 0.01 XAUUSD" in message
        assert "magic 0" in message

    def test_multiple_open_positions_are_each_listed(self) -> None:
        message = format_account_message(
            self._account(),
            [self._position(ticket=1, magic=123456), self._position(ticket=2, magic=654321)],
        )
        assert "Position เปิดอยู่: มี 2 รายการ" in message
        assert "magic 123456" in message
        assert "magic 654321" in message


class TestFormatConditionMessage:
    """`/condition`'s entry-gate checklist (`monitoring/telegram_bot.py`)."""

    def _trend(self, **overrides: object) -> TrendAlignment:
        defaults: dict[str, object] = dict(
            direction="BEARISH",
            d1_bullish=False,
            h4_bullish=False,
            h1_bullish=False,
            adx_value=18.1,
            adx_confirmed=False,
        )
        defaults.update(overrides)
        return TrendAlignment(**defaults)  # type: ignore[arg-type]

    def _breakout(self, direction: Direction = "NONE") -> BreakoutSignal:
        return BreakoutSignal(
            direction=direction, breakout_distance_points=0.0, volume_confirmed=False
        )

    def _pullback(self, direction: Direction = "NONE") -> PullbackSignal:
        return PullbackSignal(direction=direction, reference_level=4040.0)

    def _wick_fill(self, rejection: Direction = "NONE") -> WickFillResult:
        return WickFillResult(upper_shadow_ratio=0.4, lower_shadow_ratio=0.3, rejection=rejection)

    def _message(self, **overrides: object) -> str:
        defaults: dict[str, object] = dict(
            is_locked=False,
            drawdown_state_label="ACTIVE",
            has_position=False,
            trend=self._trend(),
            breakout=self._breakout(),
            pullback=self._pullback(),
            wick_fill=self._wick_fill(),
            adx_trend_threshold=25.0,
        )
        defaults.update(overrides)
        return format_condition_message(**defaults)  # type: ignore[arg-type]

    def test_locked_drawdown_shows_failing_mark(self) -> None:
        message = self._message(is_locked=True, drawdown_state_label="HARD_LOCK")
        assert "❌ Drawdown ไม่ติดล็อก (สถานะ: HARD_LOCK)" in message

    def test_unlocked_drawdown_shows_passing_mark(self) -> None:
        message = self._message(is_locked=False, drawdown_state_label="ACTIVE")
        assert "✅ Drawdown ไม่ติดล็อก (สถานะ: ACTIVE)" in message

    def test_existing_position_shows_failing_mark(self) -> None:
        message = self._message(has_position=True)
        assert "❌ ไม่มี position เปิดอยู่แล้ว" in message

    def test_no_position_shows_passing_mark(self) -> None:
        message = self._message(has_position=False)
        assert "✅ ไม่มี position เปิดอยู่แล้ว" in message

    def test_none_direction_trend_shows_failing_mark(self) -> None:
        message = self._message(trend=self._trend(direction="NONE"))
        assert "❌ เทรนด์ D1+H4+H1 ตรงกัน (ปัจจุบัน: NONE)" in message

    def test_aligned_trend_shows_passing_mark(self) -> None:
        message = self._message(trend=self._trend(direction="BEARISH"))
        assert "✅ เทรนด์ D1+H4+H1 ตรงกัน (ปัจจุบัน: BEARISH)" in message

    def test_adx_not_confirmed_shows_failing_mark(self) -> None:
        message = self._message(trend=self._trend(adx_value=18.1, adx_confirmed=False))
        assert "❌ ADX ยืนยันเทรนด์แรงพอ (18.1 / ต้อง ≥ 25.0)" in message

    def test_adx_confirmed_shows_passing_mark(self) -> None:
        message = self._message(trend=self._trend(adx_value=30.0, adx_confirmed=True))
        assert "✅ ADX ยืนยันเทรนด์แรงพอ (30.0 / ต้อง ≥ 25.0)" in message

    def test_no_trigger_signal_shows_failing_mark(self) -> None:
        message = self._message(
            breakout=self._breakout("NONE"),
            pullback=self._pullback("NONE"),
            wick_fill=self._wick_fill("NONE"),
        )
        assert "❌ มี trigger signal" in message

    def test_breakout_trigger_shows_passing_mark(self) -> None:
        message = self._message(breakout=self._breakout("BUY"))
        assert "✅ มี trigger signal" in message

    def test_pullback_trigger_shows_passing_mark(self) -> None:
        message = self._message(pullback=self._pullback("SELL"))
        assert "✅ มี trigger signal" in message

    def test_wick_fill_trigger_shows_passing_mark(self) -> None:
        message = self._message(wick_fill=self._wick_fill("BUY"))
        assert "✅ มี trigger signal" in message


class TestSummarizeIndicatorSignal:
    """The simple additive-vote BUY/SELL/HOLD heuristic
    (`monitoring/telegram_bot.py`) — separate from, and never fed into,
    `main.py`'s actual entry decision."""

    def _votes(self, **overrides: float) -> dict[str, float]:
        defaults: dict[str, float] = dict(
            current_price=2000.0,
            ma_value=1990.0,
            rsi_value=50.0,
            macd_histogram=0.2,
            bollinger_upper=2010.0,
            bollinger_lower=1970.0,
        )
        defaults.update(overrides)
        return defaults

    def test_all_bullish_votes_score_four_and_buy(self) -> None:
        label, score = summarize_indicator_signal(
            **self._votes(
                current_price=1965.0,  # above MA is impossible if also <= lower band and MA=1990
                ma_value=1960.0,
                rsi_value=25.0,
                macd_histogram=0.5,
                bollinger_lower=1970.0,
            )
        )
        assert score == 4
        assert label == "BUY"

    def test_all_bearish_votes_score_negative_four_and_sell(self) -> None:
        label, score = summarize_indicator_signal(
            **self._votes(
                current_price=2015.0,
                ma_value=2020.0,
                rsi_value=80.0,
                macd_histogram=-0.5,
                bollinger_upper=2010.0,
            )
        )
        assert score == -4
        assert label == "SELL"

    def test_ties_resolve_bullish_for_ma_and_macd(self) -> None:
        # MA (>=) and MACD (>=) both resolve bullish on an exact tie;
        # RSI/Bollinger stay neutral (0) here -> net score = 2, not a
        # true 4-way tie.
        label, score = summarize_indicator_signal(
            **self._votes(
                current_price=2000.0,
                ma_value=2000.0,
                rsi_value=50.0,
                macd_histogram=0.0,
                bollinger_upper=2010.0,
                bollinger_lower=1990.0,
            )
        )
        assert score == 2
        assert label == "BUY"

    def test_opposing_votes_cancel_to_hold(self) -> None:
        # MA bullish (+1), RSI overbought bearish (-1), MACD bearish (-1),
        # Bollinger oversold bullish (+1) -> net zero.
        label, score = summarize_indicator_signal(
            current_price=2000.0,
            ma_value=1990.0,
            rsi_value=80.0,
            macd_histogram=-0.1,
            bollinger_upper=2020.0,
            bollinger_lower=2000.0,
        )
        assert score == 0
        assert label == "HOLD"

    def test_ma_vote_bullish_when_price_at_or_above(self) -> None:
        _, score_above = summarize_indicator_signal(
            **self._votes(current_price=2000.0, ma_value=1990.0)
        )
        _, score_below = summarize_indicator_signal(
            **self._votes(current_price=1980.0, ma_value=1990.0)
        )
        assert score_above > score_below

    def test_rsi_oversold_votes_bullish(self) -> None:
        _, score = summarize_indicator_signal(**self._votes(rsi_value=20.0))
        _, neutral_score = summarize_indicator_signal(**self._votes(rsi_value=50.0))
        assert score > neutral_score

    def test_rsi_overbought_votes_bearish(self) -> None:
        _, score = summarize_indicator_signal(**self._votes(rsi_value=80.0))
        _, neutral_score = summarize_indicator_signal(**self._votes(rsi_value=50.0))
        assert score < neutral_score

    def test_min_abs_score_default_matches_prior_behavior(self) -> None:
        # score=1 (only MA bullish, everything else neutral) -> BUY under
        # the default min_abs_score=1, exactly as before this param existed.
        _, cancelled_score = summarize_indicator_signal(
            current_price=2000.0,
            ma_value=1990.0,
            rsi_value=50.0,
            macd_histogram=-0.1,
            bollinger_upper=2010.0,
            bollinger_lower=1970.0,
        )
        assert cancelled_score == 0  # MA (+1) and MACD (-1) cancel
        label, score = summarize_indicator_signal(**self._votes(macd_histogram=0.1))
        assert score == 2  # MA (+1) + MACD (+1), RSI/Bollinger neutral
        assert label == "BUY"

    def test_min_abs_score_2_requires_stronger_agreement(self) -> None:
        # MA(+1) + MACD(+1, zero-histogram tie resolves bullish), RSI/
        # Bollinger neutral -> score=2. Passes the default min_abs_score=1
        # gate but not a stricter min_abs_score=3.
        votes = self._votes(
            ma_value=1990.0,
            rsi_value=50.0,
            macd_histogram=0.0,
            bollinger_upper=2010.0,
            bollinger_lower=1970.0,
        )
        label_default, score = summarize_indicator_signal(**votes)
        assert score == 2
        label_strict, _ = summarize_indicator_signal(**votes, min_abs_score=3)
        assert label_default == "BUY"
        assert label_strict == "HOLD"


class TestFormatIndicatorMessage:
    """`/condition`'s monitoring-only MA/RSI/MACD/Bollinger readout
    (`monitoring/telegram_bot.py`) — never consulted by any trading
    decision, purely informational."""

    def _message(self, **overrides: object) -> str:
        defaults: dict[str, object] = dict(
            current_price=2000.0,
            ma_period=20,
            ma_value=1990.0,
            rsi_period=14,
            rsi_value=50.0,
            macd_line=0.5,
            macd_signal=0.3,
            macd_histogram=0.2,
            bollinger_period=20,
            bollinger_upper=2010.0,
            bollinger_middle=1990.0,
            bollinger_lower=1970.0,
        )
        defaults.update(overrides)
        return format_indicator_message(**defaults)  # type: ignore[arg-type]

    def test_reports_ma_rsi_macd_bollinger_values(self) -> None:
        message = self._message()
        assert "MA(20): 1990.00" in message
        assert "RSI(14): 50.0" in message
        assert "MACD: line 0.500 / signal 0.300 / histogram 0.200" in message
        assert "Bollinger Bands(20): upper 2010.00 / middle 1990.00 / lower 1970.00" in message

    def test_price_above_ma_reported(self) -> None:
        message = self._message(current_price=2000.0, ma_value=1990.0)
        assert "ราคาปัจจุบัน สูงกว่า MA" in message

    def test_price_below_ma_reported(self) -> None:
        message = self._message(current_price=1980.0, ma_value=1990.0)
        assert "ราคาปัจจุบัน ต่ำกว่า MA" in message

    def test_rsi_overbought_label(self) -> None:
        message = self._message(rsi_value=75.0)
        assert "overbought" in message

    def test_rsi_oversold_label(self) -> None:
        message = self._message(rsi_value=20.0)
        assert "oversold" in message

    def test_rsi_normal_label(self) -> None:
        message = self._message(rsi_value=50.0)
        assert "ปกติ" in message

    def test_macd_positive_histogram_shows_bullish_label(self) -> None:
        message = self._message(macd_histogram=0.5)
        assert "โมเมนตัมขาขึ้น" in message

    def test_macd_negative_histogram_shows_bearish_label(self) -> None:
        message = self._message(macd_histogram=-0.5)
        assert "โมเมนตัมขาลง" in message

    def test_price_above_upper_band_shows_overbought(self) -> None:
        message = self._message(current_price=2015.0, bollinger_upper=2010.0)
        assert "ชนแถบบน (overbought)" in message

    def test_price_below_lower_band_shows_oversold(self) -> None:
        message = self._message(current_price=1965.0, bollinger_lower=1970.0)
        assert "ชนแถบล่าง (oversold)" in message

    def test_price_within_bands_shows_middle(self) -> None:
        message = self._message(
            current_price=1995.0, bollinger_upper=2010.0, bollinger_lower=1970.0
        )
        assert "อยู่ในแถบกลาง" in message


class TestSignalValidationResult:
    """`backtester/signal_validation.py`'s aggregation dataclass — pure
    arithmetic over a plain tuple of `SignalPrediction`, independent of
    `validate_signal()`'s own indicator plumbing."""

    def _prediction(self, label: str, correct: bool, bar_index: int = 0) -> SignalPrediction:
        return SignalPrediction(bar_index=bar_index, label=label, score=1, correct=correct)

    def test_accuracy_matches_hand_calc(self) -> None:
        result = SignalValidationResult(
            predictions=(
                self._prediction("BUY", True),
                self._prediction("BUY", False),
                self._prediction("SELL", True),
                self._prediction("SELL", True),
            )
        )
        assert result.total == 4
        assert result.accuracy == pytest.approx(0.75)

    def test_accuracy_zero_for_no_predictions(self) -> None:
        result = SignalValidationResult(predictions=())
        assert result.total == 0
        assert result.accuracy == 0.0

    def test_accuracy_for_label_isolates_that_label_only(self) -> None:
        result = SignalValidationResult(
            predictions=(
                self._prediction("BUY", True),
                self._prediction("BUY", True),
                self._prediction("BUY", False),
                self._prediction("SELL", False),
            )
        )
        assert result.accuracy_for("BUY") == pytest.approx(2 / 3)
        assert result.accuracy_for("SELL") == 0.0

    def test_accuracy_for_label_zero_when_label_never_predicted(self) -> None:
        result = SignalValidationResult(predictions=(self._prediction("BUY", True),))
        assert result.accuracy_for("SELL") == 0.0


class TestValidateSignal:
    """`backtester/signal_validation.py`'s `validate_signal()` — end-to-end
    over small, deterministic synthetic price paths where the "correct"
    answer is known by construction."""

    def _bars(self, closes: list[float], *, start_hour: int = 0) -> gw.BarSeries:
        n = len(closes)
        start = datetime(2024, 1, 1, start_hour, tzinfo=timezone.utc)
        times = tuple(start + timedelta(hours=i) for i in range(n))
        close = np.array(closes, dtype=np.float64)
        return gw.BarSeries(
            open=close.copy(),
            high=close + 0.5,
            low=close - 0.5,
            close=close,
            tick_volume=np.full(n, 100.0),
            time_utc=times,
        )

    def test_noisy_uptrend_favors_correct_buy_predictions(self) -> None:
        # A noisy (not perfectly linear -- a perfectly linear series makes
        # MACD's histogram settle at exactly 0 once the EMAs reach their
        # steady-state lag offset, an unrealistic edge case that trips the
        # "ties resolve bullish" convention regardless of true direction)
        # steady uptrend: BUY predictions should be correct more often
        # than chance (>50%).
        rng = np.random.default_rng(11)
        closes = list(2000.0 + np.cumsum(rng.normal(0.5, 1.0, size=300)))
        result = validate_signal(self._bars(closes), horizon_bars=4, min_abs_score=1)
        assert result.total > 0
        assert result.accuracy_for("BUY") > 0.5

    def test_noisy_downtrend_favors_correct_sell_predictions(self) -> None:
        rng = np.random.default_rng(13)
        closes = list(2000.0 + np.cumsum(rng.normal(-0.5, 1.0, size=300)))
        result = validate_signal(self._bars(closes), horizon_bars=4, min_abs_score=1)
        assert result.total > 0
        assert result.accuracy_for("SELL") > 0.5

    def test_returns_empty_result_when_series_too_short_for_warmup(self) -> None:
        closes = [2000.0 + i for i in range(10)]
        result = validate_signal(self._bars(closes), horizon_bars=4)
        assert result.total == 0

    def test_start_index_end_index_restrict_the_scored_range(self) -> None:
        closes = [2000.0 + i * 0.5 for i in range(200)]
        bars = self._bars(closes)
        full = validate_signal(bars, horizon_bars=4, start_index=0, end_index=200)
        restricted = validate_signal(bars, horizon_bars=4, start_index=100, end_index=150)
        assert all(100 <= p.bar_index < 150 for p in restricted.predictions)
        assert restricted.total < full.total

    def test_min_abs_score_2_yields_fewer_or_equal_predictions_than_1(self) -> None:
        rng = np.random.default_rng(3)
        closes = list(2000.0 + np.cumsum(rng.normal(0, 2, size=300)))
        bars = self._bars(closes)
        loose = validate_signal(bars, horizon_bars=4, min_abs_score=1)
        strict = validate_signal(bars, horizon_bars=4, min_abs_score=2)
        assert strict.total <= loose.total


class TestBuildForwardLabels:
    """`backtester/signal_validation.py`'s `build_forward_labels()` — the
    forward-looking binary label shared by `validate_signal()` and
    `backtester/ml_signal_model.py`'s ML training target."""

    def test_matches_hand_calc(self) -> None:
        closes = np.array([1.0, 2.0, 1.5, 3.0, 0.5], dtype=np.float64)
        labels = build_forward_labels(closes, horizon_bars=2)
        # index i compares closes[i] vs closes[i+2], for i in [0, 3):
        # 1.0 vs 1.5 -> up, 2.0 vs 3.0 -> up, 1.5 vs 0.5 -> down
        np.testing.assert_array_equal(labels, [1.0, 1.0, 0.0])

    def test_raises_on_non_positive_horizon(self) -> None:
        closes = np.array([1.0, 2.0, 3.0], dtype=np.float64)
        with pytest.raises(ValueError, match="horizon_bars"):
            build_forward_labels(closes, horizon_bars=0)

    def test_returns_empty_when_series_not_longer_than_horizon(self) -> None:
        closes = np.array([1.0, 2.0], dtype=np.float64)
        labels = build_forward_labels(closes, horizon_bars=4)
        assert labels.shape == (0,)


class TestBuildFeatureMatrix:
    """`backtester/ml_signal_model.py`'s continuous-feature transform of
    the same 4 indicators `signal_validation._compute_indicator_arrays()`
    computes (already covered by `indicators/math_engine.py`'s own
    correctness tests elsewhere) — these tests check the *transform* on
    top of that output, not the underlying indicator math."""

    def _bars(self, closes: list[float]) -> gw.BarSeries:
        n = len(closes)
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        times = tuple(start + timedelta(hours=i) for i in range(n))
        close = np.array(closes, dtype=np.float64)
        return gw.BarSeries(
            open=close.copy(),
            high=close + 0.5,
            low=close - 0.5,
            close=close,
            tick_volume=np.full(n, 100.0),
            time_utc=times,
        )

    def test_transform_matches_hand_applied_formulas(self) -> None:
        from backtester.signal_validation import _compute_indicator_arrays

        rng = np.random.default_rng(7)
        closes = list(2000.0 + np.cumsum(rng.normal(0, 1.0, size=200)))
        bars = self._bars(closes)
        features, valid_mask = build_feature_matrix(bars)

        ma, rsi_values, macd_histogram, upper, lower = _compute_indicator_arrays(bars.close)
        expected_ma_distance = (bars.close - ma) / ma
        expected_macd_pct = macd_histogram / bars.close
        band_width = upper - lower
        expected_percent_b = np.where(
            band_width > 0, (bars.close - lower) / np.where(band_width > 0, band_width, 1.0), 0.5
        )
        valid = (
            ~np.isnan(ma)
            & ~np.isnan(rsi_values)
            & ~np.isnan(macd_histogram)
            & ~np.isnan(upper)
            & ~np.isnan(lower)
        )

        assert np.array_equal(valid_mask, valid)
        np.testing.assert_allclose(features[valid, 0], expected_ma_distance[valid])
        np.testing.assert_allclose(features[valid, 1], rsi_values[valid])
        np.testing.assert_allclose(features[valid, 2], expected_macd_pct[valid])
        np.testing.assert_allclose(features[valid, 3], expected_percent_b[valid])

    def test_valid_mask_false_only_during_warmup(self) -> None:
        rng = np.random.default_rng(9)
        closes = list(2000.0 + np.cumsum(rng.normal(0, 1.0, size=200)))
        _, valid_mask = build_feature_matrix(self._bars(closes))
        first_true = int(np.argmax(valid_mask))
        assert not valid_mask[:first_true].any()
        assert valid_mask[first_true:].all()

    def test_degenerate_zero_width_band_resolves_to_half(self) -> None:
        # A perfectly constant price makes Bollinger Bands' rolling stddev
        # (and thus band width) exactly zero for every window.
        closes = [2000.0] * 60
        features, valid_mask = build_feature_matrix(self._bars(closes))
        assert valid_mask.any()
        np.testing.assert_allclose(features[valid_mask, 3], 0.5)


class TestPrepareTrainingData:
    """`backtester/ml_signal_model.py`'s `prepare_training_data()` —
    trims indicator warm-up and the trailing unlabeled tail, and reports
    where the usable rows start in the original bar series."""

    def _bars(self, n: int, *, seed: int = 5) -> gw.BarSeries:
        rng = np.random.default_rng(seed)
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        times = tuple(start + timedelta(hours=i) for i in range(n))
        close = 2000.0 + np.cumsum(rng.normal(0, 1.0, size=n))
        return gw.BarSeries(
            open=close.copy(),
            high=close + 0.5,
            low=close - 0.5,
            close=close,
            tick_volume=np.full(n, 100.0),
            time_utc=times,
        )

    def test_x_and_y_have_matching_lengths(self) -> None:
        X, y, _ = prepare_training_data(self._bars(300), horizon_bars=4)
        assert X.shape[0] == y.shape[0]
        assert X.shape[1] == 4

    def test_first_valid_bar_index_matches_feature_matrix_warmup(self) -> None:
        bars = self._bars(300)
        _, valid_mask = build_feature_matrix(bars)
        X, y, first_valid_bar_index = prepare_training_data(bars, horizon_bars=4)
        expected_first_valid = int(np.argmax(valid_mask))
        assert first_valid_bar_index == expected_first_valid
        # Usable rows are contiguous from first_valid_bar_index through
        # (n - horizon_bars): row count must match exactly.
        assert len(y) == (300 - 4) - expected_first_valid

    def test_labels_are_binary(self) -> None:
        _, y, _ = prepare_training_data(self._bars(300), horizon_bars=4)
        assert set(np.unique(y).tolist()) <= {0.0, 1.0}


class TestBinomialCiLowerBound:
    """`backtester/ml_signal_model.py`'s `binomial_ci_lower_bound()` —
    normal-approximation 95% CI lower bound, used by the ML promotion
    bar's "not just a lucky OOS slice" check."""

    def test_matches_hand_calc(self) -> None:
        # accuracy=0.6, n=100: margin = 1.96 * sqrt(0.6*0.4/100) ~= 0.09601
        result = binomial_ci_lower_bound(0.6, 100)
        assert result == pytest.approx(0.6 - 1.96 * ((0.6 * 0.4 / 100) ** 0.5), abs=1e-9)

    def test_zero_or_negative_n_returns_zero(self) -> None:
        assert binomial_ci_lower_bound(0.9, 0) == 0.0
        assert binomial_ci_lower_bound(0.9, -5) == 0.0

    def test_wider_n_gives_a_tighter_bound(self) -> None:
        narrow = binomial_ci_lower_bound(0.6, 50)
        wide = binomial_ci_lower_bound(0.6, 5000)
        assert wide > narrow


class TestEvaluatePromotionBar:
    """`backtester/ml_signal_model.py`'s `evaluate_promotion_bar()` — all
    4 gates (CI lower bound > 50%, beats majority-class baseline, beats
    the naive heuristic, real ROC-AUC discrimination) must pass for a live
    wiring recommendation. Gate 4 exists specifically because gates 1-3
    alone let a genuinely degenerate model (regularized down to ~always
    predicting the majority class) through on real data — see
    `evaluate_promotion_bar()`'s docstring."""

    def _report(
        self,
        *,
        lr_accuracy: float,
        n_oos: int,
        majority_baseline: float,
        naive_heuristic_accuracy: float,
        lr_roc_auc: float = 0.60,
    ) -> MLValidationReport:
        lr_evaluation = ModelEvaluation(
            oos_accuracy=lr_accuracy, oos_precision=0.5, oos_recall=0.5, oos_roc_auc=lr_roc_auc
        )
        return MLValidationReport(
            feature_names=("a", "b", "c", "d"),
            horizon_bars=4,
            n_train=1000,
            n_oos=n_oos,
            majority_class_baseline=majority_baseline,
            naive_heuristic_oos_accuracy=naive_heuristic_accuracy,
            logistic_regression=LogisticRegressionEvaluation(
                evaluation=lr_evaluation,
                raw_space_coefficients=(0.1, 0.2, 0.3, 0.4),
                raw_space_intercept=0.0,
                selected_c=1.0,
            ),
            gradient_boosting=ModelEvaluation(
                oos_accuracy=0.5, oos_precision=0.5, oos_recall=0.5, oos_roc_auc=0.5
            ),
        )

    def test_passes_when_all_four_gates_clear(self) -> None:
        report = self._report(
            lr_accuracy=0.60, n_oos=5000, majority_baseline=0.51, naive_heuristic_accuracy=0.497
        )
        assert evaluate_promotion_bar(report) is True

    def test_fails_when_ci_lower_bound_at_or_below_half(self) -> None:
        # High accuracy but tiny sample: CI lower bound collapses toward 0.
        report = self._report(
            lr_accuracy=0.60, n_oos=5, majority_baseline=0.51, naive_heuristic_accuracy=0.497
        )
        assert evaluate_promotion_bar(report) is False

    def test_fails_when_not_beating_majority_class_baseline(self) -> None:
        report = self._report(
            lr_accuracy=0.55, n_oos=5000, majority_baseline=0.58, naive_heuristic_accuracy=0.497
        )
        assert evaluate_promotion_bar(report) is False

    def test_fails_when_not_beating_naive_heuristic(self) -> None:
        report = self._report(
            lr_accuracy=0.50, n_oos=5000, majority_baseline=0.49, naive_heuristic_accuracy=0.55
        )
        assert evaluate_promotion_bar(report) is False

    def test_fails_when_roc_auc_shows_no_real_discrimination(self) -> None:
        # The exact real-data scenario that motivated gate 4: accuracy
        # edges out the majority-class baseline and the naive heuristic,
        # the OOS sample is large enough for a tight CI, but ROC-AUC sits
        # at ~0.50 — a model that discriminates nothing, just nudged past
        # the base rate by heavy regularization.
        report = self._report(
            lr_accuracy=0.5200,
            n_oos=5931,
            majority_baseline=0.5198,
            naive_heuristic_accuracy=0.4974,
            lr_roc_auc=0.5007,
        )
        assert evaluate_promotion_bar(report) is False


class TestBuildNotifierFromEnv:
    """`monitoring/notifier.py`'s optional-by-design factory."""

    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_ID", raising=False)

    def test_returns_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        assert notifier_module.build_notifier_from_env() is None

    def test_returns_none_when_only_token_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        assert notifier_module.build_notifier_from_env() is None

    def test_returns_none_on_unparseable_chat_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "not-a-number")
        assert notifier_module.build_notifier_from_env() is None

    def test_builds_notifier_with_parsed_chat_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "111, 222")
        notifier = notifier_module.build_notifier_from_env()
        assert notifier is not None
        assert notifier._chat_ids == (111, 222)


class TestTelegramNotifierSend:
    """`TelegramNotifier.send()`'s never-raises contract — a notification
    failure must never take down the bar-close loop it reports on."""

    def test_send_swallows_every_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*args: object, **kwargs: object) -> object:
            raise RuntimeError("telegram is down")

        monkeypatch.setattr(notifier_module.requests, "post", boom)
        notifier = notifier_module.TelegramNotifier("123:ABC", (111,))
        notifier.send("hello")  # must not raise

    def test_send_posts_to_every_configured_chat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        posted: list[int] = []

        class FakeResponse:
            def raise_for_status(self) -> None:
                pass

        def fake_post(url: str, *, json: dict[str, object], timeout: float) -> FakeResponse:
            posted.append(int(str(json["chat_id"])))
            return FakeResponse()

        monkeypatch.setattr(notifier_module.requests, "post", fake_post)
        notifier = notifier_module.TelegramNotifier("123:ABC", (111, 222))
        notifier.send("hello")
        assert posted == [111, 222]

    def test_one_chat_failure_does_not_block_the_next(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        posted: list[int] = []

        class FakeResponse:
            def raise_for_status(self) -> None:
                pass

        def fake_post(url: str, *, json: dict[str, object], timeout: float) -> FakeResponse:
            if json["chat_id"] == 111:
                raise RuntimeError("first chat down")
            posted.append(int(str(json["chat_id"])))
            return FakeResponse()

        monkeypatch.setattr(notifier_module.requests, "post", fake_post)
        notifier_module.TelegramNotifier("123:ABC", (111, 222)).send("hello")
        assert posted == [222]


class TestFetchNewsEvents:
    """`main._fetch_news_events()` — the live loop's calendar wiring
    (previously a hardcoded `[]`, a documented §5 gap)."""

    def _event(self) -> EconomicEvent:
        return EconomicEvent(
            title="Non-Farm Payrolls",
            country="US",
            impact="HIGH",
            scheduled_at_utc=datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
        )

    def test_passes_chain_events_through(self) -> None:
        event = self._event()
        windows: list[tuple[datetime, datetime]] = []

        def fake_fetch(from_utc: datetime, to_utc: datetime) -> list[EconomicEvent]:
            windows.append((from_utc, to_utc))
            return [event]

        container = SimpleNamespace(calendar_provider=SimpleNamespace(fetch_events=fake_fetch))
        events = orchestrator._fetch_news_events(container)  # type: ignore[arg-type]
        assert events == [event]
        # The fetch window is exactly the ±MACRO_BLACKOUT_WINDOW range the
        # lock evaluates — nothing wider is ever needed.
        (from_utc, to_utc), *_ = windows
        assert to_utc - from_utc == 2 * MACRO_BLACKOUT_WINDOW

    def test_degrades_to_empty_when_whole_chain_fails(self) -> None:
        def fake_fetch(from_utc: datetime, to_utc: datetime) -> list[EconomicEvent]:
            raise NewsFeedConnectionError("all providers exhausted")

        container = SimpleNamespace(calendar_provider=SimpleNamespace(fetch_events=fake_fetch))
        assert orchestrator._fetch_news_events(container) == []  # type: ignore[arg-type]


class TestReadMainPid:
    def test_returns_none_when_file_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(telegram_bot, "MAIN_PID_PATH", tmp_path / "nonexistent.pid")
        assert telegram_bot._read_main_pid() is None

    def test_parses_valid_pid(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pid_file = tmp_path / "main.pid"
        pid_file.write_text("12345")
        monkeypatch.setattr(telegram_bot, "MAIN_PID_PATH", pid_file)
        assert telegram_bot._read_main_pid() == 12345

    def test_returns_none_on_invalid_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid_file = tmp_path / "main.pid"
        pid_file.write_text("not-a-pid")
        monkeypatch.setattr(telegram_bot, "MAIN_PID_PATH", pid_file)
        assert telegram_bot._read_main_pid() is None


class TestKillMainProcess:
    """`/killbot`'s OS-level process termination — every `subprocess.run`
    call is mocked (no real process is ever touched in this suite)."""

    def _fake_completed_process(self, *, stdout: str = "", returncode: int = 0) -> object:
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")

    def test_non_windows_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(telegram_bot.platform, "system", lambda: "Linux")
        assert "Windows" in telegram_bot._kill_main_process()

    def test_missing_pid_file_reports_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(telegram_bot.platform, "system", lambda: "Windows")
        monkeypatch.setattr(telegram_bot, "MAIN_PID_PATH", tmp_path / "nonexistent.pid")
        assert "ไม่พบข้อมูล PID" in telegram_bot._kill_main_process()

    def test_stale_pid_not_matching_main_py_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid_file = tmp_path / "main.pid"
        pid_file.write_text("999")
        monkeypatch.setattr(telegram_bot.platform, "system", lambda: "Windows")
        monkeypatch.setattr(telegram_bot, "MAIN_PID_PATH", pid_file)
        monkeypatch.setattr(
            telegram_bot,
            "subprocess",
            type(
                "FakeSubprocess",
                (),
                {"run": staticmethod(lambda *a, **k: self._fake_completed_process(stdout=""))},
            ),
        )
        reply = telegram_bot._kill_main_process()
        assert "ไม่ใช่ main.py" in reply

    def test_matching_pid_is_killed_successfully(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid_file = tmp_path / "main.pid"
        pid_file.write_text("4242")
        monkeypatch.setattr(telegram_bot.platform, "system", lambda: "Windows")
        monkeypatch.setattr(telegram_bot, "MAIN_PID_PATH", pid_file)

        calls: list[list[str]] = []

        def fake_run(args: list[str], **kwargs: object) -> object:
            calls.append(args)
            if args[0] == "powershell":
                return self._fake_completed_process(stdout="D:\\...\\python.exe main.py")
            return self._fake_completed_process(returncode=0)

        monkeypatch.setattr(
            telegram_bot, "subprocess", type("FakeSubprocess", (), {"run": staticmethod(fake_run)})
        )
        reply = telegram_bot._kill_main_process()
        assert "หยุดการทำงานของบอททั้งหมดแล้ว" in reply
        assert any(args[0] == "taskkill" for args in calls)

    def test_taskkill_failure_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid_file = tmp_path / "main.pid"
        pid_file.write_text("4242")
        monkeypatch.setattr(telegram_bot.platform, "system", lambda: "Windows")
        monkeypatch.setattr(telegram_bot, "MAIN_PID_PATH", pid_file)

        def fake_run(args: list[str], **kwargs: object) -> object:
            if args[0] == "powershell":
                return self._fake_completed_process(stdout="D:\\...\\python.exe main.py")
            return subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="Access is denied"
            )

        monkeypatch.setattr(
            telegram_bot, "subprocess", type("FakeSubprocess", (), {"run": staticmethod(fake_run)})
        )
        reply = telegram_bot._kill_main_process()
        assert "ล้มเหลว" in reply


# ---------------------------------------------------------------------------
# backtester/historical_data.py, backtester/replay_gateway.py,
# analytics/performance.py (Phase 1 backtester)
# ---------------------------------------------------------------------------


def _bar_series(times: tuple[datetime, ...], closes: list[float] | None = None) -> gw.BarSeries:
    n = len(times)
    close = np.array(closes if closes is not None else [2000.0 + i for i in range(n)])
    return gw.BarSeries(
        open=close.copy(),
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        tick_volume=np.full(n, 100.0),
        time_utc=times,
    )


class TestAuditBarSeries:
    """`backtester/historical_data.py`'s mandatory data-quality gate
    (`docs/RESEARCH.md` §1)."""

    def test_clean_hourly_series_passes(self) -> None:
        start = datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc)  # Tuesday
        times = tuple(start + timedelta(hours=i) for i in range(10))
        audit_bar_series(_bar_series(times), timeframe_minutes=60)  # no raise

    def test_non_monotonic_timestamps_raise(self) -> None:
        start = datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc)
        times = (start, start - timedelta(hours=1), start + timedelta(hours=2))
        with pytest.raises(ValueError, match="non-monotonic"):
            audit_bar_series(_bar_series(times), timeframe_minutes=60)

    def test_duplicate_timestamps_raise(self) -> None:
        start = datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc)
        times = (start, start, start + timedelta(hours=1))
        with pytest.raises(ValueError, match="non-monotonic"):
            audit_bar_series(_bar_series(times), timeframe_minutes=60)

    def test_small_non_weekend_gap_logs_but_does_not_raise(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Tuesday 00:00 -> Tuesday 08:00 is a 8h gap on a 60m timeframe
        # (> 2x), with neither endpoint inside the weekend closure window,
        # but well under MAX_PLAUSIBLE_HOLIDAY_GAP (4 days) — treated as a
        # probable holiday closure and logged, not raised (confirmed live:
        # real XAUUSD H4 history has exactly this gap shape around
        # Christmas 2024).
        start = datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc)
        times = (start, start + timedelta(hours=8))
        with caplog.at_level(logging.WARNING):
            audit_bar_series(_bar_series(times), timeframe_minutes=60)  # no raise
        assert "probable holiday closure" in caplog.text

    def test_gap_at_or_beyond_holiday_bound_raises(self) -> None:
        # Monday 00:00 -> Friday 21:00 (same week, before Friday's own
        # 22:00 close) is a ~4d21h gap: not weekend-explained (ends before
        # close) and exceeds MAX_PLAUSIBLE_HOLIDAY_GAP (4 days) — too large
        # to plausibly be an ordinary holiday, so this still raises.
        start = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)  # Monday
        times = (start, start + timedelta(days=4, hours=21))
        with pytest.raises(ValueError, match="unexplained gap"):
            audit_bar_series(_bar_series(times), timeframe_minutes=60)

    def test_weekend_gap_does_not_raise(self) -> None:
        # Friday 21:00 UTC -> Sunday 23:00 UTC: a large gap fully explained
        # by the weekly weekend closure (Friday 22:00 -> Sunday 22:00 UTC).
        friday_close = datetime(2024, 1, 5, 21, 0, tzinfo=timezone.utc)
        sunday_reopen = datetime(2024, 1, 7, 23, 0, tzinfo=timezone.utc)
        times = (friday_close, sunday_reopen)
        audit_bar_series(_bar_series(times), timeframe_minutes=60)  # no raise

    def test_short_series_is_trivially_clean(self) -> None:
        audit_bar_series(
            _bar_series((datetime(2024, 1, 2, tzinfo=timezone.utc),)), timeframe_minutes=60
        )


class TestHistoricalReplayGateway:
    """`backtester/replay_gateway.py` — the seam that lets the simulator
    drive `main.py`'s real decision code against historical bars."""

    def _gateway(self) -> tuple[HistoricalReplayGateway, tuple[datetime, ...]]:
        start = datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc)
        h1_times = tuple(start + timedelta(hours=i) for i in range(20))
        h1_bars = _bar_series(h1_times)
        d1_bars = _bar_series(tuple(start + timedelta(days=i) for i in range(3)))
        h4_bars = _bar_series(tuple(start + timedelta(hours=4 * i) for i in range(5)))
        account = gw.AccountState(
            balance=1000.0,
            equity=1000.0,
            margin_used=0.0,
            margin_free=1000.0,
            as_of_utc=start,
        )
        replay = HistoricalReplayGateway(
            d1_bars=d1_bars, h4_bars=h4_bars, h1_bars=h1_bars, starting_account_state=account
        )
        return replay, h1_times

    def test_get_bars_returns_only_bars_closed_by_cursor(self) -> None:
        replay, h1_times = self._gateway()
        replay.advance_to(h1_times[5])
        bars = replay.get_bars(gw.TIMEFRAME_H1, 100)
        assert len(bars.close) == 6  # indices 0..5 inclusive
        assert bars.time_utc[-1] == h1_times[5]

    def test_get_bars_never_leaks_a_bar_after_the_cursor(self) -> None:
        replay, h1_times = self._gateway()
        replay.advance_to(h1_times[5])
        bars = replay.get_bars(gw.TIMEFRAME_H1, 100)
        assert all(t <= h1_times[5] for t in bars.time_utc)

    def test_get_bars_respects_count_limit(self) -> None:
        replay, h1_times = self._gateway()
        replay.advance_to(h1_times[10])
        bars = replay.get_bars(gw.TIMEFRAME_H1, 3)
        assert list(bars.time_utc) == list(h1_times[8:11])

    def test_advance_to_before_any_bar_returns_empty(self) -> None:
        replay, h1_times = self._gateway()
        replay.advance_to(h1_times[0] - timedelta(minutes=1))
        bars = replay.get_bars(gw.TIMEFRAME_H1, 10)
        assert len(bars.close) == 0

    def test_get_current_price_tracks_latest_closed_h1_bar(self) -> None:
        replay, h1_times = self._gateway()
        replay.advance_to(h1_times[3])
        expected = replay.get_bars(gw.TIMEFRAME_H1, 1).close[-1]
        assert replay.get_current_price() == expected

    def test_account_state_is_settable_and_read_back(self) -> None:
        replay, _ = self._gateway()
        new_state = gw.AccountState(
            balance=500.0,
            equity=480.0,
            margin_used=20.0,
            margin_free=460.0,
            as_of_utc=datetime.now(timezone.utc),
        )
        replay.account_state = new_state
        assert replay.get_account_state() == new_state


class TestPerformanceFormulas:
    """`analytics/performance.py`'s formulas, exactly per
    `docs/RESEARCH.md` §7."""

    def _curve(
        self, values: list[float], *, step: timedelta = timedelta(hours=1)
    ) -> list[tuple[datetime, float]]:
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        return [(start + i * step, v) for i, v in enumerate(values)]

    def test_compute_returns_matches_reference(self) -> None:
        curve = self._curve([100.0, 110.0, 99.0, 99.0])
        returns = compute_returns(curve)
        np.testing.assert_allclose(returns, [0.10, -0.10, 0.0], rtol=1e-9)

    def test_compute_returns_empty_for_short_curve(self) -> None:
        assert list(compute_returns(self._curve([100.0]))) == []

    def test_infer_periods_per_year_matches_hand_calc(self) -> None:
        # 366 points spanning exactly 365 days -> ~366 periods/year.
        curve = self._curve([100.0] * 366, step=timedelta(days=1))
        result = infer_periods_per_year(curve)
        assert result == pytest.approx(366 / (365 / 365.25), rel=1e-6)

    def test_infer_periods_per_year_zero_for_short_span(self) -> None:
        curve = self._curve([100.0, 101.0], step=timedelta(minutes=1))
        assert infer_periods_per_year(curve) == 0.0

    def test_sharpe_ratio_matches_hand_computation(self) -> None:
        returns = np.array([0.01, -0.02, 0.03, 0.0])
        expected = (returns.mean() - 0.0) / returns.std(ddof=0) * np.sqrt(252.0)
        assert sharpe_ratio(returns, periods_per_year=252.0) == pytest.approx(expected)

    def test_sharpe_ratio_zero_on_zero_variance(self) -> None:
        returns = np.array([0.01, 0.01, 0.01])
        assert sharpe_ratio(returns, periods_per_year=252.0) == 0.0

    def test_sharpe_ratio_zero_on_empty_returns(self) -> None:
        assert sharpe_ratio(np.array([]), periods_per_year=252.0) == 0.0

    def test_sortino_ratio_uses_only_downside_deviation(self) -> None:
        returns = np.array([0.05, 0.05, -0.01, -0.03])
        downside = np.minimum(returns, 0.0)
        expected = returns.mean() / downside.std(ddof=0) * np.sqrt(252.0)
        assert sortino_ratio(returns, periods_per_year=252.0) == pytest.approx(expected)

    def test_sortino_ratio_zero_when_never_negative(self) -> None:
        returns = np.array([0.01, 0.02, 0.03])
        assert sortino_ratio(returns, periods_per_year=252.0) == 0.0

    def test_cagr_doubling_over_one_year(self) -> None:
        curve = self._curve([100.0, 200.0], step=timedelta(days=365))
        assert cagr(curve) == pytest.approx(1.0, rel=1e-2)

    def test_cagr_zero_for_short_span(self) -> None:
        curve = self._curve([100.0, 101.0], step=timedelta(minutes=1))
        assert cagr(curve) == 0.0

    def test_max_drawdown_matches_hand_calc(self) -> None:
        curve = self._curve([100.0, 120.0, 90.0, 110.0])
        # Peak 120 -> trough 90: drawdown = (120-90)/120 = 0.25
        assert max_drawdown(curve) == pytest.approx(0.25)

    def test_max_drawdown_zero_for_monotonic_rise(self) -> None:
        curve = self._curve([100.0, 110.0, 120.0])
        assert max_drawdown(curve) == 0.0

    def test_max_drawdown_duration_matches_hand_calc(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        curve = [
            (start, 100.0),
            (start + timedelta(days=1), 120.0),  # new peak
            (start + timedelta(days=2), 90.0),  # underwater starts
            (start + timedelta(days=5), 110.0),  # still underwater (< 120)
            (start + timedelta(days=6), 130.0),  # new peak, underwater ends
        ]
        # Underwater from day 1 (peak time) through day 6 -> 5 days.
        assert max_drawdown_duration(curve) == timedelta(days=5)

    def test_mar_ratio_zero_when_no_drawdown(self) -> None:
        curve = self._curve([100.0, 110.0, 120.0])
        assert mar_ratio(curve) == 0.0

    def test_mar_ratio_matches_cagr_over_drawdown(self) -> None:
        curve = self._curve([100.0, 200.0, 150.0], step=timedelta(days=200))
        expected = cagr(curve) / max_drawdown(curve)
        assert mar_ratio(curve) == pytest.approx(expected)

    def test_profit_factor_matches_hand_calc(self) -> None:
        # gains=30, losses=abs(-10-5)=15 -> 30/15 = 2.0
        assert profit_factor([10.0, 20.0, -10.0, -5.0]) == pytest.approx(2.0)

    def test_profit_factor_infinite_with_no_losers(self) -> None:
        assert profit_factor([10.0, 20.0]) == float("inf")

    def test_profit_factor_zero_for_no_trades(self) -> None:
        assert profit_factor([]) == 0.0

    def test_win_rate_matches_hand_calc(self) -> None:
        assert win_rate([10.0, -5.0, 3.0, -1.0]) == pytest.approx(0.5)

    def test_win_rate_zero_for_no_trades(self) -> None:
        assert win_rate([]) == 0.0


class TestSkewnessKurtosis:
    """`analytics/performance.py`'s `skewness()`/`kurtosis()` — γ3/γ4 in
    `docs/RESEARCH.md` §3's Deflated Sharpe Ratio formula."""

    def test_symmetric_distribution_has_near_zero_skewness(self) -> None:
        # A symmetric set of returns around 0 has ~0 skewness.
        returns = np.array([-2.0, -1.0, 0.0, 1.0, 2.0])
        assert skewness(returns) == pytest.approx(0.0, abs=1e-9)

    def test_right_skewed_distribution_has_positive_skewness(self) -> None:
        returns = np.array([-1.0, -1.0, -1.0, -1.0, 5.0])
        assert skewness(returns) > 0.0

    def test_left_skewed_distribution_has_negative_skewness(self) -> None:
        returns = np.array([1.0, 1.0, 1.0, 1.0, -5.0])
        assert skewness(returns) < 0.0

    def test_skewness_zero_for_constant_series(self) -> None:
        assert skewness(np.array([1.0, 1.0, 1.0])) == 0.0

    def test_skewness_zero_for_short_series(self) -> None:
        assert skewness(np.array([1.0])) == 0.0

    def test_kurtosis_of_uniform_like_series_below_normal(self) -> None:
        # A uniform-ish (platykurtic) series reads below the normal
        # distribution's kurtosis of 3.0.
        returns = np.array([-2.0, -1.0, 0.0, 1.0, 2.0])
        assert kurtosis(returns) < 3.0

    def test_kurtosis_three_for_constant_series(self) -> None:
        # Degenerate (zero-variance) input reads as the normal-distribution
        # value — a neutral default, not an error.
        assert kurtosis(np.array([1.0, 1.0, 1.0])) == 3.0

    def test_kurtosis_three_for_short_series(self) -> None:
        assert kurtosis(np.array([1.0])) == 3.0


class TestDeflatedSharpeRatio:
    """`analytics/performance.py`'s `deflated_sharpe_ratio()`, exactly per
    `docs/RESEARCH.md` §3. Reference values independently computed via
    `math.erf` for Φ (not reusing `scipy.stats.norm.cdf`, so this is a
    genuine cross-check of the formula, not just of scipy) and
    `scipy.stats.norm.ppf` for Φ⁻¹ (trusted as a correct library primitive
    rather than reimplemented from scratch)."""

    def test_matches_independently_computed_reference_case_a(self) -> None:
        # sharpe=0.3, n_obs=20, skew=0.1, kurt=3.5, n_trials=10, var_sr=0.04
        # -> DSR ~= 0.4744 (worked by hand, see this phase's implementation
        # notes).
        result = deflated_sharpe_ratio(
            0.3,
            n_observations=20,
            skewness=0.1,
            kurtosis=3.5,
            n_trials=10,
            sharpe_variance_across_trials=0.04,
        )
        assert result == pytest.approx(0.4744070116757761, rel=1e-9)

    def test_more_trials_deflates_the_same_sharpe_further(self) -> None:
        # Same sharpe/observations/skew/kurt, only n_trials rises 10 -> 50:
        # DSR must fall (more trials = more chance the best-of-N looks good
        # by luck alone, so the deflation correction grows).
        dsr_10_trials = deflated_sharpe_ratio(
            0.3,
            n_observations=20,
            skewness=0.1,
            kurtosis=3.5,
            n_trials=10,
            sharpe_variance_across_trials=0.04,
        )
        dsr_50_trials = deflated_sharpe_ratio(
            0.3,
            n_observations=20,
            skewness=0.1,
            kurtosis=3.5,
            n_trials=50,
            sharpe_variance_across_trials=0.04,
        )
        assert dsr_50_trials == pytest.approx(0.25204958177051884, rel=1e-9)
        assert dsr_50_trials < dsr_10_trials

    def test_higher_sharpe_gives_higher_dsr_all_else_equal(self) -> None:
        low = deflated_sharpe_ratio(
            0.2,
            n_observations=50,
            skewness=0.0,
            kurtosis=3.0,
            n_trials=20,
            sharpe_variance_across_trials=0.05,
        )
        high = deflated_sharpe_ratio(
            1.0,
            n_observations=50,
            skewness=0.0,
            kurtosis=3.0,
            n_trials=20,
            sharpe_variance_across_trials=0.05,
        )
        assert high > low

    def test_zero_for_insufficient_observations(self) -> None:
        result = deflated_sharpe_ratio(
            1.0,
            n_observations=1,
            skewness=0.0,
            kurtosis=3.0,
            n_trials=10,
            sharpe_variance_across_trials=0.04,
        )
        assert result == 0.0

    def test_zero_for_insufficient_trials(self) -> None:
        result = deflated_sharpe_ratio(
            1.0,
            n_observations=50,
            skewness=0.0,
            kurtosis=3.0,
            n_trials=1,
            sharpe_variance_across_trials=0.04,
        )
        assert result == 0.0

    def test_zero_for_non_positive_trial_variance(self) -> None:
        result = deflated_sharpe_ratio(
            1.0,
            n_observations=50,
            skewness=0.0,
            kurtosis=3.0,
            n_trials=10,
            sharpe_variance_across_trials=0.0,
        )
        assert result == 0.0

    def test_zero_for_non_positive_denominator(self) -> None:
        # A large positive skew combined with a large sharpe can drive the
        # formula's inner square root negative: 1 - 10*10 + 0.5*100 = -49.
        result = deflated_sharpe_ratio(
            10.0,
            n_observations=50,
            skewness=10.0,
            kurtosis=3.0,
            n_trials=10,
            sharpe_variance_across_trials=0.04,
        )
        assert result == 0.0


class TestGenerateFolds:
    """`backtester/walk_forward.py`'s anchored WFO fold generation, exactly
    per `docs/RESEARCH.md` §4."""

    def test_train_start_is_always_the_anchor(self) -> None:
        anchor = datetime(2020, 1, 1, tzinfo=timezone.utc)
        data_end = datetime(2023, 6, 1, tzinfo=timezone.utc)
        folds = generate_folds(anchor, data_end, min_folds=1)
        assert all(f.train_start == anchor for f in folds)

    def test_train_end_advances_by_step_months_each_fold(self) -> None:
        anchor = datetime(2020, 1, 1, tzinfo=timezone.utc)
        data_end = datetime(2023, 6, 1, tzinfo=timezone.utc)
        folds = generate_folds(
            anchor, data_end, initial_train_months=24, step_months=1, min_folds=1
        )
        assert folds[0].train_end == datetime(2022, 1, 1, tzinfo=timezone.utc)
        assert folds[1].train_end == datetime(2022, 2, 1, tzinfo=timezone.utc)

    def test_test_start_is_train_end_plus_embargo(self) -> None:
        anchor = datetime(2020, 1, 1, tzinfo=timezone.utc)
        data_end = datetime(2023, 6, 1, tzinfo=timezone.utc)
        folds = generate_folds(
            anchor, data_end, initial_train_months=24, embargo_days=3, min_folds=1
        )
        assert folds[0].test_start == folds[0].train_end + timedelta(days=3)

    def test_test_end_is_test_start_plus_step_months(self) -> None:
        anchor = datetime(2020, 1, 1, tzinfo=timezone.utc)
        data_end = datetime(2023, 6, 1, tzinfo=timezone.utc)
        folds = generate_folds(
            anchor, data_end, initial_train_months=24, step_months=2, min_folds=1
        )
        assert folds[0].test_end == datetime(2022, 3, 2, tzinfo=timezone.utc)

    def test_stops_before_exceeding_data_end(self) -> None:
        anchor = datetime(2020, 1, 1, tzinfo=timezone.utc)
        data_end = datetime(2023, 6, 1, tzinfo=timezone.utc)
        folds = generate_folds(anchor, data_end, initial_train_months=24, min_folds=1)
        assert all(f.test_end <= data_end for f in folds)

    def test_raises_when_fewer_than_min_folds_fit(self) -> None:
        anchor = datetime(2020, 1, 1, tzinfo=timezone.utc)
        data_end = datetime(2020, 6, 1, tzinfo=timezone.utc)  # far too short
        with pytest.raises(ValueError, match="only 0 fold"):
            generate_folds(anchor, data_end, initial_train_months=24, min_folds=12)

    def test_default_settings_yield_at_least_12_folds_over_3_3_years(self) -> None:
        # Matches the real IC Markets history span this phase actually
        # validates against (2023-03-20 to ~2026-07).
        anchor = datetime(2023, 3, 20, tzinfo=timezone.utc)
        data_end = datetime(2026, 7, 17, tzinfo=timezone.utc)
        folds = generate_folds(anchor, data_end)
        assert len(folds) >= 12


class TestGenerateParameterGrid:
    """`backtester/walk_forward.py`'s parameter sweep grid."""

    def test_reduced_default_grid_has_35_combos(self) -> None:
        assert len(generate_parameter_grid()) == 35

    def test_full_spec_grid_has_144_combos(self) -> None:
        assert len(generate_parameter_grid(adx_step=1.0, trailing_step=0.25)) == 144

    def test_grid_bounds_are_respected(self) -> None:
        grid = generate_parameter_grid()
        adx_values = {adx for adx, _ in grid}
        trailing_values = {trailing for _, trailing in grid}
        assert min(adx_values) == pytest.approx(20.0)
        assert max(adx_values) == pytest.approx(35.0)
        assert min(trailing_values) == pytest.approx(1.0)
        assert max(trailing_values) == pytest.approx(3.0)

    def test_grid_has_no_duplicate_combos(self) -> None:
        grid = generate_parameter_grid()
        assert len(grid) == len(set(grid))


class TestSliceBars:
    """`backtester/walk_forward.py`'s `_slice_bars()` — the fold IS/OOS
    window cutter."""

    def _bars(self) -> gw.BarSeries:
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        times = tuple(start + timedelta(days=i) for i in range(10))
        close = np.arange(10, dtype=np.float64) + 100.0
        return gw.BarSeries(
            open=close.copy(),
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            tick_volume=np.full(10, 100.0),
            time_utc=times,
        )

    def test_slice_includes_both_boundaries(self) -> None:
        bars = self._bars()
        start = bars.time_utc[2]
        end = bars.time_utc[5]
        sliced = wf._slice_bars(bars, start, end)
        assert sliced.time_utc[0] == start
        assert sliced.time_utc[-1] == end
        assert len(sliced.close) == 4

    def test_slice_excludes_bars_outside_the_window(self) -> None:
        bars = self._bars()
        start = bars.time_utc[3]
        end = bars.time_utc[3]
        sliced = wf._slice_bars(bars, start, end)
        assert len(sliced.close) == 1
        assert sliced.close[0] == bars.close[3]

    def test_slice_returns_empty_when_window_outside_data(self) -> None:
        bars = self._bars()
        sliced = wf._slice_bars(
            bars,
            datetime(2025, 1, 1, tzinfo=timezone.utc),
            datetime(2025, 1, 2, tzinfo=timezone.utc),
        )
        assert len(sliced.close) == 0
