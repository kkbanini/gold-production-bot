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

## Depends On

`indicators/` (`ema`, `adx`, `FloatArray`), `docs/API_SPEC.md` (`Bar`, `Tick`, `Signal`,
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

Entry-trigger logic (breakout/pullback patterns, wick-fill analytics, tick
volume filters) is not part of this phase — it lands separately alongside
execution triggers. No automated `tests/strategy/` suite yet — verification
this phase was ad hoc (synthetic bullish/bearish/mismatched/choppy OHLC
scenarios; see `CHANGELOG.md` §0.5.0), consistent with the project's plan
to introduce the full automated test harness in a dedicated later phase.
