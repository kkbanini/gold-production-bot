# Traceability Matrix

Maps every top-level system requirement to its owning module, governing ADR, and the
test(s) that will verify it. This matrix is a living document — every phase updates
the rows for the modules it touches and adds new requirement rows as the spec is
elaborated. Phase 0 seeded the matrix with placeholder phase numbers guessed before
a concrete roadmap existed; as of Phase 2, "pending Phase N" annotations below have
been corrected to match the actual approved roadmap:

Phase 1 `config/` · Phase 2 `storage/` · Phase 3 `broker/` · Phase 4
`indicators/`+`strategy/` (trend filter) · Phase 5 `strategy/` (entry triggers) ·
Phase 6 `risk/`+`execution/` · Phase 7 `news/` · Phase 8 `optimizer/` · Phase 9
`tests/` (formal automated suite) · Phase 10 `main.py` (core FSM loop) · Phase 11
(sub-phased) `docs/PRODUCTION_SPEC.md`-driven hardening: 11a secrets/DI, 11b
calendar/clock providers, 11c pre-flight idempotency/event sourcing, 11d FSM
drawdown breaker, 11e resiliency/SLO/disaster recovery + test reorg.

Two Phase-0-specified requirements (RQ-015 `backtester/`, RQ-016 `analytics/`) have
no phase assigned in the current roadmap — flagged explicitly below rather than
silently carried forward, since neither module appears in the approved 10-phase plan.

## Legend

- **Req ID** — stable identifier, never renumbered even if a requirement is dropped
  (dropped rows are struck through and retained for audit history, not deleted).
- **Status** — `SPECIFIED` (documented, no code), `IMPLEMENTED` (code + passing
  test), `VERIFIED` (implemented + reviewed + observed in paper trading).

