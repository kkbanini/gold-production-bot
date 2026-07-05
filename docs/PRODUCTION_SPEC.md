# Production Engineering & Architectural Specifications (v1.0.0-RC1)

## 1. Secrets Management & Boot Validation
- All sensitive credentials (MT5_LOGIN, MT5_PASSWORD, API_KEYS) must pass strictly through verified Environment Variables.
- The `ConfigValidator` engine must inspect all keys at initialization. If values are empty, default-leaked, or syntactically invalid, trigger a fatal application panic and stop execution.
- Sensitive values must be dynamically redacted or excluded from all structured logging frameworks to prevent credential leaking in production logs.

## 2. Dynamic Calendar Feed Priority
- The system must decouple the economic calendar feed through a unified `CalendarProvider` interface to ensure high availability and prevent single-point-of-failure blocks.
- Core runtime priority must be driven by configuration matrices:
  ```yaml
  calendar:
    provider_priority: ['tradingeconomics', 'finnhub', 'offline_snapshot']
    timeout_ms: 3000
    rate_limit_per_min: 60
  ```
- The fallback logic must cleanly transition between active providers upon network fault detection, applying a strict 3000ms timeout window and a localized rate limiter capped tightly at max 60 requests per minute.

## 3. Normalized Clock Abstraction
- All execution session boundaries must be dynamically calculated using the signature: `ClockProvider.get_server_time(symbol: str) -> AwareDatetime`.
- The provider must derive boundaries strictly from the broker-provided server time representation combined with a configured timezone policy object. Hardcoded Daylight Saving Time (DST) definitions or local machine-naive timestamps are strictly prohibited.

## 4. Pre-Flight Idempotency & Database Atomicity
- Prior to routing any transaction payload to the MT5 execution API gateway, the platform must write a pre-flight execution log record.
- This operation must execute within an atomic transaction block:
  ```sql
  INSERT INTO order_ledger (client_order_id, state, timestamp) VALUES (?, 'REQUESTED', ?);
  ```
- Before triggering any automated network retry loop following an MT5 timeout, the gateway component must actively audit the local transaction engine and query the server cache to guarantee the transaction state was not already processed.

## 5. Event Sourcing Paradigm & Rich Gate Validation
- The trading ledger must separate immutable data states from dynamic projections.
- The Event Store is strictly append-only. Active order tracking states must be projected by reducing historical lifecycle events: `Requested`, `Validated`, `Sent`, `Pending`, `Partially_Filled`, `Filled`, `Modified`, `Cancelled`, `Rejected`, `Expired`, and `Closed`.
- The `PreTradeValidator` pipeline must return a rich `ValidationResult` payload:
  ```python
  @dataclass(frozen=True)
  class ValidationResult:
      is_valid: bool
      reason_code: str
      severity: SeverityLevel  # INFO, WARNING, ERROR, CRITICAL
      is_retryable: bool
      metadata: dict
  ```

## 6. Pure-Function FSM Drawdown Breaker
- Global capital protection barriers must act as a pure mathematical state transition system: `FSM(current_state, event) -> new_state`. Conditional logic flags must not be scattered across modules.
- The operational states are bound to: `ACTIVE`, `WARNING`, `SOFT_LOCK`, `HARD_LOCK`, and `MANUAL_RESET_REQUIRED`.
- Behavior in `HARD_LOCK` must be driven by configuration: If `config.flags.liquidate_on_hard_lock` is true, trigger immediate market exit orders. If false, freeze execution channels and wait for manual human interaction.

## 7. Bifurcated Resiliency, SLO, & Disaster Recovery
- Network I/O operations must implement Exponential Backoff (Max 5 attempts: 2s, 4s, 8s, 16s, 32s). SQLite operations are barred from sleep-based retries and must utilize immediate rollbacks combined with explicit `busy_timeout` definitions.
- Every state-altering command (Circuit breaker reset, Manual overrides, Flag alterations) must be logged inside an immutable persistent `Audit Trail` table capturing timestamps, actor hashes, and original-vs-new parameter deltas.
- Disaster Recovery: Upon runtime boot following an unexpected crash or system restart, the system must read the last known state projection from the SQLite WAL engine, actively synchronize and reconcile those parameters against the live open tickets inside the MT5 broker terminal, and fix any data discrepancies before releasing the FSM to `ACTIVE` mode.
- SLO Metrics: The background daemon thread must track and compute real-time Operational Metrics (`trade_latency`, `spread`, `order_reject_rate`, `mt5_latency`, `retry_count`, `heartbeat_failures`) to evaluate structural system health against contractual Service Level Objectives.
