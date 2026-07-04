# execution/

## Responsibility

The mandatory pre-trade risk gate every `Signal` must pass through before becoming
an `Order` (RQ-009) — no direct `Signal -> Order` path exists. Owns order routing
via `BrokerGateway`, the slippage guard (RQ-010, RR-006), duplicate-submission
idempotency checks against `client_order_id` (RR-007), position sizing and margin
checks (RR-011), `NewsWindow` blackout enforcement (RQ-017, RR-009), and partial
position closures (RQ-011).

## Depends On

`broker/` (`BrokerGateway` Protocol — order submission, position queries),
`storage/` (`OrderRepository` idempotency checks), `news/` (active
`NewsWindow` events), `docs/API_SPEC.md` (`Signal`, `RiskGateDecision`, `Order`,
`Fill`, `Position` shapes).

## Depended On By

`core` (subscribes to `SignalEvent`, emits `OrderRequestEvent`/`RiskBreachEvent`),
`storage/` (persists resulting `Order`/`Position`/`Fill` records), `analytics/`
(trade-level data for performance reporting).

## Governing Docs

`docs/API_SPEC.md` §1/§3. `docs/RISK_REGISTER.md` RR-006, RR-007, RR-009, RR-011,
RR-017 (broker-side stop-loss mandate). `docs/RUNBOOK.md` §3 (incident response
tied to execution-layer risk breaches).

## Non-Goals (This Phase)

No code exists yet. Risk gate, order router, and slippage guard implementations
and their tests (RQ-009–RQ-011) land in Phase 5.
