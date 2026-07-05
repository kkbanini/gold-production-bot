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
| RQ-001 | System shall interface exclusively with MetaTrader 5 via an internal `BrokerGateway` abstraction; no other module may import the MT5 package. | `broker/` | ADR-0002 | `broker/mt5_gateway.py` implemented Phase 3 as the sole importer of `MetaTrader5`; `tests/test_unit.py::TestBrokerPureLogic` + `tests/test_integration.py::TestMT5ServerDropouts`/`TestOrderActionSubmission` (Phase 9) exercise it via a `FakeMT5` substitute; a dedicated static "no other module imports MetaTrader5" lint/test still does not exist | IMPLEMENTED |
| RQ-002 | All timestamps crossing the broker boundary shall be normalized to UTC, correcting for broker server time vs. host local time and broker-side DST. | `broker/` | ADR-0002, `docs/RESEARCH.md` §Time & Session Normalization | `MT5Gateway.broker_utc_offset`/`is_within_execution_window()` implemented Phase 3; formally tested Phase 9 (`tests/test_unit.py::TestBrokerPureLogic::test_execution_window_boundaries`, `tests/test_integration.py::TestMT5ServerDropouts`); periodic re-validation/DST-drift alerting (RR-003) still not implemented | IMPLEMENTED (partial) |
| RQ-003 | All cross-module communication shall occur through immutable, typed events on a single `EventBus`; no module shall mutate another module's state directly. | core / all | ADR-0001 | **Not implemented as specified** — Phase 10's `main.py` is a single-threaded poll-and-decide loop, not a full `EventBus`; it does satisfy the *spirit* (one writer, one dispatch loop) without the formal event-typed infrastructure ADR-0001 describes | SPECIFIED |
| RQ-004 | Trading state mutations shall occur on exactly one logical thread of control (single-writer core loop). | core | ADR-0001 | `main.py`'s `main()` loop is single-threaded by construction (Phase 10) — satisfied in practice, though not via the formal `core`/`EventBus` design | IMPLEMENTED (informally) |
| RQ-005 | Every event and derived state transition shall be persisted transactionally (ACID) such that a crash mid-write cannot desynchronize the event log from derived state. | `storage/` | ADR-0003 | `storage/state_manager.py` implemented Phase 2; formally tested Phase 9 (`tests/test_integration.py::TestCrashRecovery`, `TestDatabaseRollback`) for the FSM-state-snapshot and transactional-rollback cases; full event-log/derived-table divergence guarantee remains narrower than originally specified (see `storage/README.md` "Simplification" note) | IMPLEMENTED (partial) |
| RQ-006 | The system shall be able to fully reconstruct current trading state by replaying the persisted event log from the last snapshot. | `storage/`, core | ADR-0001, ADR-0003 | Remains single-snapshot recovery (no intermediate event replay), since no `EventBus`/event log was built in Phase 10 either (see RQ-003) | SPECIFIED |
| RQ-007 | Indicator calculations shall be pure functions over numpy arrays with no I/O or broker dependency. | `indicators/` | `docs/API_SPEC.md` §1, `docs/RESEARCH.md` | `indicators/math_engine.py` (`sma`/`ema`/`atr`/`adx`) implemented Phase 4-5; formally tested Phase 9 (`tests/test_unit.py::TestMathEngine`, every function cross-checked against an independent pure-Python reference, plus the boundary/error-path checks that caught the Phase 4 ADX bug — see `CHANGELOG.md` §0.5.0/§0.10.0) | IMPLEMENTED |
| RQ-008 | Strategy signal generation shall be deterministic given identical `Bar`/`Tick` history and an identical `ParameterUpdate`. | `strategy/` | `docs/API_SPEC.md` §1 | `strategy/trend_filter.py` (Phase 4) + `strategy/execution_triggers.py` (Phase 5) implemented; formally tested Phase 9 (`tests/test_unit.py::TestTrendFilter`, `TestExecutionTriggers`); combining the three independent trigger signals into one entry decision is deferred to `execution/` | IMPLEMENTED (partial — no `ParameterUpdate` consumption yet) |
| RQ-009 | Every `Signal` shall pass through a pre-trade risk gate (`RiskGateDecision`) before becoming an `Order`; no direct `Signal → Order` path shall exist. | `execution/`, `risk/` | `docs/API_SPEC.md` §1, `docs/RISK_REGISTER.md` | Not implemented; no test exists | SPECIFIED |
| RQ-010 | Order submission shall guard against excess slippage and reject/re-route fills outside a configured tolerance band. | `execution/` | `docs/RISK_REGISTER.md` (RR-006) | Not implemented; no test exists | SPECIFIED |
| RQ-011 | The system shall support partial position closures as a first-class execution operation. | `execution/`, `broker/` | `docs/API_SPEC.md` §3 (`close_position`) | `execution/position_manager.py`'s `evaluate_partial_close_and_breakeven()` + `broker/mt5_gateway.py`'s `submit_position_action()` implemented Phase 6; formally tested Phase 9 (`tests/test_unit.py::TestPositionManager`, `tests/test_integration.py::TestOrderActionSubmission`); wired into `main.py`'s live orchestration loop Phase 10 (`tests/test_unit.py::TestBarCloseCycle`) | IMPLEMENTED |
| RQ-012 | Strategy parameter re-optimization shall run offline (weekend cadence) and shall never mutate live strategy parameters mid-week or mid-signal-evaluation. | `optimizer/` | ADR-0001 §5, ADR-0004 | `optimizer/self_learning.py`'s Saturday-only `CronTrigger` + independent runtime re-check, and the isolation guarantee implemented Phase 8; formally verified against a real SQLite database Phase 9 (`tests/test_integration.py::TestOptimizerIsolation`); no wiring to `strategy/`'s actual live parameters exists yet | IMPLEMENTED (partial — cadence/isolation only, no live-parameter wiring) |
| RQ-013 | Parameter re-optimization shall use anchored walk-forward validation exclusively; non-anchored or shuffled time-series validation is architecturally forbidden. | `optimizer/`, `backtester/` | ADR-0004 | **Not implemented** — Phase 8 built a deliberately simpler rule-based single-parameter shift against live/paper ledger metrics instead (see `optimizer/README.md` "Simplification vs. the original ADR-0004 design"); anchored WFO remains unimplemented pending a `backtester/` module, which is unscheduled in the current roadmap (Phase 2 note); `tests/optimizer/test_anchored_fold_construction.py` has no phase assigned | SPECIFIED |
| RQ-014 | The optimizer shall refuse to emit a `ParameterUpdateEvent` if Deflated Sharpe Ratio or IS/OOS efficiency ratio gates fail. | `optimizer/` | ADR-0004, `docs/RESEARCH.md` §Promotion Gates | **Not implemented** — same Simplification note as RQ-013; Phase 8's `decide_parameter_shift()` uses simple win-rate/profit-factor thresholds, not DSR/IS-OOS gates, and there is no `ParameterUpdateEvent`/`core` to emit one to; `tests/optimizer/test_promotion_gate_enforcement.py` has no phase assigned | SPECIFIED |
| RQ-015 | The backtester shall support both a fast vectorized mode (research iteration) and an event-driven mode sharing live `strategy/`/`execution/` code (validation/parity). | `backtester/` | ADR-0001, ADR-0002 | `tests/backtester/test_vectorized_vs_event_parity.py` | **UNSCHEDULED** — `backtester/` does not appear in the current 10-phase roadmap; Phase 8's Monte Carlo bootstrap validates the optimizer's ledger metrics but is not the full vectorized/event-driven backtester ADR-0004 assumes. Flagging for a roadmap decision. |
| RQ-016 | Performance analytics shall compute Sharpe, Sortino, MAR, and maximum drawdown from the persisted equity curve, not from in-memory ad hoc state. | `analytics/` | `docs/API_SPEC.md` §5 | `tests/analytics/test_metrics_from_ledger.py` | **UNSCHEDULED** — `analytics/` does not appear in the current 10-phase roadmap; Phase 8's self-learning optimizer reads "SQLite ledger metrics" directly, which may end up substituting for a dedicated `analytics/` module. Flagging for a roadmap decision. |
| RQ-017 | High-impact economic calendar events shall trigger a pre-trade blackout window enforced by the risk gate. | `news/`, `execution/` | `docs/API_SPEC.md` §2 (`NewsWindow`), `docs/RISK_REGISTER.md` (RR-009) | `news/news_engine.py`'s `is_trade_entry_locked()` (±30 min NFP/CPI/FOMC blackout) implemented Phase 7, formally tested Phase 9; `main.py`'s `decide_entry_signal()` consults it Phase 10, **but the live loop never actually calls `fetch_calendar_events()`** — `_fetch_market_snapshot()` passes a hardcoded empty event list, so the blackout is currently inert in practice (see `docs/ARCHITECTURE_SUMMARY.md` §5) | IMPLEMENTED (partial — logic wired but fed no real data) |
| RQ-018 | Secrets (broker credentials, API keys) shall never be committed to source control and shall be sourced from environment/`.env` only, and the system shall refuse to boot if any required configuration key is missing. | `config/` | `docs/RISK_REGISTER.md` (RR-001, RR-012), `docs/DEPLOYMENT.md` | `config/config_manager.py`'s `ConfigManager.load()` implemented Phase 1; formally tested Phase 9 (`tests/test_unit.py::TestConfigManager`) | IMPLEMENTED |
| RQ-019 | CI shall block merge on any Ruff lint failure, Mypy strict-mode failure, or Pytest failure, and shall report coverage. | `.github/workflows/` | this document | `.github/workflows/ci.yml`'s exact invocation (`pytest --cov=. --cov-report=term-missing --cov-fail-under=90`) run locally through Phase 10: 131 tests pass, 95.68% total coverage; the workflow itself has still not executed in a real GitHub Actions run (no push to a remote has occurred) | IMPLEMENTED (locally verified, not yet CI-run) |
| RQ-020 | Every ADR, API contract change, and risk register update shall be reflected in `CHANGELOG.md` with a correct SemVer bump. | project-wide | `CHANGELOG.md` policy | manual release-checklist review (`docs/RUNBOOK.md`) | IMPLEMENTED (process, Phase 0) |
| RQ-021 | The system shall measure per-bar-close processing time and flag any cycle exceeding a 200ms cap. | `main.py` | `docs/ARCHITECTURE_SUMMARY.md` §2 | `main.py`'s `evaluate_processing_time()` implemented and formally tested Phase 10 (`tests/test_unit.py::TestProcessingCap`, `TestBarCloseCycle::test_processing_cap_breach_is_logged`). An operational metric (logs a warning), not a trading halt | IMPLEMENTED |
| RQ-022 | The system shall hard-lock new trade entries when account drawdown exceeds 5% daily, 10% weekly, or 20% monthly. | `main.py` | `docs/RISK_REGISTER.md` (risk-of-ruin), `docs/ARCHITECTURE_SUMMARY.md` §2/§5 | `main.py`'s `check_drawdown_breach()` implemented and formally tested Phase 10 (`tests/test_unit.py::TestDrawdownBreach`, `TestBarCloseCycle::test_drawdown_breach_halts_and_blocks_entry`/`test_halted_state_stays_halted_regardless_of_recovery`); **equity baselines are seeded once at process start and never rolled over at UTC day/week/month boundaries**, so the "daily"/"weekly"/"monthly" framing degrades the longer the process runs uninterrupted — flagged as a must-fix-before-live item in `docs/ARCHITECTURE_SUMMARY.md` §5 | IMPLEMENTED (partial — baseline rollover not wired) |
| RQ-023 | Credentials shall be validated at boot for presence, placeholder/default-value leakage, and syntactic validity, triggering a fatal panic on any violation; secrets shall be redacted from structured logs. | `config/` | `docs/PRODUCTION_SPEC.md` §1 | `config/config_manager.py`'s `ConfigValidator` and `config/secret_redaction.py`'s `SecretRedactingFilter` implemented and formally tested Phase 11a (`tests/test_unit.py::TestConfigManager`/`TestConfigValidator`/`TestSecretRedaction`, `tests/test_integration.py::TestApplicationContainer::test_build_attaches_secret_redaction_to_root_logger`) | IMPLEMENTED |
| RQ-024 | Long-lived services (config, storage, broker) shall be constructed and wired through a centralized composition root using constructor-based dependency injection. | `container.py` | `docs/PRODUCTION_SPEC.md` "Core Orchestration Directive" #1 | `container.py`'s `ApplicationContainer.build()` implemented Phase 11a, replacing `main.py`'s inline `bootstrap_system()`; formally tested (`tests/test_integration.py::TestApplicationContainer`) | IMPLEMENTED |