| Req ID | Requirement | Owning Module | Governing ADR / Spec | Test Reference | Status |
|---|---|---|---|---|---|
| RQ-001 | System shall interface exclusively with MetaTrader 5 via an internal `BrokerGateway` abstraction; no other module may import the MT5 package. | `broker/` | ADR-0002 | `broker/mt5_gateway.py` implemented Phase 3 as the sole importer of `MetaTrader5`; `tests/unit/test_unit.py::TestBrokerPureLogic` + `tests/integration/test_integration.py::TestMT5ServerDropouts`/`TestOrderActionSubmission` (Phase 9) exercise it via a `FakeMT5` substitute; a dedicated static "no other module imports MetaTrader5" lint/test still does not exist | IMPLEMENTED |
| RQ-002 | All timestamps crossing the broker boundary shall be normalized to UTC, correcting for broker server time vs. host local time and broker-side DST. | `broker/` | ADR-0002, `docs/RESEARCH.md` §Time & Session Normalization | `MT5Gateway.broker_utc_offset`/`is_within_execution_window()` implemented Phase 3; formally tested Phase 9 (`tests/unit/test_unit.py::TestBrokerPureLogic::test_execution_window_boundaries`, `tests/integration/test_integration.py::TestMT5ServerDropouts`); periodic re-validation/DST-drift alerting (RR-003) still not implemented | IMPLEMENTED (partial) |
| RQ-003 | All cross-module communication shall occur through immutable, typed events on a single `EventBus`; no module shall mutate another module's state directly. | core / all | ADR-0001 | **Not implemented as specified** — Phase 10's `main.py` is a single-threaded poll-and-decide loop, not a full `EventBus`; it does satisfy the *spirit* (one writer, one dispatch loop) without the formal event-typed infrastructure ADR-0001 describes | SPECIFIED |
| RQ-004 | Trading state mutations shall occur on exactly one logical thread of control (single-writer core loop). | core | ADR-0001 | `main.py`'s `main()` loop is single-threaded by construction (Phase 10) — satisfied in practice, though not via the formal `core`/`EventBus` design | IMPLEMENTED (informally) |
| RQ-005 | Every event and derived state transition shall be persisted transactionally (ACID) such that a crash mid-write cannot desynchronize the event log from derived state. | `storage/` | ADR-0003 | `storage/state_manager.py` implemented Phase 2; formally tested Phase 9 (`tests/integration/test_integration.py::TestCrashRecovery`, `TestDatabaseRollback`) for the FSM-state-snapshot and transactional-rollback cases; full event-log/derived-table divergence guarantee remains narrower than originally specified (see `storage/README.md` "Simplification" note) | IMPLEMENTED (partial) |
| RQ-006 | The system shall be able to fully reconstruct current trading state by replaying the persisted event log from the last snapshot. | `storage/`, core | ADR-0001, ADR-0003 | Remains single-snapshot recovery (no intermediate event replay), since no `EventBus`/event log was built in Phase 10 either (see RQ-003) | SPECIFIED |
| RQ-007 | Indicator calculations shall be pure functions over numpy arrays with no I/O or broker dependency. | `indicators/` | `docs/API_SPEC.md` §1, `docs/RESEARCH.md` | `indicators/math_engine.py` (`sma`/`ema`/`atr`/`adx`) implemented Phase 4-5; formally tested Phase 9 (`tests/unit/test_unit.py::TestMathEngine`, every function cross-checked against an independent pure-Python reference, plus the boundary/error-path checks that caught the Phase 4 ADX bug — see `CHANGELOG.md` §0.5.0/§0.10.0) | IMPLEMENTED |
| RQ-008 | Strategy signal generation shall be deterministic given identical `Bar`/`Tick` history and an identical `ParameterUpdate`. | `strategy/` | `docs/API_SPEC.md` §1 | `strategy/trend_filter.py` (Phase 4) + `strategy/execution_triggers.py` (Phase 5) implemented; formally tested Phase 9 (`tests/unit/test_unit.py::TestTrendFilter`, `TestExecutionTriggers`); combining the three independent trigger signals into one entry decision is deferred to `execution/` | IMPLEMENTED (partial — no `ParameterUpdate` consumption yet) |
| RQ-009 | Every `Signal` shall pass through a pre-trade risk gate (`RiskGateDecision`) before becoming an `Order`; no direct `Signal → Order` path shall exist. | `execution/`, `risk/` | `docs/API_SPEC.md` §1, `docs/RISK_REGISTER.md` | Not implemented; no test exists | SPECIFIED |
| RQ-010 | Order submission shall guard against excess slippage and reject/re-route fills outside a configured tolerance band. | `execution/` | `docs/RISK_REGISTER.md` (RR-006) | Not implemented; no test exists | SPECIFIED |
| RQ-011 | The system shall support partial position closures as a first-class execution operation. | `execution/`, `broker/` | `docs/API_SPEC.md` §3 (`close_position`) | `execution/position_manager.py`'s `evaluate_partial_close_and_breakeven()` + `broker/mt5_gateway.py`'s `submit_position_action()` implemented Phase 6; formally tested Phase 9 (`tests/unit/test_unit.py::TestPositionManager`, `tests/integration/test_integration.py::TestOrderActionSubmission`); wired into `main.py`'s live orchestration loop Phase 10 (`tests/unit/test_unit.py::TestBarCloseCycle`) | IMPLEMENTED |
| RQ-012 | Strategy parameter re-optimization shall run offline (weekend cadence) and shall never mutate live strategy parameters mid-week or mid-signal-evaluation. | `optimizer/` | ADR-0001 §5, ADR-0004 | `optimizer/self_learning.py`'s Saturday-only `CronTrigger` + independent runtime re-check, and the isolation guarantee implemented Phase 8; formally verified against a real SQLite database Phase 9 (`tests/integration/test_integration.py::TestOptimizerIsolation`); no wiring to `strategy/`'s actual live parameters exists yet | IMPLEMENTED (partial — cadence/isolation only, no live-parameter wiring) |
| RQ-013 | Parameter re-optimization shall use anchored walk-forward validation exclusively; non-anchored or shuffled time-series validation is architecturally forbidden. | `optimizer/`, `backtester/` | ADR-0004 | **Not implemented** — Phase 8 built a deliberately simpler rule-based single-parameter shift against live/paper ledger metrics instead (see `optimizer/README.md` "Simplification vs. the original ADR-0004 design"); anchored WFO remains unimplemented pending a `backtester/` module, which is unscheduled in the current roadmap (Phase 2 note); `tests/optimizer/test_anchored_fold_construction.py` has no phase assigned | SPECIFIED |
| RQ-014 | The optimizer shall refuse to emit a `ParameterUpdateEvent` if Deflated Sharpe Ratio or IS/OOS efficiency ratio gates fail. | `optimizer/` | ADR-0004, `docs/RESEARCH.md` §Promotion Gates | **Not implemented** — same Simplification note as RQ-013; Phase 8's `decide_parameter_shift()` uses simple win-rate/profit-factor thresholds, not DSR/IS-OOS gates, and there is no `ParameterUpdateEvent`/`core` to emit one to; `tests/optimizer/test_promotion_gate_enforcement.py` has no phase assigned | SPECIFIED |
| RQ-015 | The backtester shall support both a fast vectorized mode (research iteration) and an event-driven mode sharing live `strategy/`/`execution/` code (validation/parity). | `backtester/` | ADR-0001, ADR-0002 | `tests/backtester/test_vectorized_vs_event_parity.py` | **UNSCHEDULED** — `backtester/` does not appear in the current 10-phase roadmap; Phase 8's Monte Carlo bootstrap validates the optimizer's ledger metrics but is not the full vectorized/event-driven backtester ADR-0004 assumes. Flagging for a roadmap decision. |
| RQ-016 | Performance analytics shall compute Sharpe, Sortino, MAR, and maximum drawdown from the persisted equity curve, not from in-memory ad hoc state. | `analytics/` | `docs/API_SPEC.md` §5 | `tests/analytics/test_metrics_from_ledger.py` | **UNSCHEDULED** — `analytics/` does not appear in the current 10-phase roadmap; Phase 8's self-learning optimizer reads "SQLite ledger metrics" directly, which may end up substituting for a dedicated `analytics/` module. Flagging for a roadmap decision. |
| RQ-017 | High-impact economic calendar events shall trigger a pre-trade blackout window enforced by the risk gate. | `news/`, `execution/` | `docs/API_SPEC.md` §2 (`NewsWindow`), `docs/RISK_REGISTER.md` (RR-009) | `news/news_engine.py`'s `is_trade_entry_locked()` (±30 min NFP/CPI/FOMC blackout) implemented Phase 7, formally tested Phase 9; `main.py`'s `decide_entry_signal()` consults it Phase 10, **but the live loop never actually calls `fetch_calendar_events()`** — `_fetch_market_snapshot()` passes a hardcoded empty event list, so the blackout is currently inert in practice (see `docs/ARCHITECTURE_SUMMARY.md` §5) | IMPLEMENTED (partial — logic wired but fed no real data) |
| RQ-018 | Secrets (broker credentials, API keys) shall never be committed to source control and shall be sourced from environment/`.env` only, and the system shall refuse to boot if any required configuration key is missing. | `config/` | `docs/RISK_REGISTER.md` (RR-001, RR-012), `docs/DEPLOYMENT.md` | `config/config_manager.py`'s `ConfigManager.load()` implemented Phase 1; formally tested Phase 9 (`tests/unit/test_unit.py::TestConfigManager`) | IMPLEMENTED |
| RQ-019 | CI shall block merge on any Ruff lint failure, Mypy strict-mode failure, or Pytest failure, and shall report coverage. | `.github/workflows/` | this document | `.github/workflows/ci.yml`'s exact invocation (`pytest --cov=. --cov-report=term-missing --cov-fail-under=90`) run locally through Phase 10: 131 tests pass, 95.68% total coverage; the workflow itself has still not executed in a real GitHub Actions run (no push to a remote has occurred) | IMPLEMENTED (locally verified, not yet CI-run) |
| RQ-020 | Every ADR, API contract change, and risk register update shall be reflected in `CHANGELOG.md` with a correct SemVer bump. | project-wide | `CHANGELOG.md` policy | manual release-checklist review (`docs/RUNBOOK.md`) | IMPLEMENTED (process, Phase 0) |
| RQ-021 | The system shall measure per-bar-close processing time and flag any cycle exceeding a 200ms cap. | `main.py` | `docs/ARCHITECTURE_SUMMARY.md` §2 | `main.py`'s `evaluate_processing_time()` implemented and formally tested Phase 10 (`tests/unit/test_unit.py::TestProcessingCap`, `TestBarCloseCycle::test_processing_cap_breach_is_logged`). An operational metric (logs a warning), not a trading halt | IMPLEMENTED |
| RQ-022 | The system shall hard-lock new trade entries when account drawdown exceeds 5% daily, 10% weekly, or 20% monthly. | `risk/` | `docs/RISK_REGISTER.md` (risk-of-ruin), `docs/ARCHITECTURE_SUMMARY.md` §2/§5 | Phase 10's `main.py`-local `check_drawdown_breach()` (single-tier hard lock) was **superseded Phase 11d** by `risk/drawdown_fsm.py`'s graduated FSM — see RQ-029/RQ-030, which formally supersede this row's implementation while preserving its exact 5%/10%/20% numbers as the new `SOFT_LOCK` tier. **Equity baselines are still seeded once at process start and never rolled over at UTC day/week/month boundaries** (unchanged limitation, now tracked under RQ-029) — flagged as a must-fix-before-live item in `docs/ARCHITECTURE_SUMMARY.md` §5 | IMPLEMENTED (superseded by RQ-029/RQ-030; baseline rollover still not wired) |
| RQ-023 | Credentials shall be validated at boot for presence, placeholder/default-value leakage, and syntactic validity, triggering a fatal panic on any violation; secrets shall be redacted from structured logs. | `config/` | `docs/PRODUCTION_SPEC.md` §1 | `config/config_manager.py`'s `ConfigValidator` and `config/secret_redaction.py`'s `SecretRedactingFilter` implemented and formally tested Phase 11a (`tests/unit/test_unit.py::TestConfigManager`/`TestConfigValidator`/`TestSecretRedaction`, `tests/integration/test_integration.py::TestApplicationContainer::test_build_attaches_secret_redaction_to_root_logger`) | IMPLEMENTED |
| RQ-024 | Long-lived services (config, storage, broker) shall be constructed and wired through a centralized composition root using constructor-based dependency injection. | `container.py` | `docs/PRODUCTION_SPEC.md` "Core Orchestration Directive" #1 | `container.py`'s `ApplicationContainer.build()` implemented Phase 11a, replacing `main.py`'s inline `bootstrap_system()`; formally tested (`tests/integration/test_integration.py::TestApplicationContainer`) | IMPLEMENTED |
| RQ-025 | The economic calendar feed shall be decoupled behind a unified `CalendarProvider` interface, with configuration-driven provider priority, a strict per-provider timeout, and a rate limiter capped at a configured requests-per-minute ceiling; fallback shall transition cleanly to the next provider on any failure. | `news/`, `config/` | `docs/PRODUCTION_SPEC.md` §2 | `news/calendar_provider.py`'s `CalendarProvider`/`CalendarProviderChain`/`RateLimiter`/`build_calendar_provider_chain()` and `config/calendar_config.py`'s `CalendarConfig` implemented and formally tested Phase 11b (`tests/unit/test_unit.py::TestCalendarConfig`/`TestRateLimiter`/`TestOfflineSnapshotCalendarProvider`/`TestCalendarProviderChain`/`TestBuildCalendarProviderChain`); wired into `container.py`'s `ApplicationContainer` (`tests/integration/test_integration.py::TestApplicationContainer::test_build_wires_calendar_and_clock_providers_with_defaults`/`test_build_propagates_calendar_config_error_uncaught`) — **`main.py`'s live loop does not yet call it** (see `docs/ARCHITECTURE_SUMMARY.md` §5) | IMPLEMENTED (partial — not consumed by the live loop) |
| RQ-026 | All execution session boundaries shall be dynamically calculated via a `ClockProvider.get_server_time(symbol) -> AwareDatetime` signature, derived strictly from broker-provided server time; hardcoded DST tables or machine-local timestamps are prohibited. | `broker/` | `docs/PRODUCTION_SPEC.md` §3 | `broker/clock_provider.py`'s `ClockProvider`/`MT5ClockProvider` implemented Phase 11b, deriving server time from `MT5Gateway.broker_utc_offset` (ADR-0002) rather than any hardcoded DST rule; formally tested (`tests/unit/test_unit.py::TestMT5ClockProvider`); wired into `container.py`'s `ApplicationContainer` — **`main.py`'s live loop still calls `datetime.now(timezone.utc)` directly** rather than through it (see `docs/ARCHITECTURE_SUMMARY.md` §5) | IMPLEMENTED (partial — not consumed by the live loop) |
| RQ-027 | Prior to routing any order/action payload to the MT5 gateway, the system shall write a pre-flight execution log record (`client_order_id`, `REQUESTED`, timestamp) inside an atomic transaction; before any automated retry after a timeout, the gateway shall audit both the local transaction engine and the broker's live position cache to guarantee the transaction wasn't already processed. | `storage/`, `broker/`, `execution/`, `main.py` | `docs/PRODUCTION_SPEC.md` §4, `docs/RISK_REGISTER.md` RR-007 | `storage.state_manager.record_order_event()` implemented Phase 11c (atomic `order_events` append + `order_ledger` projection upsert); `main.py`'s `submit_with_pre_flight_ledger()` calls it before both real broker-submission call sites; `broker.mt5_gateway.MT5Gateway.is_ticket_still_open()` (the "query the server cache" half) and `execution.validation.check_duplicate_order_before_retry()` (the audit/decision gate) are implemented and formally tested but **not yet wired into an actual retry loop — none exists in `main.py`** (`docs/PRODUCTION_SPEC.md` §7's backoff cadence is a later sub-phase); tests: `tests/unit/test_unit.py::TestOrderEventStore`/`TestSubmitWithPreFlightLedger`/`TestCheckDuplicateOrderBeforeRetry`, `broker`'s `test_is_ticket_still_open_*` | IMPLEMENTED (partial — pre-flight write is live; the retry-audit gate has no retry loop to protect yet) |
| RQ-028 | The trading ledger shall separate an immutable, structurally append-only Event Store (capturing `Requested`/`Validated`/`Sent`/`Pending`/`Partially_Filled`/`Filled`/`Modified`/`Cancelled`/`Rejected`/`Expired`/`Closed` lifecycle events) from a mutable derived-state projection; the `PreTradeValidator` pipeline shall return a rich `ValidationResult` (`is_valid`, `reason_code`, `severity`, `is_retryable`, `metadata`). | `storage/`, `execution/` | `docs/PRODUCTION_SPEC.md` §5 | `storage/migrations.py`'s `order_events` table (append-only, enforced by `trg_order_events_no_update`/`_no_delete` SQLite triggers — a database-engine-level guarantee, not just convention) and `order_ledger` projection implemented Phase 11c; `execution.validation.ValidationResult`/`SeverityLevel` implement the spec's exact payload shape (`dict[str, Any]` rather than a bare `dict`, since `mypy --strict`'s `disallow-any-generics` forbids the latter); formally tested (`tests/unit/test_unit.py::TestSchemaMigrations`/`TestOrderEventStore`/`TestCheckDuplicateOrderBeforeRetry`) | IMPLEMENTED |
| RQ-029 | Global capital-protection barriers shall act as a pure mathematical state-transition system, `FSM(current_state, event) -> new_state`, formally mapping 5 deterministic states (`ACTIVE`, `WARNING`, `SOFT_LOCK`, `HARD_LOCK`, `MANUAL_RESET_REQUIRED`) with conditional logic centralized in one module rather than dispersed; `SOFT_LOCK` shall freeze new entries while allowing active server-side position management (trailing stop, breakeven, partial close) to continue executing. | `risk/`, `main.py` | `docs/PRODUCTION_SPEC.md` §6 | `risk/drawdown_fsm.py`'s `DrawdownState`/`DrawdownEvent`/`classify_drawdown_event()`/`transition_drawdown_state()`/`blocks_new_entries()`/`blocks_position_management()` implemented Phase 11d, superseding Phase 10's single-tier `check_drawdown_breach()`/`TradingState.HALTED` (RQ-022); wired live into `main.py`'s `run_bar_close_cycle()`, gating both the entry and position-management branches; formally tested (`tests/unit/test_unit.py::TestClassifyDrawdownEvent`/`TestTransitionDrawdownState`/`TestDrawdownStatePredicates`, `TestBarCloseCycle`'s `SOFT_LOCK`/`HARD_LOCK`/`MANUAL_RESET_REQUIRED`/`WARNING` cases); the human-facing `MANUAL_RESET_CONFIRMED` control channel does not exist yet (`main()` always passes `manual_reset_confirmed=False`) — flagged in `docs/ARCHITECTURE_SUMMARY.md` §5; equity-baseline rollover remains unimplemented (carried over from RQ-022) | IMPLEMENTED (partial — no live manual-reset channel; baseline rollover not wired) |
| RQ-030 | `HARD_LOCK` behavior shall be governed by a `FeatureFlagManager`-driven `config.flags.liquidate_on_hard_lock`: if `true`, trigger an immediate emergency market liquidation payload; if `false`, execute an absolute system freeze blocking all operations until an explicit human `MANUAL_RESET_CONFIRMED` event. | `config/`, `risk/`, `execution/`, `main.py` | `docs/PRODUCTION_SPEC.md` §6 | `config/feature_flags.py`'s `FeatureFlags`/`FeatureFlagManager` (defaulting `liquidate_on_hard_lock` to `False`, the safer choice), `risk/drawdown_fsm.py`'s `decide_hard_lock_response()`, and `execution/position_manager.py`'s `build_emergency_liquidation_action()` (a full-volume `TRADE_ACTION_DEAL` close) implemented Phase 11d and wired live into `main.py`'s `run_bar_close_cycle()`; formally tested (`tests/unit/test_unit.py::TestFeatureFlags`/`TestFeatureFlagManager`/`TestDecideHardLockResponse`/`TestBuildEmergencyLiquidationAction`, `TestBarCloseCycle::test_hard_lock_with_liquidate_flag_true_emits_liquidation_action`/`test_hard_lock_with_liquidate_flag_false_freezes_everything`) | IMPLEMENTED |
| RQ-031 | Network I/O operations shall implement exponential backoff with a strict retry budget (max 5 attempts: 2s, 4s, 8s, 16s, 32s); SQLite operations shall be barred from sleep-based retries, relying instead on immediate atomic rollback and an explicit `busy_timeout`. | `resilience/`, `storage/`, `news/` | `docs/PRODUCTION_SPEC.md` §7 | `resilience/backoff.py`'s `compute_backoff_delays()`/`retry_with_backoff()`/`RetryBudgetExhaustedError` implemented Phase 11e, matching the spec's exact delay sequence; applied to `news/calendar_provider.py`'s `NetworkCalendarProvider` (deliberately overridden to a smaller 1-retry budget — see `docs/ARCHITECTURE_SUMMARY.md` §3 — so Phase 11b's fast provider-failover guarantee isn't undermined). `storage/db_engine.py`'s `connect()` now sets `PRAGMA busy_timeout` (default 5000ms); every write already used `with connection:` (atomic rollback) since Phase 2. `tests/unit/test_unit.py::TestStorageNeverSleeps` statically enforces (via `ast`) that no `storage/*.py` file ever calls `sleep(...)`. `broker/mt5_gateway.py`'s pre-existing `MT5Gateway.connect()` backoff (Phase 3/RR-002) was deliberately *not* refactored onto this module — see the Flagged note in `resilience/README.md`; tests: `tests/unit/test_unit.py::TestComputeBackoffDelays`/`TestRetryWithBackoff`/`TestBusyTimeout`/`TestStorageNeverSleeps`, `tests/chaos/test_chaos.py::TestNewsFeedSocketDisconnections::test_transient_failure_recovers_via_retry_before_falling_through`/`test_persistent_failure_raises_news_feed_error_after_retry_budget` | IMPLEMENTED |
| RQ-032 | Every state-altering administrative command (circuit-breaker reset, manual override, feature-flag alteration) shall be logged inside an immutable persistent Audit Trail table capturing timestamps, actor hashes, and original-vs-new parameter deltas. | `storage/` | `docs/PRODUCTION_SPEC.md` §7 | `storage/migrations.py`'s `audit_trail` table (migration version 2; structurally append-only via `trg_audit_trail_no_update`/`_no_delete` triggers, same pattern as `order_events`) and `storage/state_manager.py`'s `AuditActionType`/`AuditEvent`/`record_audit_event()`/`get_audit_trail()` implemented Phase 11e; `record_audit_event()` SHA-256-hashes the caller-supplied `actor` before storage (the spec's literal "actor hashes"). Wired into `container.py`'s Disaster Recovery reconciliation (a `DISASTER_RECOVERY_RECONCILIATION` entry on any boot-time divergence) — no other state-altering command (a runtime feature-flag toggle, an interactive circuit-breaker reset) exists yet to audit, since flags are boot-time-only and no live manual-reset channel exists (RQ-029's gap); formally tested (`tests/unit/test_unit.py::TestAuditTrail`, integration coverage via `TestApplicationContainer::test_build_logs_position_audit_divergence`) | IMPLEMENTED (partial — only the Disaster Recovery call site exists; no runtime flag/manual-override call site to audit yet) |
| RQ-033 | Upon boot following an unexpected crash or system restart, the system shall read the last known state projection from the SQLite WAL engine, reconcile it against the live open tickets inside the MT5 broker terminal, and settle any discrepancies before releasing the FSM to `ACTIVE` mode. | `broker/`, `storage/`, `container.py`, `main.py` | `docs/PRODUCTION_SPEC.md` §7 | `broker/mt5_gateway.py`'s `resolve_position_audit()`/`DisasterRecoveryPlan` (pure: turns a `PositionAuditReport` — the "last known state projection", `trade_ledger`, cross-checked against `audit_open_positions()`'s live broker query — into concrete `trade_ledger` upserts) implemented Phase 11e; `container.py`'s `ApplicationContainer.build()` applies the plan, records a Disaster Recovery audit entry (RQ-032) on any divergence, and computes `initial_drawdown_state` (`MANUAL_RESET_REQUIRED` on divergence — `docs/RUNBOOK.md`'s pre-existing `HIGH`-severity policy for a position-audit mismatch, RR-008 — `ACTIVE` otherwise); `main.py`'s `_seed_initial_fsm_context()` seeds `TradingState`/`position` from the broker's live open positions directly. Formally tested (`tests/unit/test_unit.py::TestResolvePositionAudit`, `tests/integration/test_integration.py::TestApplicationContainer::test_build_logs_position_audit_divergence`/`test_build_seeds_initial_fsm_context_from_live_broker_position`/`test_build_seeds_flat_context_when_no_open_positions`) | IMPLEMENTED |
| RQ-034 | A background daemon thread shall track and compute real-time Operational Metrics (`trade_latency`, `spread`, `order_reject_rate`, `mt5_latency`, `retry_count`, `heartbeat_failures`) to evaluate structural system health against contractual Service Level Objectives. | — | `docs/PRODUCTION_SPEC.md` §7 | **Not implemented** — Phase 11e's explicit instruction list substituted the test-suite reorganization for this bullet rather than including it (flagged prominently in `docs/ARCHITECTURE_SUMMARY.md` §3/§5, not silently dropped); no automated, structured health signal exists beyond Python `logging` output | SPECIFIED |

