# optimizer/

## Responsibility

Offline, weekend-cadence self-learning engine. **This is simpler than the
Phase 0 vision below** — see "Simplification vs. the original ADR-0004
design" for why.

## Implementation

`self_learning.py`:

- `is_market_closed_for_optimization(now_utc)` — `True` only on Saturday
  (UTC), the day Gold is fully closed all day.
- `create_weekend_optimizer_scheduler(job)` — wraps `job` in an
  APScheduler `BackgroundScheduler` with a `CronTrigger(day_of_week="sat",
  hour=3, timezone="UTC")`. This is the *primary* gate.
- `run_weekly_optimization_cycle(state_manager, tunable_parameters, ...)`
  — the function the scheduled job calls. Re-checks
  `is_market_closed_for_optimization` itself as a *second, independent*
  gate, so a direct/manual call outside the scheduler still can't run on
  a non-Saturday. Reads closed-trade history, computes performance
  metrics, decides at most one parameter shift, persists it, and runs the
  Monte Carlo bootstrap.
- `compute_ledger_metrics(closed_trades)` — pure aggregation: trade
  count, win rate, profit factor, total profit.
- `decide_parameter_shift(tunable_parameters, metrics)` — **rule-based,
  at most one parameter change per call**: below
  `MIN_TRADES_FOR_ADJUSTMENT` (10) trades, no change; win rate below 40%
  tightens `ADX_TREND_THRESHOLD` by one step; otherwise, profit factor
  below 1.0 widens `TRAILING_ATR_MULTIPLIER` by one step; otherwise, no
  change. A parameter already at its bound, or missing from the supplied
  dict, also yields no change.
- `run_monte_carlo_bootstrap(trade_profits, iterations=1000)` — resamples
  the closed-trade P&L sequence with replacement 1000 times (default),
  reporting the 5th/95th percentile of resampled final P&L and the
  fraction of resamples that were profitable. Tests whether observed
  performance is robust to trade *ordering*, not an artifact of a lucky
  win/loss sequence.

### Isolation guarantee (verified, not just asserted)

This module reads only `StateManager.get_closed_trades()` and writes only
via `StateManager.record_parameter_change()`, which appends to a new,
dedicated `parameter_history` table (added this phase). It never calls
`save_fsm_state()` and never touches an open `trade_ledger` row. Verified
against a real SQLite database: seeded an FSM-state snapshot and an open
position, ran a full weekly optimization cycle that triggered a parameter
shift, and confirmed byte-for-byte that both the FSM snapshot and the
open trade were completely unchanged afterward, with exactly one new
`parameter_history` row written.

## Simplification vs. the original ADR-0004 design

`docs/RESEARCH.md` and ADR-0004 describe a much heavier mechanism:
anchored walk-forward optimization against historical OHLC data via
`backtester/`'s event-driven mode, a Deflated-Sharpe-weighted objective
function, and multi-gate promotion criteria (DSR ≥ 0.95, IS/OOS efficiency
ratio ≥ 0.5, MAR floor, drawdown ceiling, minimum trade count). None of
that exists — `backtester/` was flagged as **unscheduled** in the current
10-phase roadmap back in Phase 2's `docs/TRACEABILITY_MATRIX.md` update,
and no phase has built it. This phase's directive ("rule-based
configuration shift, max 1 parameter change per iteration... 1,000
iteration random sequence bootstrap") describes something categorically
simpler: a single deterministic rule evaluated against *live/paper*
closed-trade ledger metrics, not a historical-data optimization search.
This implementation matches that simpler ask. The ADR-0004 machinery
remains the documented target for a future, more complete
optimizer+backtester phase, if one is added to the roadmap — it is not
silently abandoned, just not what this phase built.

## Flagged — rule thresholds and parameter mapping were not specified

The phase directive asked for "rule-based configuration shift" without
giving exact trigger thresholds or which parameter each condition should
adjust. `MIN_TRADES_FOR_ADJUSTMENT=10`, `LOW_WIN_RATE_THRESHOLD=0.40`,
`LOW_PROFIT_FACTOR_THRESHOLD=1.0`, and the win-rate→`ADX_TREND_THRESHOLD`
/ profit-factor→`TRAILING_ATR_MULTIPLIER` mapping are this
implementation's choice, flagged for review — all overridable via
`TunableParameter`/function arguments.

## Depends On

`storage/` (`StateManager.get_closed_trades()`,
`StateManager.record_parameter_change()`). External: `apscheduler`.

## Depended On By

The eventual FSM orchestration loop (`main.py`) would own actually
starting `create_weekend_optimizer_scheduler()` at boot and supplying the
live `tunable_parameters` dict sourced from `strategy/`/`execution/`'s
current constants — that wiring doesn't exist yet (this phase delivers
the mechanism, not its integration into a running process).

## Governing Docs

`docs/RISK_REGISTER.md` RR-014 (weekend parameter update must not touch
mid-week/mid-signal-evaluation state — satisfied here by the isolation
guarantee above, though in a narrower form than ADR-0001 §5's original
`ParameterUpdateEvent`/bar-close-boundary design, since no `core`/`EventBus`
exists yet). ADR-0004 and `docs/RESEARCH.md` remain the target design for
a future, fuller optimizer — see Simplification note above.

## Non-Goals (This Phase)

No automated `tests/optimizer/` suite yet — verification this phase was ad
hoc, including a real-SQLite isolation test (see `CHANGELOG.md` §0.9.0),
consistent with the project's plan to introduce the full automated test
harness in a dedicated later phase. Anchored WFO, the Deflated Sharpe
Ratio objective function, and the multi-gate promotion criteria from
ADR-0004/`docs/RESEARCH.md` are not implemented — see Simplification note.
Wiring this module's output into `strategy/`'s live parameters is deferred
to a future phase (no `core`/`EventBus` exists to carry a
`ParameterUpdateEvent` yet).
