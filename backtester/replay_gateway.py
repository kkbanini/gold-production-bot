"""`HistoricalReplayGateway`: a `main.MarketDataGateway`-satisfying stand-in
that replays pre-fetched historical bars instead of talking to a live MT5
terminal — the seam that lets `backtester/simulator.py` drive `main.py`'s
real `_fetch_market_snapshot()`/`run_bar_close_cycle()` decision code
against historical data (ADR-0004 §6's "same code the live system runs").

Never touches `MetaTrader5`/`broker/mt5_gateway.py`'s live connection —
all data is supplied once at construction (already fetched + audited by
`backtester/historical_data.py`) and consumed by advancing an internal,
monotonically-forward cursor (`advance_to()`); a backtest never rewinds.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, replace
from datetime import datetime

from broker.mt5_gateway import (
    TIMEFRAME_D1,
    TIMEFRAME_H1,
    TIMEFRAME_H4,
    AccountState,
    BarSeries,
    SymbolSpec,
)

# The real IC Markets XAUUSD spec, confirmed live (broker/mt5_gateway.py's
# MT5Gateway.symbol_spec on the connected account) — a fixed approximation
# for this phase (contract specs can drift over 3+ years of history; not
# modeled, same simplification as get_current_price()'s bar-close pricing
# below).
DEFAULT_XAUUSD_SYMBOL_SPEC = SymbolSpec(
    name="XAUUSD",
    point=0.01,
    digits=2,
    tick_value=1.0,
    tick_size=0.01,
    volume_min=0.01,
    volume_max=100.0,
    volume_step=0.01,
)


@dataclass(frozen=True, slots=True)
class _TimeframeCursor:
    """One timeframe's full bar series plus how many of its bars have
    closed as-of the replay cursor (`closed_count`) — the exclusive upper
    bound for slicing `[0:closed_count]`."""

    bars: BarSeries
    closed_count: int = 0


class HistoricalReplayGateway:
    """Satisfies `main.MarketDataGateway` structurally: `get_bars()`,
    `symbol_spec`, `get_account_state()`, `get_current_price()`.

    `account_state` is a plain public attribute the simulator overwrites
    directly every cycle (simulated equity/balance/margin) — this class
    has no opinion on P&L; it is purely a historical-data window plus a
    pass-through account-state holder.
    """

    def __init__(
        self,
        *,
        d1_bars: BarSeries,
        h4_bars: BarSeries,
        h1_bars: BarSeries,
        starting_account_state: AccountState,
        symbol_spec: SymbolSpec = DEFAULT_XAUUSD_SYMBOL_SPEC,
    ) -> None:
        self._cursors: dict[int, _TimeframeCursor] = {
            TIMEFRAME_D1: _TimeframeCursor(d1_bars),
            TIMEFRAME_H4: _TimeframeCursor(h4_bars),
            TIMEFRAME_H1: _TimeframeCursor(h1_bars),
        }
        self.symbol_spec = symbol_spec
        self.account_state = starting_account_state
        self._current_price = float(h1_bars.close[0]) if len(h1_bars.close) > 0 else 0.0

    def advance_to(self, cursor_time: datetime) -> None:
        """Advance every timeframe's closed-bar count to include every bar
        whose close time is `<= cursor_time`, and refresh
        `get_current_price()`'s value from the latest closed H1 bar.
        `bisect_right` over each series' ascending `time_utc` — a backtest
        only ever moves `cursor_time` forward, but each call is independent
        (no assumption of monotonic call order) since it's cheap either way.
        """
        for timeframe, cursor in self._cursors.items():
            closed_count = bisect_right(cursor.bars.time_utc, cursor_time)
            self._cursors[timeframe] = replace(cursor, closed_count=closed_count)

        h1_cursor = self._cursors[TIMEFRAME_H1]
        if h1_cursor.closed_count > 0:
            self._current_price = float(h1_cursor.bars.close[h1_cursor.closed_count - 1])

    def get_bars(self, timeframe: int, count: int) -> BarSeries:
        """The last `count` bars closed as-of the current cursor — mirrors
        `MT5Gateway.get_bars()`'s closed-bars-only contract exactly (no
        currently-forming bar can leak through, since `advance_to()` only
        ever counts bars whose close time has actually passed)."""
        cursor = self._cursors[timeframe]
        bars = cursor.bars
        start = max(0, cursor.closed_count - count)
        end = cursor.closed_count
        return BarSeries(
            open=bars.open[start:end],
            high=bars.high[start:end],
            low=bars.low[start:end],
            close=bars.close[start:end],
            tick_volume=bars.tick_volume[start:end],
            time_utc=bars.time_utc[start:end],
        )

    def get_account_state(self) -> AccountState:
        return self.account_state

    def get_current_price(self) -> float:
        return self._current_price