## Coverage Summary (as of Phase 11e / `1.0.0-RC1`)

| Category | Requirements Specified | Implemented | Verified |
|---|---|---|---|
| Architecture / Core | RQ-001–RQ-006 | 4 (RQ-001, RQ-002 partial, RQ-004 informally, RQ-005 partial) | 0 |
| Strategy / Indicators | RQ-007–RQ-008 | 2 (RQ-007; RQ-008 partial — no `ParameterUpdate` yet) | 0 |
| Execution / Risk | RQ-009–RQ-011 | 1 (RQ-011; RQ-009/RQ-010 not started — see Non-Goals in `execution/README.md`) | 0 |
| Optimizer / Backtest | RQ-012–RQ-015 | 1 (RQ-012, partial — cadence/isolation only; RQ-013/RQ-014 deliberately not built, see `optimizer/README.md`) | 0 |
| Analytics / News | RQ-016–RQ-017 | 1 (RQ-017, partial — logic wired but fed no real data, see `docs/ARCHITECTURE_SUMMARY.md` §5) | 0 |
| Platform / Process | RQ-018–RQ-020 | 3 (RQ-018, RQ-019 locally verified, RQ-020 process) | 0 |
| Orchestration (Phase 10) | RQ-021–RQ-022 | 2 (RQ-021; RQ-022 partial — superseded by RQ-029/RQ-030, baseline rollover still not wired) | 0 |
| Production Hardening (Phase 11a–11e) | RQ-023–RQ-034 | 11 (RQ-023/RQ-024/RQ-028/RQ-030/RQ-031/RQ-033 fully implemented and tested; RQ-025/RQ-026/RQ-027/RQ-029/RQ-032 implemented and tested but each has an unwired or narrower-than-spec remainder — see their notes; RQ-034 not implemented) | 0 |

