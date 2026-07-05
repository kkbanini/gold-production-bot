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
- `build_emergency_liquidation_action(position)` (Phase 11d,
  `docs/PRODUCTION_SPEC.md` §6) — a full-volume `TRADE_ACTION_DEAL` close,
  the payload `risk.drawdown_fsm`'s `HardLockResponse.should_liquidate`
  triggers. Unlike `evaluate_partial_close_and_breakeven`'s 50% partial
  close, this closes the position's entire remaining volume in one deal.
  No new broker-side method was needed — `submit_position_action()`
  already translates any `TRADE_ACTION_DEAL` (partial or full) into a real
  `mt5.order_send()` close.

`validation.py` (Phase 11c, `docs/PRODUCTION_SPEC.md` §4/§5, RR-007) —
the `PreTradeValidator` pipeline's concrete duplicate-order gate:

- `SeverityLevel` (`INFO`/`WARNING`/`ERROR`/`CRITICAL`) and
  `ValidationResult` (`is_valid`/`reason_code`/`severity`/`is_retryable`/
  `metadata`) — the spec's exact rich validator payload shape.
- `check_duplicate_order_before_retry(latest_event, *, broker_ticket_still_open)`
  — a pure function reducing the local Event Store's most recent recorded
  state (`storage.state_manager.OrderEvent`, "audit the local transaction
  engine") and whether the broker still confirms a previously-recorded
  ticket open (`broker.mt5_gateway.MT5Gateway.is_ticket_still_open()`,
  "query the server cache") into a `ValidationResult` deciding whether an
  automated retry is safe. Ambiguous cases (reached the broker but no
  confirmed-open ticket either way) are refused rather than assumed safe —
  the conservative reading of "strictly mitigating duplicate order
  anomalies".
- Takes the two already-fetched facts as plain parameters rather than a
  `StateManager`/`MT5Gateway` reference, so it has zero I/O and zero
  import-time dependency on either module (avoiding a `broker` <-> `execution`
  import cycle, since `broker/mt5_gateway.py` already imports
  `execution.position_manager.OrderActionPayload`).
- A general-purpose pre-trade risk/slippage gate (RQ-009/RQ-010) is a
  separate, still-open gap — not built here; see Non-Goals.

## Depends On

`broker/` (`BrokerGateway` Protocol — order submission, position queries),
`risk/` (`clamp_lot_size`, for partial-close volume rounding;
`build_emergency_liquidation_action()` is called *from* `risk.drawdown_fsm`'s
consumer, `main.py`, not by `risk/` itself — no new dependency direction),
`storage/` (Phase 11c: `validation.py` imports `OrderEvent`/`OrderLifecycleState`
from `storage.state_manager` — the concrete idempotency-check input,
replacing the future/generic `OrderRepository` this section previously
anticipated), `news/` (active `NewsWindow` events, future),
`docs/API_SPEC.md` (`Signal`, `RiskGateDecision`, `Order`, `Fill`,
`Position` shapes).

## Depended On By

`broker/mt5_gateway.py` (imports `OrderActionPayload` to type
`submit_position_action()` — anticipated by `docs/API_SPEC.md`'s module
ownership matrix, which has `broker/`'s `submit_order()` take an
`execution/`-owned `Order` type), `main.py` (Phase 11c:
`submit_with_pre_flight_ledger()` is the actual caller of the pre-flight
ledger write this module's `validation.py` audits against — though
`main.py` does not yet call `check_duplicate_order_before_retry()` itself,
since no automated retry loop exists yet, see
`docs/ARCHITECTURE_SUMMARY.md` §5; Phase 11d: `run_bar_close_cycle()` calls
`build_emergency_liquidation_action()` the instant `risk.drawdown_fsm`
decides `HARD_LOCK` should liquidate), `core` (subscribes to
`SignalEvent`, emits `OrderRequestEvent`/`RiskBreachEvent`, future),
`analytics/` (trade-level data for performance reporting, future).

## Governing Docs

`docs/API_SPEC.md` §1/§3. `docs/PRODUCTION_SPEC.md` §4/§5 (Phase 11c) and
§6 (Phase 11d's emergency liquidation payload). `docs/RISK_REGISTER.md`
RR-006, RR-007 (Phase 11c's `validation.py` is the concrete
implementation), RR-009, RR-011, RR-017 (broker-side stop-loss mandate).
`docs/RUNBOOK.md` §3 (incident response tied to execution-layer risk
breaches; `FATAL`-severity "full trading halt" maps to `HARD_LOCK`/
`MANUAL_RESET_REQUIRED`, `CRITICAL`-severity "autonomous de-risk" maps to
`SOFT_LOCK`).

## Non-Goals (This Phase)

The pre-trade risk gate (RQ-009) and slippage guard (RQ-010) described in
Responsibility above are **still not** implemented — Phase 6 covers
post-entry position management (partial close, breakeven, ATR trailing
stop) and Phase 11c covers the duplicate-order idempotency check (RR-007)
specifically; a general pre-trade risk/slippage gate remains a separate,
open gap. `check_duplicate_order_before_retry()` is built and tested but
**not wired into an actual retry loop** — `main.py` has no automated
order-submission retry mechanism today (a single `BrokerOrderRejectedError`
propagates and halts the process; see `main.submit_with_pre_flight_ledger()`).
Building that loop's backoff cadence is `docs/PRODUCTION_SPEC.md` §7's
explicit domain (a later sub-phase), not this one. No automated
`tests/execution/` suite yet for Phase 6's original scope — verification
that phase was ad hoc (see `CHANGELOG.md` §0.7.0); Phase 11c's
`validation.py` is formally unit-tested
(`tests/test_unit.py::TestCheckDuplicateOrderBeforeRetry`), as is Phase
11d's `build_emergency_liquidation_action()`
(`tests/test_unit.py::TestBuildEmergencyLiquidationAction`).
