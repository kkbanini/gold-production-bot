# Research Specification: Quantitative Edge, Validation, and Data Constraints

| Field | Value |
|---|---|
| Status | Normative — governs `optimizer/`, `backtester/`, `analytics/`, `strategy/` |
| Governs / Governed by | ADR-0004 (methodology), this document (numbers) |

This document is the quantitative reference for anything `optimizer/` or
`backtester/` computes. ADR-0004 established *that* anchored walk-forward
optimization is mandatory; this document defines the exact parameters, formulas,
and data specifications so that "anchored WFO" is a reproducible procedure, not a
phrase.

## 1. Instrument & Data Specification

| Property | Value |
|---|---|
| Instrument | XAUUSD (Gold vs. USD spot, as quoted by the connected MT5 broker) |
| Primary analysis timeframes | M1 (execution/tick-adjacent), M15 and H1 (signal generation), H4 and D1 (regime/trend filter context) |
| Minimum history depth (anchor start) | Earliest available continuous, gap-audited history from the broker's history server — practically bounded by broker retention, targeted minimum 5 years of M15+ bars for regime coverage (spans multiple monetary-policy regimes, at least one high-volatility gold regime, e.g. a real-rate/USD-driven repricing episode). |
| Data provenance | MT5 broker's own historical price server, retrieved via `BrokerGateway.get_bars` (ADR-0002) — no third-party historical data vendor is mixed into the same series, to avoid price-series discontinuities from differing feed/aggregation conventions. |
| Corporate-action adjustment | Not applicable (spot metal, no dividends/splits); swap/rollover cost is modeled explicitly in `execution/`'s cost model (RQ, later phase), not adjusted into the price series. |
| Gap/quality audit | Every bar series pulled for research or WFO is audited for: (a) non-monotonic timestamps, (b) duplicate bars, (c) gaps exceeding `2 × timeframe duration` outside known weekend/holiday closures. Any failed audit halts the research run — a WFO report built on unaudited data is invalid by definition and must not be produced. |

## 2. Time & Session Normalization

Referenced by ADR-0002/RQ-002. All time-of-day-dependent logic (session filters,
news blackout windows, daily-reset boundaries for risk counters) is defined in
**UTC**, derived as follows:

1. At `BrokerGateway.connect()`, resolve `broker_utc_offset = broker_reported_time -
   datetime.now(timezone.utc)` using the broker's own server-time field
   (`symbol_info().time` / equivalent), sampled at connection and re-sampled on a
   fixed **15-minute** interval thereafter.
2. If the re-sampled offset differs from the previously resolved offset by more
   than **1 minute** outside of a scheduled DST transition date (broker-published,
   configured in `config/`), raise `RiskBreach(severity=HIGH)` — this indicates
   either a broker time anomaly or a host clock issue (RR-003), and no
   session/news-window logic may trust an unresolved offset.
3. Trading session windows (for any future session-based filter in `strategy/`)
   are defined in UTC directly (e.g. "London session: 07:00–16:00 UTC"), never in
   broker-local time, so that a session boundary's meaning does not silently shift
   if broker DST convention differs from the definition's original intent.

## 3. Objective Function (Pre-Registered, ADR-0004 §5)

The optimizer's parameter search evaluates a single pre-registered objective
function per run: the **Deflated Sharpe-weighted Calmar blend**, defined as:

```
Objective(θ) = DSR(θ) × min(1, MAR(θ) / MAR_floor)
```

where, for a candidate parameter set `θ` evaluated over its walk-forward OOS
segments (concatenated, not averaged per-fold, to preserve compounding/drawdown
continuity across folds):

- `DSR(θ)` — Deflated Sharpe Ratio (López de Prado, 2014), computed as:

  ```
  DSR(θ) = Φ( ( (SR(θ) - SR₀) × sqrt(n - 1) )
              / sqrt(1 - γ₃·SR(θ) + ((γ₄ - 1) / 4)·SR(θ)²) )
  ```

  where `SR(θ)` is the OOS-segment annualized Sharpe Ratio, `n` is the number of
  OOS return observations, `γ₃`/`γ₄` are the sample skewness/kurtosis of OOS
  returns, `Φ` is the standard normal CDF, and `SR₀` is the *expected maximum
  Sharpe under the null of no skill*, computed from the number of independent
  trials `N` in the parameter sweep via the expected-maximum-of-N-Gaussians
  approximation:

  ```
  SR₀ ≈ sqrt(Var[SR]) × ( (1 - γ_E)·Φ⁻¹(1 - 1/N) + γ_E·Φ⁻¹(1 - 1/(N·e)) )
  ```

  (`γ_E` = Euler-Mascheroni constant ≈ 0.5772; `Var[SR]` estimated from the
  cross-sectional variance of Sharpe ratios observed across the sweep's trials).
  This is what makes the gate *deflated*: it corrects the raw in-sample-selected
  Sharpe for the fact that the best of `N` trials is expected to look good by
  chance alone.

- `MAR(θ)` — MAR ratio (CAGR / Max Drawdown) of the concatenated OOS equity curve.
- `MAR_floor` — minimum acceptable MAR ratio, configured per strategy in
  `strategy/`'s parameter schema (not hardcoded here; this document defines the
  formula shape, not the strategy-specific threshold, since that is a
  strategy-design parameter, not an architecture constant).

