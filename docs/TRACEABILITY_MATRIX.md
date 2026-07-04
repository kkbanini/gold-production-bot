# Traceability Matrix

Maps every top-level system requirement to its owning module, governing ADR, and the
test(s) that will verify it. This matrix is a living document — every phase updates
the rows for the modules it touches and adds new requirement rows as the spec is
elaborated. Phase 0 seeds the matrix with all Phase-0-visible requirements; module
rows below marked `Test: (pending Phase N)` have no code yet, by directive, and no
test exists until that phase lands.

## Legend

- **Req ID** — stable identifier, never renumbered even if a requirement is dropped
  (dropped rows are struck through and retained for audit history, not deleted).
- **Status** — `SPECIFIED` (documented, no code), `IMPLEMENTED` (code + passing
  test), `VERIFIED` (implemented + reviewed + observed in paper trading).

| Req ID | Requirement | Owning Module | Governing ADR / Spec | Test Reference | Status |
|---|---|---|---|---|---|
| RQ-001 | System shall interface exclusively with MetaTrader 5 via an internal `BrokerGateway` abstraction; no other module may import the MT5 package. | `broker/` | ADR-0002 | `tests/broker/test_import_boundary.py` (pending Phase 2) | SPECIFIED |
| RQ-002 | All timestamps crossing the broker boundary shall be normalized to UTC, correcting for broker server time vs. host local time and broker-side DST. | `broker/` | ADR-0002, `docs/RESEARCH.md` §Time & Session Normalization | `tests/broker/test_time_normalization.py` (pending Phase 2) | SPECIFIED |
| RQ-003 | All cross-module communication shall occur through immutable, typed events on a single `EventBus`; no module shall mutate another module's state directly. | core / all | ADR-0001 | `tests/core/test_single_writer_invariant.py` (pending Phase 2) | SPECIFIED |
| RQ-004 | Trading state mutations shall occur on exactly one logical thread of control (single-writer core loop). | core | ADR-0001 | `tests/core/test_concurrency_model.py` (pending Phase 2) | SPECIFIED |
| RQ-005 | Every event and derived state transition shall be persisted transactionally (ACID) such that a crash mid-write cannot desynchronize the event log from derived state. | `storage/` | ADR-0003 | `tests/storage/test_crash_recovery.py` (pending Phase 3) | SPECIFIED |
| RQ-006 | The system shall be able to fully reconstruct current trading state by replaying the persisted event log from the last snapshot. | `storage/`, core | ADR-0001, ADR-0003 | `tests/storage/test_event_replay.py` (pending Phase 3) | SPECIFIED |
| RQ-007 | Indicator calculations shall be pure functions over numpy arrays with no I/O or broker dependency. | `indicators/` | `docs/API_SPEC.md` §1, `docs/RESEARCH.md` | `tests/indicators/test_purity.py` (pending Phase 4) | SPECIFIED |
| RQ-008 | Strategy signal generation shall be deterministic given identical `Bar`/`Tick` history and an identical `ParameterUpdate`. | `strategy/` | `docs/API_SPEC.md` §1 | `tests/strategy/test_determinism.py` (pending Phase 4) | SPECIFIED |
| RQ-009 | Every `Signal` shall pass through a pre-trade risk gate (`RiskGateDecision`) before becoming an `Order`; no direct `Signal → Order` path shall exist. | `execution/` | `docs/API_SPEC.md` §1, `docs/RISK_REGISTER.md` | `tests/execution/test_risk_gate_mandatory.py` (pending Phase 5) | SPECIFIED |
| RQ-010 | Order submission shall guard against excess slippage and reject/re-route fills outside a configured tolerance band. | `execution/` | `docs/RISK_REGISTER.md` (RR-006) | `tests/execution/test_slippage_guard.py` (pending Phase 5) | SPECIFIED |
| RQ-011 | The system shall support partial position closures as a first-class execution operation. | `execution/`, `broker/` | `docs/API_SPEC.md` §3 (`close_position`) | `tests/execution/test_partial_closure.py` (pending Phase 5) | SPECIFIED |
| RQ-012 | Strategy parameter re-optimization shall run offline (weekend cadence) and shall never mutate live strategy parameters mid-week or mid-signal-evaluation. | `optimizer/` | ADR-0001 §5, ADR-0004 | `tests/optimizer/test_apply_boundary.py` (pending Phase 6) | SPECIFIED |
| RQ-013 | Parameter re-optimization shall use anchored walk-forward validation exclusively; non-anchored or shuffled time-series validation is architecturally forbidden. | `optimizer/`, `backtester/` | ADR-0004 | `tests/optimizer/test_anchored_fold_construction.py` (pending Phase 6) | SPECIFIED |
| RQ-014 | The optimizer shall refuse to emit a `ParameterUpdateEvent` if Deflated Sharpe Ratio or IS/OOS efficiency ratio gates fail. | `optimizer/` | ADR-0004, `docs/RESEARCH.md` §Promotion Gates | `tests/optimizer/test_promotion_gate_enforcement.py` (pending Phase 6) | SPECIFIED |
| RQ-015 | The backtester shall support both a fast vectorized mode (research iteration) and an event-driven mode sharing live `strategy/`/`execution/` code (validation/parity). | `backtester/` | ADR-0001, ADR-0002 | `tests/backtester/test_vectorized_vs_event_parity.py` (pending Phase 7) | SPECIFIED |
| RQ-016 | Performance analytics shall compute Sharpe, Sortino, MAR, and maximum drawdown from the persisted equity curve, not from in-memory ad hoc state. | `analytics/` | `docs/API_SPEC.md` §5 | `tests/analytics/test_metrics_from_ledger.py` (pending Phase 8) | SPECIFIED |
| RQ-017 | High-impact economic calendar events shall trigger a pre-trade blackout window enforced by the risk gate. | `news/`, `execution/` | `docs/API_SPEC.md` §2 (`NewsWindow`), `docs/RISK_REGISTER.md` (RR-009) | `tests/news/test_blackout_enforcement.py` (pending Phase 9) | SPECIFIED |
| RQ-018 | Secrets (broker credentials, API keys) shall never be committed to source control and shall be sourced from environment/`.env` only, and the system shall refuse to boot if any required configuration key is missing. | `config/` | `docs/RISK_REGISTER.md` (RR-001, RR-012), `docs/DEPLOYMENT.md` | `config/config_manager.py`'s `ConfigManager.load()` implemented and manually verified Phase 1 (see `CHANGELOG.md` §0.2.0); `tests/config/test_config_manager.py` formal suite pending a future phase | IMPLEMENTED |
| RQ-019 | CI shall block merge on any Ruff lint failure, Mypy strict-mode failure, or Pytest failure, and shall report coverage. | `.github/workflows/` | this document | CI pipeline itself is the verification | SPECIFIED |
| RQ-020 | Every ADR, API contract change, and risk register update shall be reflected in `CHANGELOG.md` with a correct SemVer bump. | project-wide | `CHANGELOG.md` policy | manual release-checklist review (`docs/RUNBOOK.md`) | IMPLEMENTED (process, Phase 0) |

## Coverage Summary (as of Phase 1)

| Category | Requirements Specified | Implemented | Verified |
|---|---|---|---|
| Architecture / Core | RQ-001–RQ-006 | 0 | 0 |
| Strategy / Indicators | RQ-007–RQ-008 | 0 | 0 |
| Execution / Risk | RQ-009–RQ-011 | 0 | 0 |
| Optimizer / Backtest | RQ-012–RQ-015 | 0 | 0 |
| Analytics / News | RQ-016–RQ-017 | 0 | 0 |
| Platform / Process | RQ-018–RQ-020 | 2 (RQ-018 code + manual verification; RQ-020 process) | 0 |

Phase 0 was documentation-only by directive, so its 0/0 implemented/verified
counts were expected, not a gap. Phase 1 lands the first real implementation
(`config/config_manager.py`, RQ-018), moving it to `IMPLEMENTED`; it is not yet
`VERIFIED` because that status requires a formal automated test suite and
observed paper-trading behavior, neither of which apply to a config-loading
module in isolation.