## Coverage Summary (as of Phase 11a)

| Category | Requirements Specified | Implemented | Verified |
|---|---|---|---|
| Architecture / Core | RQ-001–RQ-006 | 4 (RQ-001, RQ-002 partial, RQ-004 informally, RQ-005 partial) | 0 |
| Strategy / Indicators | RQ-007–RQ-008 | 2 (RQ-007; RQ-008 partial — no `ParameterUpdate` yet) | 0 |
| Execution / Risk | RQ-009–RQ-011 | 1 (RQ-011; RQ-009/RQ-010 not started — see Non-Goals in `execution/README.md`) | 0 |
| Optimizer / Backtest | RQ-012–RQ-015 | 1 (RQ-012, partial — cadence/isolation only; RQ-013/RQ-014 deliberately not built, see `optimizer/README.md`) | 0 |
| Analytics / News | RQ-016–RQ-017 | 1 (RQ-017, partial — logic wired but fed no real data, see `docs/ARCHITECTURE_SUMMARY.md` §5) | 0 |
| Platform / Process | RQ-018–RQ-020 | 3 (RQ-018, RQ-019 locally verified, RQ-020 process) | 0 |
| Orchestration (Phase 10) | RQ-021–RQ-022 | 2 (RQ-021; RQ-022 partial — baseline rollover not wired) | 0 |
| Production Hardening (Phase 11a, new) | RQ-023–RQ-024 | 2 (both fully implemented and tested) | 0 |

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
suite (`tests/test_unit.py`, `tests/test_integration.py`, 97 tests, 97.27%
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
sub-phase) — 147 tests, 96.50% total coverage. No row in this matrix is
`VERIFIED` — per this document's Legend, that status additionally requires
*observed paper-trading behavior*, which cannot exist until a human runs
this system against a real demo/live account, following
`docs/ARCHITECTURE_SUMMARY.md` §7 and `docs/DEPLOYMENT.md`'s explicit
promotion gates.
