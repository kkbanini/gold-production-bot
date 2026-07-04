# indicators/

## Responsibility

Standalone, pure mathematical indicator functions operating on numpy arrays of
`Bar`/`Tick` data. No I/O, no broker dependency, no mutable module-level state —
every function is deterministic given its input array(s) and parameters (RQ-007).
Consumed by `strategy/` for signal generation and by `optimizer/` during walk-forward
parameter sweeps.

## Depends On

Nothing internal. External: `numpy` (and `pandas` where a rolling-window
convenience is materially clearer than raw numpy — decided per indicator, not
blanket-adopted).

## Depended On By

`strategy/` (signal generation), `backtester/` (vectorized mode recomputes
indicators over historical arrays), `optimizer/` (parameter sweep evaluates
indicator outputs across candidate parameter sets).

## Governing Docs

`docs/API_SPEC.md` §1 (`Bar`/`Tick` shapes indicators consume).
`docs/RISK_REGISTER.md` RR-015 (numerical bugs / NaN propagation).

## Non-Goals (This Phase)

No code exists yet. Indicator implementations and their reference-value test
suite (RQ-007, `tests/indicators/test_purity.py`) land in Phase 4, alongside
`strategy/`.