Phase 0 was documentation-only by directive, so its 0/0 implemented/verified
counts were expected, not a gap. Phase 1 landed `config/config_manager.py`
(RQ-018). Phase 2 landed `storage/db_engine.py` + `storage/state_manager.py`
(RQ-005, partial — see that row's notes on the narrowed crash-recovery
guarantee). Phase 3 landed `broker/mt5_gateway.py` (RQ-001; RQ-002 partial —
offset resolution and the GMT window exist, periodic DST-drift re-validation
from RR-003 does not yet). Phase 4 landed `indicators/math_engine.py`
(RQ-007) and `strategy/trend_filter.py` (RQ-008, trend-alignment filter).
Phase 5 landed `strategy/execution_triggers.py` (breakout/pullback/wick-fill
signals, RQ-008) and `indicators/math_engine.sma()`. Phase 6 landed the new
`risk/risk_manager.py` (equity-based compounding), `execution/position_manager.py`
(partial close, breakeven, ATR trailing — RQ-011), and
`broker/mt5_gateway.py`'s `submit_position_action()`. Phase 7 landed
`news/news_engine.py` (calendar feed client, macro-event blackout window,
News-API-down fail-safe — RQ-017 partial). Phase 8 landed
`optimizer/self_learning.py` (Saturday-gated rule-based parameter shift +
Monte Carlo bootstrap — RQ-012 partial; a new `parameter_history` storage
table) — deliberately simpler than ADR-0004's anchored-WFO design, per that
module's README. Phase 9 landed the project's first formal automated test
suite (`tests/unit/test_unit.py`, `tests/integration/test_integration.py`, 97 tests, 97.27%
total coverage), replacing every prior phase's ad hoc verification scripts
with committed, CI-runnable tests.

