# optimizer/

## Responsibility

Offline, weekend-cadence self-learning parameter re-optimization engine. Runs
anchored walk-forward optimization (ADR-0004) exclusively — never sliding-window
or shuffled time-series validation — against historical data via `backtester/`'s
event-driven mode. Computes the pre-registered objective function and mandatory
promotion gates (Deflated Sharpe Ratio, IS/OOS efficiency ratio, MAR floor,
drawdown ceiling, minimum trade count — `docs/RESEARCH.md` §5) and emits a single
`ParameterUpdateEvent` only when every gate passes (RQ-014). Never mutates live
strategy parameters directly or mid-week (RQ-012, RR-014).

## Depends On

`backtester/` (event-driven OOS fold evaluation), `analytics/`
(`PerformanceReport` for gate evaluation), `storage/`
(`ParameterHistoryRepository`), `docs/RESEARCH.md` (fold parameterization,
objective function, gate thresholds).

## Depended On By

`strategy/` (consumes the resulting `ParameterUpdateEvent`), `core` (applies the
event at a bar-close boundary only).

## Governing Docs

ADR-0004 (anchored WFO as sole validation methodology). `docs/RESEARCH.md`
(quantitative parameterization). `docs/RISK_REGISTER.md` RR-004, RR-010, RR-014.

## Non-Goals (This Phase)

No code exists yet. Anchored fold construction, promotion gate enforcement, and
their tests (RQ-012–RQ-014) land in Phase 6, after `backtester/` and
`analytics/` exist to evaluate against.
