# ADR-0004: Anchored Walk-Forward Optimization as the Sole Model-Validation Methodology

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-07-04 |
| Deciders | Principal Quantitative Engineering |
| Supersedes | — |
| Superseded by | — |
| Related | ADR-0001, ADR-0003 |

## Context

The system includes a weekend offline "self-learning" parameter optimizer
(`optimizer/`) that re-fits strategy parameters (`strategy/`) against recent XAUUSD
history and publishes a `ParameterUpdateEvent` (ADR-0001) consumed by the live core
before the next trading week. This is the single highest-risk component in the
system from a *methodological* (not implementation) standpoint: it is trivial to
build an optimizer that looks excellent in-sample and is catastrophic live, via any
of the well-documented failure modes of time-series backtesting:

1. **Look-ahead bias** — using information not available at decision time (e.g.
   fitting on a window that includes future bars relative to a simulated trade).
2. **Random k-fold cross-validation on time series** — shuffling temporally ordered
   data breaks the autocorrelation structure and leaks future information into
   training folds; standard for i.i.d. ML data, invalid for price series.
3. **Non-anchored (sliding, disjoint) re-optimization windows** that silently drop
   older regimes and overfit to whichever recent regime happens to be in the window,
   producing parameter sets that chase the last few weeks of price action.
4. **Multiple-comparisons / selection bias** — testing many parameter combinations
   and reporting the best one's in-sample Sharpe without correcting for the number
   of trials (the "backtest overfitting" problem formalized by López de Prado's
   Probability of Backtest Overfitting and Deflated Sharpe Ratio work).
5. **Survivorship/parity gap** between the vectorized research backtester and live
   execution (addressed structurally by ADR-0002, but the *validation methodology*
   itself must also be sound independent of that).

## Decision

The system's **sole sanctioned validation methodology** for any strategy parameter
set — whether hand-tuned or produced by `optimizer/` — is **anchored walk-forward
optimization (WFO)**, fully specified quantitatively in `docs/RESEARCH.md`
(§"Anchored Walk-Forward Parameterization"). No parameter set may be promoted to
paper or live trading (`docs/DEPLOYMENT.md` environment promotion gate) without a
passing anchored-WFO report.

Concretely:

1. **Anchored, not sliding, training windows.** Each successive re-optimization step
   extends the training window's start date fixed at the dataset's earliest point
   (`anchor`) and only advances the window's *end* date — never drops older data
   from the training set. This is the "anchored" property: parameter sets must
   remain consistent with all prior observed regimes, not just the most recent one.
2. **Strict chronological train/test separation, no shuffling.** Each walk-forward
   fold is `[train_start=anchor, train_end=t] → [test_start=t+1, test_end=t+step]`.
   The test segment is *never* seen by the optimizer's objective function. Folds
   are evaluated strictly in chronological order; a fold's test segment must never
   overlap a later fold's train segment.
3. **Out-of-sample (OOS) aggregate is the only number that gates promotion.**
   In-sample (IS) metrics are recorded for diagnostic IS/OOS degradation analysis
   only (per `docs/RESEARCH.md`'s "IS/OOS Efficiency Ratio") and are explicitly
   forbidden from being used as a promotion criterion.
4. **A single, pre-registered objective function per optimizer run.** The objective
   function (deflated/penalized risk-adjusted return — specified in
   `docs/RESEARCH.md`) is fixed *before* the parameter sweep runs, not selected
   post hoc from among several candidate metrics. Changing the objective function
   is an ADR-governed change, logged in `CHANGELOG.md`.
5. **Overfitting-control statistics are mandatory outputs, not optional
   diagnostics.** Every optimizer run must emit: number of parameter combinations
   trialed, Deflated Sharpe Ratio (DSR) of the selected combination, and the IS/OOS
   efficiency ratio. `optimizer/` must refuse to emit a `ParameterUpdateEvent` if
   any gate in `docs/RESEARCH.md`'s promotion criteria fails — this is a hard
   `CRITICAL`-severity guard per `docs/RISK_REGISTER.md`, not a warning.
6. **The event-driven backtester is the OOS evaluator.** Per ADR-0002, OOS folds
   are evaluated by running the *same* `strategy/`/`execution/` code the live
   system runs, against `backtester/`'s `BrokerGateway` test-double, over the OOS
   segment only — closing the backtest/live parity gap for the validation step
   itself, not only for ad hoc manual backtests.

## Consequences

### Positive

- Structurally forecloses the most common and most damaging backtest-overfitting
  failure modes (look-ahead bias, shuffled time-series CV, sliding-window regime
  chasing, unchecked multiple comparisons) before any strategy code is written.
- Produces an audit-ready, quantitatively reproducible promotion gate: every
  parameter set that ever reaches live trading has an attached anchored-WFO report,
  satisfying `docs/TRACEABILITY_MATRIX.md` and `docs/RISK_REGISTER.md` audit
  requirements.
- Because OOS evaluation reuses the live `strategy/`/`execution/` code path
  (ADR-0002), the validation result is a much stronger predictor of live behavior
  than an isolated vectorized backtest would be.

### Negative / Accepted Trade-offs

- Anchored WFO is computationally more expensive than a single train/test split or
  sliding-window CV (training set grows every fold); accepted because the
  optimizer runs offline, on a weekend cadence, with no live-latency constraint.
- Requires maintaining a growing historical dataset indefinitely (anchor never
  moves forward) rather than a bounded rolling buffer; storage cost is accepted as
  negligible relative to correctness gained (XAUUSD OHLCV + tick history is small
  relative to available storage).
- Imposes process discipline (pre-registered objective function, no post hoc metric
  swapping) that is easy to violate under research pressure to "just check one more
  metric" — mitigated by making the objective function part of the versioned
  `docs/RESEARCH.md` spec, changeable only via a new ADR revision.

## Alternatives Considered

| Alternative | Rejected Because |
|---|---|
| Sliding (non-anchored) walk-forward windows | Silently discards older regimes; produces parameter sets that overfit to recent conditions and whipsaw across regime changes — precisely the failure mode this ADR exists to prevent. |
| k-fold cross-validation with shuffled folds | Invalid for autocorrelated time series; leaks future information into training folds via adjacency, inflating apparent skill. |
| Single fixed train/test split (no walk-forward) | One split gives one noisy performance estimate with no regime-robustness signal and no defense against multiple-comparisons bias across parameter sweeps. |
| Purely in-sample optimization with manual out-of-sample "eyeballing" | Not falsifiable, not reproducible, not automatable for the weekend self-learning optimizer's unattended operation. |

## Compliance / Verification

- Quantitative parameterization (anchor date, fold length, step size, embargo
  period, objective function formula, DSR/IS-OOS gate thresholds) specified in
  `docs/RESEARCH.md` and is the normative reference; this ADR governs the
  *methodology*, `docs/RESEARCH.md` governs the *numbers*.
- `optimizer/` must be unit-tested (Phase 6+) to prove it structurally cannot
  construct a fold where `test_start ≤ train_end` (look-ahead guard) and cannot
  emit a `ParameterUpdateEvent` when any promotion gate fails.
- Traceability recorded in `docs/TRACEABILITY_MATRIX.md` under the
  `optimizer/`/`backtester/` requirement rows.
