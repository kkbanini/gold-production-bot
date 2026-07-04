# analytics/

## Responsibility

Computes performance metrics (Sharpe, Sortino, MAR, max drawdown/duration, win
rate, profit factor, Deflated Sharpe Ratio) exclusively from the persisted equity
curve and order/fill history in `storage/` — never from in-memory or ad hoc
recomputation (RQ-016), so live-reported analytics and `optimizer/`'s WFO-report
analytics share one implementation. Produces the `PerformanceReport` DTO
(`docs/API_SPEC.md` §5) consumed by `optimizer/`'s promotion gate.

## Depends On

`storage/` (`EquityCurveRepository`, `OrderRepository` reads).
`docs/RESEARCH.md` §7 (formula definitions).

## Depended On By

`optimizer/` (promotion gate evaluation, ADR-0004 §5), operational
reporting/dashboards (future phase).

## Governing Docs

`docs/API_SPEC.md` §5 (`PerformanceReport`). `docs/RESEARCH.md` §3
(Deflated Sharpe Ratio formula), §7 (Sharpe/Sortino/MAR/drawdown formulas).

## Non-Goals (This Phase)

No code exists yet. Metrics engine implementation and
`tests/analytics/test_metrics_from_ledger.py` (RQ-016) land in Phase 8, after
`storage/` and `execution/` produce real ledger data to compute over.
