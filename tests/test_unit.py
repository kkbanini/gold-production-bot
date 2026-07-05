"""Unit tests: pure-function/logic-level coverage for every module with
business logic. No network, no live MT5 terminal, no external process —
anything that crosses a real boundary (HTTP, MT5, multi-module state
persistence) belongs in test_integration.py instead.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

import broker.mt5_gateway as gw
from config.config_manager import ConfigManager, ConfigurationError
from execution.position_manager import (
    PositionState,
    calculate_base_take_profit,
    calculate_trailing_stop,
    evaluate_partial_close_and_breakeven,
)
from indicators.math_engine import adx, atr, ema, sma
from news.news_engine import (
    EconomicEvent,
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
from risk.risk_manager import calculate_compounded_lot_size, clamp_lot_size
from storage.db_engine import checkpoint_wal, connect, initialize_schema
from storage.state_manager import TradeLedgerEntry
from strategy.execution_triggers import (
    analyze_wick_fill,
    detect_breakout,
    detect_pullback,
)
from strategy.trend_filter import evaluate_master_trend
from tests.conftest import FakeMT5, FakeSymbolInfo

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

    def test_non_integer_mt5_login_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_env(monkeypatch)
        for key, value in self.REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("MT5_LOGIN", "not-a-number")
        with pytest.raises(ConfigurationError, match="MT5_LOGIN"):
            ConfigManager.load(env_file="nonexistent.env")


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