**Phase 10 landed `main.py`** (the master FSM orchestration loop, RQ-021,
RQ-022 partial) and the `broker/mt5_gateway.py` methods needed to make it
real (`get_account_state`, `get_bars`, `submit_market_order`) — 131 tests,
95.68% total coverage. This was the **final phase of the original 10-phase
roadmap**; `docs/ARCHITECTURE_SUMMARY.md` is the consolidated capstone
document listing every open gap across all ten phases and the concrete
checklist before a first live/demo run.

**Phase 11a landed** `config/config_manager.py`'s `ConfigValidator`,
`config/secret_redaction.py`, and `container.py`'s `ApplicationContainer`
(RQ-023, RQ-024 — both new rows, `docs/PRODUCTION_SPEC.md`'s first
sub-phase) — 147 tests, 96.50% total coverage.

**Phase 11b landed** `news/calendar_provider.py` (`CalendarProvider`
Protocol, `RateLimiter`, `NetworkCalendarProvider`,
`OfflineSnapshotCalendarProvider`, `CalendarProviderChain`,
`build_calendar_provider_chain()`), `config/calendar_config.py`
(`CalendarConfig.from_env()`), and `broker/clock_provider.py`
(`ClockProvider` Protocol, `MT5ClockProvider`) — RQ-025, RQ-026, both new
rows, `docs/PRODUCTION_SPEC.md` §2/§3. Both are wired into `container.py`'s
`ApplicationContainer` as `calendar_provider`/`clock_provider` via
constructor injection, but neither is yet consumed by `main.py`'s live
loop (flagged in both new rows and `docs/ARCHITECTURE_SUMMARY.md` §5).

