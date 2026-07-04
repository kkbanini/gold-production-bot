# risk/

## Responsibility

Equity-based position-size compounding. Not part of the original Phase 0
module scaffold — created in Phase 6 per that phase's directive, which
asked for a dedicated `risk/risk_manager.py`. Pure functions over plain
floats; no I/O, no broker dependency, no dependency on `broker/`'s
`SymbolSpec` type (broker volume constraints are passed in as raw floats,
matching the pattern `strategy/execution_triggers.py` already uses for
`point`).

## Implementation

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

Nothing internal. No external dependencies beyond the standard library
(`math`).

## Depended On By

`execution/position_manager.py` (`clamp_lot_size`, for partial-close
volume rounding), and — for the primary compounding sizing decision at
trade-entry time — the eventual FSM orchestration loop (`main.py`, future
phase).

## Governing Docs

No dedicated ADR yet (cross-cutting money-management concern). See
`docs/RISK_REGISTER.md` RR-011 (margin/capital risk) for the broader
context this module's output feeds into.

## Non-Goals (This Phase)

No automated `tests/risk/` suite yet — verification this phase was ad hoc
(boundary/clamping scenarios, tier scaling, error paths; see
`CHANGELOG.md` §0.7.0), consistent with the project's plan to introduce
the full automated test harness in a dedicated later phase. A hard
risk-of-ruin ceiling / maximum-exposure cap is not implemented here —
only the compounding tier formula and broker-constraint clamping the
phase directive asked for.
