# execution/

## Responsibility

The mandatory pre-trade risk gate every `Signal` must pass through before becoming
an `Order` (RQ-009) — no direct `Signal -> Order` path exists. Owns order routing
via `BrokerGateway`, the slippage guard (RQ-010, RR-006), duplicate-submission
idempotency checks against `client_order_id` (RR-007), position sizing and margin
checks (RR-011), `NewsWindow` blackout enforcement (RQ-017, RR-009), and partial
position closures (RQ-011).

## Implementation

`position_manager.py` (Phase 6) — post-entry position management, i.e. what
happens to a position *after* it's open, not the pre-trade risk gate above
(that remains unimplemented — see Non-Goals):

- `calculate_base_take_profit(entry_price, atr_value, side)` — `Base_TP =
  entry ± ATR × 2`.
- `evaluate_partial_close_and_breakeven(position, current_price, atr_value, ...)`
  — once price reaches `Base_TP`, returns a two-step action list: close 50%
  of volume, then move the remaining volume's stop-loss to breakeven (the
  exact entry price — no spread/commission buffer). Returns `[]` if
  already partial-closed or `Base_TP` not yet reached.
- `calculate_trailing_stop(position, current_price, atr_value, ...)` — a
  dynamic ATR(14) × 1.5 trailing stop, active only once breakeven has been
  set. The candidate stop only ever tightens — a level that would loosen
  the existing stop is rejected.
- All three return `OrderActionPayload` — a dataclass mirroring the shape
  of a MetaTrader5 `order_send()` request dict (`action`/`position`/
  `symbol`/`volume`/`sl`/`tp`/`magic`/`comment`) **without importing
  `MetaTrader5`**. `broker/mt5_gateway.py`'s `submit_position_action()` is
  the sole translator from this payload into a real `mt5.order_send()`
  call, preserving the ADR-0002/RQ-001 import boundary (only `broker/` may
  import `MetaTrader5`) even though the phase directive's literal wording
  ("using the MT5 Python library payload configurations") could be read
  as putting that construction directly in `position_manager.py`.

## Depends On

`broker/` (`BrokerGateway` Protocol — order submission, position queries),
`risk/` (`clamp_lot_size`, for partial-close volume rounding),
`storage/` (`OrderRepository` idempotency checks, future), `news/` (active
`NewsWindow` events, future), `docs/API_SPEC.md` (`Signal`, `RiskGateDecision`, `Order`,
`Fill`, `Position` shapes).

## Depended On By

`broker/mt5_gateway.py` (imports `OrderActionPayload` to type
`submit_position_action()` — anticipated by `docs/API_SPEC.md`'s module
ownership matrix, which has `broker/`'s `submit_order()` take an
`execution/`-owned `Order` type), `core` (subscribes to `SignalEvent`,
emits `OrderRequestEvent`/`RiskBreachEvent`, future), `storage/` (persists
resulting `Order`/`Position`/`Fill` records, future), `analytics/`
(trade-level data for performance reporting, future).

## Governing Docs

`docs/API_SPEC.md` §1/§3. `docs/RISK_REGISTER.md` RR-006, RR-007, RR-009, RR-011,
RR-017 (broker-side stop-loss mandate). `docs/RUNBOOK.md` §3 (incident response
tied to execution-layer risk breaches).

## Non-Goals (This Phase)

The pre-trade risk gate (RQ-009), slippage guard (RQ-010), and duplicate-order
idempotency check (RR-007) described in Responsibility above are **not**
implemented yet — Phase 6 only covers post-entry position management
(partial close, breakeven, ATR trailing stop). No automated `tests/execution/`
suite yet — verification this phase was ad hoc (see `CHANGELOG.md` §0.7.0),
consistent with the project's plan to introduce the full automated test
harness in a dedicated later phase.
