# strategy/

## Responsibility

Trend filters and signal-trigger logic that consume `indicators/` outputs and
produce `Signal` events (`docs/API_SPEC.md` §1). Deterministic given identical
bar/tick history and an identical active `ParameterUpdate` (RQ-008). Owns no
broker connection, no order state, and no risk sizing — those are `execution/`'s
responsibility. Consumes the currently-active parameter set published by
`optimizer/` via `ParameterUpdateEvent`, applied only at a bar-close boundary
(ADR-0001 §5, RR-014).

## Depends On

`indicators/` (pure math functions), `docs/API_SPEC.md` (`Bar`, `Tick`, `Signal`,
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

No code exists yet. Trend filter / signal trigger implementation and
`tests/strategy/test_determinism.py` (RQ-008) land in Phase 4.
