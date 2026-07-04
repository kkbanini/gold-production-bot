# backtester/

## Responsibility

Two validation modes over historical XAUUSD data: (1) a fast vectorized mode for
rapid research iteration, and (2) an event-driven mode that implements the
`BrokerGateway` `Protocol` (`docs/API_SPEC.md` §3) as a historical-replay test
double, allowing the exact same `strategy/`/`execution/` code that runs live to
run against historical bars/ticks (RQ-015). The event-driven mode is the sole OOS
evaluator for `optimizer/`'s anchored WFO folds (ADR-0004 §6).

## Depends On

`docs/API_SPEC.md` (`BrokerGateway` Protocol it implements), `indicators/` and
`strategy/` (vectorized mode recomputes/executes these directly),
historical bar/tick data sourced identically to `broker/`'s live feed (§1 of
`docs/RESEARCH.md` — no mixed third-party data provenance).

## Depended On By

`optimizer/` (OOS fold evaluation), `strategy/`/`execution/` developers
(manual backtest runs during Phase 4+ development), RR-005 parity-check job
(replays a recent live event window through this module's event-driven mode).

## Governing Docs

ADR-0001 (shared event types), ADR-0002 (shared `BrokerGateway` Protocol —
this is what makes backtest/live parity structural rather than aspirational).
`docs/RESEARCH.md` §1 (data provenance/audit), §4 (anchored WFO fold
parameters this module must honor exactly).

## Non-Goals (This Phase)

No code exists yet. Vectorized and event-driven backtester implementations and
the vectorized-vs-event-driven parity test (RQ-015) land in Phase 7.
