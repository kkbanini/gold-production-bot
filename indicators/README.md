# indicators/

## Responsibility

Standalone, pure mathematical indicator functions operating on numpy arrays.
No I/O, no broker dependency, no mutable module-level state — every
function is deterministic given its input array(s) and parameters (RQ-007).
Direct MT5-computed indicator values are banned by directive; every value
here is derived from raw OHLC arrays.

## Implementation

`math_engine.py`:

- `sma(values, period)` — simple moving average via a vectorized
  cumulative-sum window (not an IIR filter like the others, so no
  recursive loop is needed). First `period - 1` entries are NaN. Used by
  `strategy/execution_triggers.py`'s tick-volume filter.
- `ema(values, period)` — exponential moving average, seeded with the
  simple average of the first `period` values. First `period - 1` entries
  are NaN (undefined warm-up).
- `atr(high, low, close, period=14)` — Average True Range using Wilder's
  smoothing (not a plain EMA — Wilder's smoothing uses `alpha = 1/period`
  seeded with a sum, versus EMA's `alpha = 2/(period+1)` seeded with a
  mean). First `period - 1` entries are NaN.
- `adx(high, low, close, period=14)` — Average Directional Index (Wilder):
  computes +DM/-DM, Wilder-smooths them alongside True Range into +DI/-DI,
  derives DX, then Wilder-smooths DX into ADX. First `2*(period-1)` entries
  are NaN (two rounds of Wilder smoothing).
- All three raise `ValueError` on insufficient history or mismatched array
  lengths rather than silently producing a NaN-derived result from too
  little data (RQ-007 — a pure function with a well-defined precondition,
  not a function that guesses).
- `FloatArray` — the module's `npt.NDArray[np.float64]` type alias,
  imported by `strategy/trend_filter.py` and any future indicator consumer
  for consistent typing under `mypy --strict`.

## Verification note — a real bug this caught

The first implementation of `adx()` Wilder-smoothed the DX line using the
same "smoothed sum" convention used internally for True Range/+DM/-DM, but
forgot the final `/ period` normalization that convention requires before
the result means anything as an average — exactly the normalization
`atr()` already applies to `smoothed_tr`. This let ADX blow past its
mathematically required `[0, 100]` bound (a strong synthetic uptrend
produced `ADX = 1400.0`). Caught during ad hoc verification (no automated
`tests/indicators/` suite exists yet) by cross-checking against **two**
independently-derived pure-Python reference implementations of Wilder's
ADX — one using the sum-then-divide convention, one using direct
step-by-step averaging — plus an explicit `0 <= ADX <= 100` bound
assertion. The first reference implementation initially shared the exact
same missing-division bug (derived from the same flawed mental model), so
it did not catch the error on its own; the second, structurally different
derivation did. Fixed by adding the missing `/ period` division (see
`CHANGELOG.md` §0.5.0).

## Depends On

Nothing internal. External: `numpy`.

## Depended On By

`strategy/trend_filter.py` (EMA + ADX for master trend alignment),
`backtester/` (vectorized mode recomputes indicators over historical
arrays, future phase), `optimizer/` (parameter sweep evaluates indicator
outputs across candidate parameter sets, future phase).

## Governing Docs

`docs/API_SPEC.md` §1 (`Bar`/`Tick` shapes indicators consume).
`docs/RISK_REGISTER.md` RR-015 (numerical bugs / NaN propagation — the ADX
bug above is exactly this risk materializing and being caught pre-commit).

## Non-Goals (This Phase)

No automated `tests/indicators/` suite yet — verification this phase was ad
hoc (independent reference-implementation cross-checks, boundary
assertions, error-path checks; see `CHANGELOG.md` §0.5.0), consistent with
the project's plan to introduce the full automated test harness in a
dedicated later phase.