**Phase 11c landed** `storage/migrations.py` (`apply_pending_migrations()`,
a lightweight schema-migration framework), `storage/state_manager.py`'s
`OrderLifecycleState`/`OrderEvent`/`record_order_event()`/`get_order_events()`/
`get_latest_order_event()`/`get_order_ledger_state()` (the append-only
Event Store + `order_ledger` projection), `broker/mt5_gateway.py`'s
`is_ticket_still_open()`, and `execution/validation.py`'s
`SeverityLevel`/`ValidationResult`/`check_duplicate_order_before_retry()`
— RQ-027, RQ-028, both new rows, `docs/PRODUCTION_SPEC.md` §4/§5. Unlike
Phase 11b, this phase also wires the pre-flight ledger write directly into
`main.py`'s live loop via the new `submit_with_pre_flight_ledger()`,
around both of its real broker-submission call sites (a new market order,
and every position action) — the one part of this sub-phase that changes
`main.py`'s actual runtime behavior (additively; the happy path and
rejection path are otherwise unchanged). The duplicate-order retry-audit
gate itself remains unwired, since no automated retry loop exists in
`main.py` to protect — that loop's backoff cadence is
`docs/PRODUCTION_SPEC.md` §7's explicit domain, a later sub-phase.

**Phase 11d landed** `risk/drawdown_fsm.py` (`DrawdownState`,
`DrawdownEvent`, `EquityBaselines`, `classify_drawdown_event()`,
`transition_drawdown_state()`, `blocks_new_entries()`,
`blocks_position_management()`, `decide_hard_lock_response()`),
`config/feature_flags.py` (`FeatureFlags`, `FeatureFlagManager`), and
`execution/position_manager.py`'s `build_emergency_liquidation_action()`
— RQ-029, RQ-030, both new rows, `docs/PRODUCTION_SPEC.md` §6, superseding
RQ-022's single-tier `TradingState.HALTED`/`check_drawdown_breach()`
(Phase 10). Unlike RQ-025/RQ-026/RQ-027's still-unwired remainders, this
phase's FSM is wired *live* into `main.py`'s `run_bar_close_cycle()` —
every bar-close cycle now transitions through it, and a `HARD_LOCK` breach
with `liquidate_on_hard_lock=True` really does emit an emergency
liquidation payload. The one remaining gap is the human-facing side:
`MANUAL_RESET_CONFIRMED` has no live control channel yet, so a real
`HARD_LOCK` today can only be cleared by a process restart, not a running
override (`docs/ARCHITECTURE_SUMMARY.md` §5).

