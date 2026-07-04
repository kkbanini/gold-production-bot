# strategy/

## Responsibility

Trend filters and signal-trigger logic that consume `indicators/` outputs and
produce `Signal` events (`docs/API_SPEC.md` §1). Deterministic given identical
bar/tick history and an identical active `ParameterUpdate` (RQ-008). Owns no
broker connection, no order state, and no risk sizing — those are `execution/`'s
responsibility. Consumes the currently-active parameter set published by
`optimizer/` via `ParameterUpdateEvent`, applied only at a bar-close boundary
(ADR-0001 §5, RR-014).

## Implementation

`trend_filter.py` — `evaluate_master_trend()`: validates master trend
alignment across three timeframes, each compared against its *own* EMA
(D1 EMA(200), H4 EMA(50), H1 EMA(40)) rather than a single cross-timeframe
price, since each timeframe's close is only available at its own bar-close
cadence. Direction is `BULLISH` only if all three sit above their EMA,
`BEARISH` only if all three sit below, otherwise `NONE`. Additionally
requires the H1 ADX(14) to exceed `ADX_TREND_THRESHOLD` (25.0) —
`TrendAlignment.is_valid` is `True` only when both the direction agrees
across all three timeframes *and* ADX confirms sufficient trend strength.
Raises `ValueError` (via `indicators.math_engine`) on insufficient history
rather than silently evaluating against NaN-derived indicator values.

`execution_triggers.py` — three independent entry-trigger signals, precise
definitions in `docs/RESEARCH.md` §8 (authored this phase — flagged there
as this implementation's specific choice, since no prior spec existed for
the breakout/pullback pattern *shapes*, only for the already-given
50-point and tick-volume thresholds):

- `detect_breakout()` — 2-candle breakout: latest close must clear the
  prior bar's high/low by ≥ 50 broker points (`SymbolSpec.point` from
  `broker/`), `volume_confirmed` requires `tick_volume > SMA(20) × 1.5`.
  `BreakoutSignal.is_valid` requires both.
- `detect_pullback()` — trend-continuation pullback against a
  `reference_level` array (typically the trend's own EMA): the latest
  bar's low/high must touch or cross the level intrabar but close back on
  the trend side of it. Only meaningful given a non-`"NONE"` trend
  direction from `trend_filter.py`.
- `analyze_wick_fill()` — classifies the latest bar's upper/lower shadow
  as a fraction of its full range; > 60% on either side is a rejection
  signal (long lower wick → `BUY`, long upper wick → `SELL`). A zero-range
  bar yields `NONE` rather than a division error.

Also added `indicators.math_engine.sma()` (simple moving average) this
phase, needed for the tick-volume filter above.

## Depends On

`indicators/` (`ema`, `adx`, `sma`, `FloatArray`), `docs/API_SPEC.md` (`Bar`, `Tick`, `Signal`,
`ParameterUpdate` shapes).

## Depended On By

`core` (subscribes `strategy/`'s handler to `TickEvent`/`BarClosedEvent`),
`execution/` (consumes emitted `Signal`s), `backtester/` (both vectorized and
event-driven modes execute the same strategy code).

## Governing Docs

ADR-0001 (event-driven dispatch, parameter-update application boundary),
ADR-0004 (parameter sets must originate from anchored WFO promotion).
`docs/RESEARCH.md` (objective function context for parameter meaning).

## Non-Goals (This Phase)

`execution_triggers.py`'s three signals (breakout, pullback, wick-fill) are
independent — combining them into a single entry decision (e.g. requiring
breakout AND volume confirmation, or pullback OR wick-fill rejection) is an
`execution/` concern for a later phase, not decided here. No automated
`tests/strategy/` suite yet — verification both phases was ad hoc (see
`CHANGELOG.md` §0.5.0 and §0.6.0), consistent with the project's plan to
introduce the full automated test harness in a dedicated later phase.
