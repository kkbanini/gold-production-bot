"""Chaos tests (Phase 11e test-suite reorganization,
`docs/PRODUCTION_SPEC.md` §7): simulated fault/disruption scenarios —
MT5 server dropouts, socket/HTTP disconnections, and an abrupt process
crash — kept isolated from `tests/unit/`/`tests/integration/` so the
standard CI pipeline's fast feedback loop never depends on this suite.
Run explicitly via `pytest tests/chaos` (not part of `pyproject.toml`'s
default `testpaths`).

None of these are actually slow today (every sleep/socket call is
monkeypatched), but they are grouped here by *what* they simulate — a
fault or disruption — rather than by wall-clock duration, consistent
with `tests/stress/`'s complementary "load/volume" framing (currently an
empty placeholder — see `tests/stress/README.md`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

import broker.mt5_gateway as gw
import news.calendar_provider as calendar_provider
import news.news_engine as ne
from storage.state_manager import StateManager
from tests.conftest import FakeMT5, FakeSymbolInfo, FakeTick

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

    def test_network_calendar_provider_delegates_to_fetch_calendar_events(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`calendar_provider.NetworkCalendarProvider` (Phase 11b) is a thin
        wrapper around `fetch_calendar_events` — this exercises that
        delegation directly rather than only asserting its constructor
        fields (`tests/unit/test_unit.py::TestBuildCalendarProviderChain`)."""

        def fake_get(
            url: str,
            params: dict[str, str] | None = None,
            timeout: tuple[float, float] | None = None,
        ) -> FakeHTTPResponse:
            return FakeHTTPResponse(
                200,
                [
                    {
                        "title": "CPI y/y",
                        "country": "US",
                        "impact": "High",
                        "date": "2026-07-04T12:30:00+00:00",
                    }
                ],
            )

        monkeypatch.setattr(requests, "get", fake_get)
        provider = calendar_provider.NetworkCalendarProvider(
            name="finnhub",
            base_url="https://example.com/calendar",
            api_key="fake-key",
            connect_timeout_seconds=3.0,
            read_timeout_seconds=3.0,
        )
        events = provider.fetch_events(self.FROM_UTC, self.TO_UTC)
        assert [e.title for e in events] == ["CPI y/y"]

    def test_transient_failure_recovers_via_retry_before_falling_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Phase 11e's `resilience.backoff.retry_with_backoff()` integration:
        one connection error followed by success must resolve within the
        same provider, without the chain ever seeing a failure."""
        attempts = {"count": 0}

        def flaky_get(
            url: str,
            params: dict[str, str] | None = None,
            timeout: tuple[float, float] | None = None,
        ) -> FakeHTTPResponse:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise requests.exceptions.ConnectionError("socket refused")
            return FakeHTTPResponse(
                200,
                [
                    {
                        "title": "Retail Sales",
                        "country": "US",
                        "impact": "Medium",
                        "date": "2026-07-04T12:30:00+00:00",
                    }
                ],
            )

        monkeypatch.setattr(requests, "get", flaky_get)
        monkeypatch.setattr("resilience.backoff.time.sleep", lambda seconds: None)
        provider = calendar_provider.NetworkCalendarProvider(
            name="finnhub",
            base_url="https://example.com/calendar",
            api_key="fake-key",
            connect_timeout_seconds=3.0,
            read_timeout_seconds=3.0,
        )
        events = provider.fetch_events(self.FROM_UTC, self.TO_UTC)
        assert attempts["count"] == 2
        assert [e.title for e in events] == ["Retail Sales"]

    def test_persistent_failure_raises_news_feed_error_after_retry_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def always_fails(
            url: str,
            params: dict[str, str] | None = None,
            timeout: tuple[float, float] | None = None,
        ) -> FakeHTTPResponse:
            raise requests.exceptions.ConnectionError("socket refused")

        monkeypatch.setattr(requests, "get", always_fails)
        monkeypatch.setattr("resilience.backoff.time.sleep", lambda seconds: None)
        provider = calendar_provider.NetworkCalendarProvider(
            name="finnhub",
            base_url="https://example.com/calendar",
            api_key="fake-key",
            connect_timeout_seconds=3.0,
            read_timeout_seconds=3.0,
        )
        with pytest.raises(ne.NewsFeedConnectionError):
            provider.fetch_events(self.FROM_UTC, self.TO_UTC)


# ---------------------------------------------------------------------------
# Simulated abrupt process crash (storage/state_manager.py)
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