**Phase 11e landed** `resilience/backoff.py` (`compute_backoff_delays()`,
`retry_with_backoff()`, `RetryBudgetExhaustedError`), `storage/db_engine.py`'s
`PRAGMA busy_timeout`, `storage/migrations.py`'s `audit_trail` table
(migration version 2) and `storage/state_manager.py`'s
`AuditActionType`/`AuditEvent`/`record_audit_event()`/`get_audit_trail()`,
`broker/mt5_gateway.py`'s `resolve_position_audit()`/`DisasterRecoveryPlan`,
`container.py`'s Disaster Recovery wiring (`initial_drawdown_state`), and
`main.py`'s `_seed_initial_fsm_context()` — RQ-031, RQ-032, RQ-033, RQ-034
(the last **not implemented**, flagged prominently rather than silently
dropped — see its own row), `docs/PRODUCTION_SPEC.md` §7 — plus the
`tests/unit`/`tests/integration`/`tests/chaos`/`tests/stress`
reorganization (not its own RQ row; a process/tooling change, like
RQ-019/RQ-020). This is the final Phase 11 sub-phase: `VERSION` now
reads `1.0.0-RC1`.

No row in this matrix is `VERIFIED` — per this document's Legend, that
status additionally requires *observed paper-trading behavior*, which
cannot exist until a human runs this system against a real demo/live
account, following `docs/ARCHITECTURE_SUMMARY.md` §7 and
`docs/DEPLOYMENT.md`'s explicit promotion gates.