Changing this objective function's formula is an ADR-governed change (ADR-0004
Consequence "process discipline"), logged in `CHANGELOG.md` as a MINOR (additive
gate) or MAJOR (redefinition of the core objective) version event depending on
whether prior promoted parameter sets remain valid under the new formula.

## 4. Anchored Walk-Forward Parameterization

Per ADR-0004, every fold is `[train_start=anchor, train_end=t] → [test_start=t+1,
test_end=t+step]`. Concrete parameterization:

| Parameter | Value | Rationale |
|---|---|---|
| `anchor` | Fixed at the dataset's earliest audited bar (§1). Never advances. | Defining property of "anchored" WFO (ADR-0004 §1). |
| `initial_train_length` | 24 months of the primary signal timeframe (M15/H1) | Long enough to span multiple monthly volatility regimes before the first OOS fold is evaluated. |
| `step` (OOS fold length) | 1 month | Balances a large enough OOS sample per fold against re-optimizing frequently enough to track the weekly optimizer cadence (`docs/RUNBOOK.md` §6). |
| `embargo` | 1 trading day between `train_end` and `test_start` | Prevents information leakage from indicator lookback windows (e.g. a 20-period EMA computed at `test_start` must not implicitly reuse `train_end` bars in a way that blurs the train/test boundary — the embargo is a purge zone, per López de Prado's purged/embargoed CV principle adapted to the anchored (non-shuffled) setting). |
| Fold advancement | `train_end` advances by `step` each fold; `train_start` never moves. | Anchored property (ADR-0004 §1); `train_length` grows monotonically fold over fold. |
| Minimum number of folds per WFO run | 12 | Ensures the concatenated OOS curve spans at least one full year, capturing seasonal/regime variation in gold markets. |

## 5. Promotion Gates (ADR-0004 §5, RQ-014)

A candidate parameter set is eligible for `ParameterUpdateEvent` emission only if
**all** of the following hold on its concatenated OOS segment:

1. `DSR(θ) ≥ 0.95` (95% confidence the OOS Sharpe is genuinely positive after
   deflation for the number of trials in the sweep).
2. `IS/OOS Efficiency Ratio = OOS_Sharpe / IS_Sharpe ≥ 0.5` — the OOS performance
   must retain at least half of the in-sample performance; a lower ratio indicates
   overfitting to the training window regardless of the absolute DSR value.
3. `MAR(θ) ≥ MAR_floor` (§3).
4. Maximum OOS drawdown does not exceed the strategy's configured risk-of-ruin
   ceiling (defined alongside `MAR_floor` in `strategy/`'s parameter schema).
5. Minimum OOS trade count ≥ 30 per fold, aggregated ≥ 360 across the minimum 12
   folds — below this, Sharpe/DSR estimates are considered statistically
   unreliable regardless of their computed value.

Failing any gate is not an error condition — it means the current parameter set
remains active another optimizer cycle (`docs/RUNBOOK.md` §3.1). This is logged at
`INFO`/`LOW` severity, never treated as an incident.

## 6. Paper-Trading Minimum Observation Window (Deployment §6)

The minimum `paper` environment observation period before `live` promotion is
defined as the calendar duration required to observe **≥ 100 closed trades** at
the strategy's realized paper-trading trade frequency, with a floor of **30
calendar days** regardless of trade count (to ensure at least one full monthly
regime, including at least one high-impact news cycle, is observed live-adjacent
before capital is committed). Both conditions must be satisfied — high trade
frequency does not shorten the 30-day floor, and low trade frequency extends the
window beyond 30 days until 100 trades accrue.

## 7. Analytics Formulas Reference (`analytics/`, `docs/API_SPEC.md` §5)

- **Sharpe Ratio**: `(mean(returns) - risk_free_rate) / std(returns) × sqrt(periods_per_year)`,
  annualized using the bar frequency the returns series is sampled at.
- **Sortino Ratio**: identical to Sharpe but the denominator uses downside
  deviation only (`std` of `min(return, 0)` returns), annualized identically.
- **MAR Ratio**: `CAGR / abs(Max Drawdown)`, computed over the full equity curve
  in `EquityCurveRepository`.
- **Max Drawdown**: `max over t of (running_peak_equity(t) - equity(t)) / running_peak_equity(t)`.
- **Max Drawdown Duration**: longest contiguous span where `equity(t) < running_peak_equity(t)`.
- **Profit Factor**: `sum(winning trade P&L) / abs(sum(losing trade P&L))`.

All formulas above operate on the persisted equity curve (`storage/`,
`EquityCurveRepository`) — never on an in-memory or ad hoc recomputation — per
RQ-016, so that live-reported analytics and WFO-report analytics share one
implementation.
