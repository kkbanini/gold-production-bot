# risk/

## Responsibility

Equity-based position-size compounding (`risk_manager.py`, Phase 6) and
the centralized, pure-function drawdown-breaker FSM (`drawdown_fsm.py`,
Phase 11d, `docs/PRODUCTION_SPEC.md` §6) — global capital-protection
barriers. Not part of the original Phase 0 module scaffold — created in
Phase 6 per that phase's directive, which asked for a dedicated
`risk/risk_manager.py`. Pure functions throughout; no I/O, no broker
dependency, no dependency on `broker/`'s `SymbolSpec` type (broker volume
constraints are passed in as raw floats, matching the pattern
`strategy/execution_triggers.py` already uses for `point`).

## Implementation

`drawdown_fsm.py` (Phase 11d, `docs/PRODUCTION_SPEC.md` §6) — the single,
centralized place equity-drawdown-driven state transitions are decided;
`main.py` only consumes it (never reimplements or disperses this logic):

- `DrawdownState` — the 5 operational states the spec names verbatim:
  `ACTIVE`, `WARNING`, `SOFT_LOCK`, `HARD_LOCK`, `MANUAL_RESET_REQUIRED`.
- `classify_drawdown_event(current_equity, baselines)` — the sole
  numeric-to-symbolic boundary: computes daily/weekly/monthly drawdown
  and classifies the *worst* tier into one `DrawdownEvent`
  (`WITHIN_TOLERANCE`/`WARNING_THRESHOLD_BREACHED`/
  `SOFT_LOCK_THRESHOLD_BREACHED`/`HARD_LOCK_THRESHOLD_BREACHED`).
  `SOFT_LOCK` thresholds (5%/10%/20%) are unchanged from Phase 10's
  original hard locks; `HARD_LOCK` thresholds (10%/20%/40%, double each)
  and the `WARNING` ratio (60% of the nearest `SOFT_LOCK` limit) are new,
  made-up-but-documented defaults — no catastrophic-tier number was
  specified anywhere, flagged for review.
- `transition_drawdown_state(current_state, event)` — the pure FSM
  reducer the spec requires: `(current_state, event) -> new_state`.
  `ACTIVE`/`WARNING`/`SOFT_LOCK` are all recoverable (re-evaluated fresh
  every cycle, can step back down as equity recovers); only `HARD_LOCK` is
  sticky, always advancing to `MANUAL_RESET_REQUIRED` regardless of the
  next event, and `MANUAL_RESET_REQUIRED` only clears on an explicit
  `DrawdownEvent.MANUAL_RESET_CONFIRMED` (returns straight to `ACTIVE`
  from any state) — a deliberate evolution beyond Phase 10's "never
  auto-resumes at any severity" posture, flagged in
  `docs/ARCHITECTURE_SUMMARY.md` §3.
- `blocks_new_entries(state)` / `blocks_position_management(state)` — the
  two predicates `main.py`'s `run_bar_close_cycle()` gates its own
  entry/position-management branches on. `SOFT_LOCK` blocks new entries
  only — position management (trailing stop, breakeven, partial close)
  explicitly keeps running, per the phase directive.
- `decide_hard_lock_response(new_state, *, liquidate_on_hard_lock)` — the
  `FeatureFlagManager`-driven decision the instant `HARD_LOCK` is freshly
  entered: `should_liquidate=True` if the flag is set (triggering
  `execution.position_manager.build_emergency_liquidation_action()`),
  `False` otherwise (absolute freeze). Returns `None` for every other
  state, so a caller never re-triggers the decision on a later cycle.

`risk_manager.py`:

- `clamp_lot_size(raw_lots, volume_min, volume_max, volume_step)` — rounds
  down to the nearest broker `volume_step` multiple and clamps to
  `[volume_min, volume_max]`; a non-positive input clamps up to
  `volume_min` rather than producing a zero/negative order size. Raises
  `ValueError` on invalid broker volume constraints.
- `calculate_compounded_lot_size(equity, volume_min, volume_max, volume_step, ...)`
  — scales position size directly with account equity in discrete tiers:
  for every `equity_per_lot_increment` (default 1000.0) of equity, add
  `lot_increment` (default 0.01) lots on top of `base_lot_size` (default
  0.01). This is what makes the sizing "compounding" — profits that grow
  equity automatically grow the next trade's size, and drawdowns
  automatically shrink it.

## Flagged: default tier parameters were not specified

The phase directive asked to "calculate lot compounding dynamically
against current account equity" without giving exact tier numbers (unlike
earlier phases, which gave precise thresholds — e.g. the 50-point
breakout filter, `SMA(20) × 1.5`). The default parameters above
(`base_lot_size=0.01`, `equity_per_lot_increment=1000.0`,
`lot_increment=0.01`) are this implementation's choice among reasonable
conventions, not a pre-existing spec — flagged for review, all
overridable via keyword arguments.

## Depends On

`risk_manager.py`: nothing internal, no external dependencies beyond the
standard library (`math`). `drawdown_fsm.py`: nothing internal either —
pure over plain floats/enums; `config.feature_flags.FeatureFlagManager` is
passed into `main.py`'s call site, not imported by this module itself
(`decide_hard_lock_response()` takes a plain `liquidate_on_hard_lock: bool`).

## Depended On By

`execution/position_manager.py` (`clamp_lot_size`, for partial-close
volume rounding; `build_emergency_liquidation_action()` is the payload
`drawdown_fsm.py`'s `HardLockResponse` triggers). `main.py`'s
`run_bar_close_cycle()` (Phase 11d): calls `classify_drawdown_event()` /
`transition_drawdown_state()` / `blocks_new_entries()` /
`blocks_position_management()` / `decide_hard_lock_response()` every
cycle — the sole consumer of this module's drawdown FSM, never
reimplementing its decision logic.

## Governing Docs

No dedicated ADR yet (cross-cutting money-management concern).
`docs/PRODUCTION_SPEC.md` §6 (Phase 11d's drawdown FSM/feature flags). See
`docs/RISK_REGISTER.md` RR-011 (margin/capital risk) and the risk-of-ruin
context `docs/ARCHITECTURE_SUMMARY.md` §2/§5 already tracked for the
predecessor single-tier hard locks (RQ-022).

## Non-Goals (This Phase)

No automated `tests/risk/` suite yet for `risk_manager.py` — verification
that phase was ad hoc (boundary/clamping scenarios, tier scaling, error
paths; see `CHANGELOG.md` §0.7.0), consistent with the project's plan to
introduce the full automated test harness in a dedicated later phase.
(Phase 9 later added formal `tests/test_unit.py` coverage, superseding
this note.) A hard risk-of-ruin ceiling / maximum-exposure cap beyond the
drawdown FSM is not implemented here. `drawdown_fsm.py` itself is fully
unit-tested this phase
(`tests/test_unit.py::TestClassifyDrawdownEvent`/`TestTransitionDrawdownState`/
`TestDrawdownStatePredicates`/`TestDecideHardLockResponse`), but the sole
channel for a human to issue `DrawdownEvent.MANUAL_RESET_CONFIRMED`
(`run_bar_close_cycle()`'s `manual_reset_confirmed` parameter) has no live
control channel wired into `main()` yet — no API/CLI/admin signal exists
for an operator to actually set it; see `docs/ARCHITECTURE_SUMMARY.md` §5.
