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
`tests/` (formal automated suite) · Phase 10 `main.py` (core FSM loop).

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
| RQ-001 | System shall interface exclusively with MetaTrader 5 via an internal `BrokerGateway` abstraction; no other module may import the MT5 package. | `broker/` | ADR-0002 | `broker/mt5_gateway.py` implemented Phase 3 as the sole importer of `MetaTrader5`; formal `tests/broker/test_import_boundary.py` pending Phase 9 | IMPLEMENTED |
| RQ-002 | All timestamps crossing the broker boundary shall be normalized to UTC, correcting for broker server time vs. host local time and broker-side DST. | `broker/` | ADR-0002, `docs/RESEARCH.md` §Time & Session Normalization | `MT5Gateway.broker_utc_offset`/`is_within_execution_window()` implemented and ad hoc verified Phase 3 (see `CHANGELOG.md` §0.4.0) against a fake `MetaTrader5` substitute, since no live terminal exists in this environment; periodic re-validation/DST-drift alerting (RR-003) not yet implemented; formal `tests/broker/test_time_normalization.py` pending Phase 9 | IMPLEMENTED (partial) |
| RQ-003 | All cross-module communication shall occur through immutable, typed events on a single `EventBus`; no module shall mutate another module's state directly. | core / all | ADR-0001 | `tests/core/test_single_writer_invariant.py` (pending Phase 10) | SPECIFIED |
| RQ-004 | Trading state mutations shall occur on exactly one logical thread of control (single-writer core loop). | core | ADR-0001 | `tests/core/test_concurrency_model.py` (pending Phase 10) | SPECIFIED |
| RQ-005 | Every event and derived state transition shall be persisted transactionally (ACID) such that a crash mid-write cannot desynchronize the event log from derived state. | `storage/` | ADR-0003 | `storage/state_manager.py` implemented and ad hoc verified Phase 2 (see `CHANGELOG.md` §0.3.0) for the FSM-state-snapshot case; full event-log/derived-table divergence guarantee remains narrower than originally specified (see `storage/README.md` "Simplification" note); formal `tests/storage/test_crash_recovery.py` pending Phase 9 | IMPLEMENTED (partial) |
| RQ-006 | The system shall be able to fully reconstruct current trading state by replaying the persisted event log from the last snapshot. | `storage/`, core | ADR-0001, ADR-0003 | Narrowed this phase to single-snapshot recovery (no intermediate event replay, since no `EventBus`/event log exists yet — pending Phase 10); `tests/storage/test_event_replay.py` pending Phase 9 | SPECIFIED |
| RQ-007 | Indicator calculations shall be pure functions over numpy arrays with no I/O or broker dependency. | `indicators/` | `docs/API_SPEC.md` §1, `docs/RESEARCH.md` | `indicators/math_engine.py` (`sma`/`ema`/`atr`/`adx`) implemented Phase 4-5; ad hoc verified against independent pure-Python references + boundary/error-path checks (see `CHANGELOG.md` §0.5.0, which also documents a real ADX normalization bug caught this way); formal `tests/indicators/test_purity.py` pending Phase 9 | IMPLEMENTED |
| RQ-008 | Strategy signal generation shall be deterministic given identical `Bar`/`Tick` history and an identical `ParameterUpdate`. | `strategy/` | `docs/API_SPEC.md` §1 | `strategy/trend_filter.py` (Phase 4) + `strategy/execution_triggers.py` (Phase 5: `detect_breakout`/`detect_pullback`/`analyze_wick_fill`) implemented and ad hoc verified (see `CHANGELOG.md` §0.5.0/§0.6.0); pattern shapes for breakout/pullback authored into `docs/RESEARCH.md` §8 this phase, flagged for review since no prior spec existed for their exact shape; combining the three independent trigger signals into one entry decision is deferred to `execution/`; formal `tests/strategy/test_determinism.py` pending Phase 9 | IMPLEMENTED (partial — no `ParameterUpdate` consumption yet) |
| RQ-009 | Every `Signal` shall pass through a pre-trade risk gate (`RiskGateDecision`) before becoming an `Order`; no direct `Signal → Order` path shall exist. | `execution/`, `risk/` | `docs/API_SPEC.md` §1, `docs/RISK_REGISTER.md` | `tests/execution/test_risk_gate_mandatory.py` (pending Phase 9) | SPECIFIED (pending Phase 6 implementation) |
| RQ-010 | Order submission shall guard against excess slippage and reject/re-route fills outside a configured tolerance band. | `execution/` | `docs/RISK_REGISTER.md` (RR-006) | `tests/execution/test_slippage_guard.py` (pending Phase 9) | SPECIFIED (pending Phase 6 implementation) |
| RQ-011 | The system shall support partial position closures as a first-class execution operation. | `execution/`, `broker/` | `docs/API_SPEC.md` §3 (`close_position`) | `execution/position_manager.py`'s `evaluate_partial_close_and_breakeven()` + `broker/mt5_gateway.py`'s `submit_position_action()` (`TRADE_ACTION_DEAL` path) implemented Phase 6 and ad hoc verified against a fake `MetaTrader5` substitute (see `CHANGELOG.md` §0.7.0); formal `tests/execution/test_partial_closure.py` pending Phase 9 | IMPLEMENTED |
| RQ-012 | Strategy parameter re-optimization shall run offline (weekend cadence) and shall never mutate live strategy parameters mid-week or mid-signal-evaluation. | `optimizer/` | ADR-0001 §5, ADR-0004 | `optimizer/self_learning.py`'s Saturday-only `CronTrigger` + independent runtime re-check, and the isolation guarantee (only ever writes `parameter_history`, never `system_state`/open `trade_ledger` rows) implemented Phase 8 and verified against a real SQLite database (see `CHANGELOG.md` §0.9.0); no wiring to `strategy/`'s actual live parameters exists yet, so "never mutate live parameters" currently holds trivially (nothing is wired to mutate); formal `tests/optimizer/test_apply_boundary.py` pending Phase 9 | IMPLEMENTED (partial — cadence/isolation only, no live-parameter wiring) |
| RQ-013 | Parameter re-optimization shall use anchored walk-forward validation exclusively; non-anchored or shuffled time-series validation is architecturally forbidden. | `optimizer/`, `backtester/` | ADR-0004 | **Not implemented** — Phase 8 built a deliberately simpler rule-based single-parameter shift against live/paper ledger metrics instead (see `optimizer/README.md` "Simplification vs. the original ADR-0004 design"); anchored WFO remains unimplemented pending a `backtester/` module, which is unscheduled in the current roadmap (Phase 2 note); `tests/optimizer/test_anchored_fold_construction.py` has no phase assigned | SPECIFIED |
| RQ-014 | The optimizer shall refuse to emit a `ParameterUpdateEvent` if Deflated Sharpe Ratio or IS/OOS efficiency ratio gates fail. | `optimizer/` | ADR-0004, `docs/RESEARCH.md` §Promotion Gates | **Not implemented** — same Simplification note as RQ-013; Phase 8's `decide_parameter_shift()` uses simple win-rate/profit-factor thresholds, not DSR/IS-OOS gates, and there is no `ParameterUpdateEvent`/`core` to emit one to; `tests/optimizer/test_promotion_gate_enforcement.py` has no phase assigned | SPECIFIED |
| RQ-015 | The backtester shall support both a fast vectorized mode (research iteration) and an event-driven mode sharing live `strategy/`/`execution/` code (validation/parity). | `backtester/` | ADR-0001, ADR-0002 | `tests/backtester/test_vectorized_vs_event_parity.py` | **UNSCHEDULED** — `backtester/` does not appear in the current 10-phase roadmap; Phase 8's Monte Carlo bootstrap validates the optimizer's ledger metrics but is not the full vectorized/event-driven backtester ADR-0004 assumes. Flagging for a roadmap decision. |
| RQ-016 | Performance analytics shall compute Sharpe, Sortino, MAR, and maximum drawdown from the persisted equity curve, not from in-memory ad hoc state. | `analytics/` | `docs/API_SPEC.md` §5 | `tests/analytics/test_metrics_from_ledger.py` | **UNSCHEDULED** — `analytics/` does not appear in the current 10-phase roadmap; Phase 8's self-learning optimizer reads "SQLite ledger metrics" directly, which may end up substituting for a dedicated `analytics/` module. Flagging for a roadmap decision. |
| RQ-017 | High-impact economic calendar events shall trigger a pre-trade blackout window enforced by the risk gate. | `news/`, `execution/` | `docs/API_SPEC.md` §2 (`NewsWindow`), `docs/RISK_REGISTER.md` (RR-009) | `news/news_engine.py`'s `is_trade_entry_locked()` (±30 min NFP/CPI/FOMC blackout) and `apply_news_feed_fail_safe()` implemented Phase 7 and ad hoc verified against a faked `requests.get` (see `CHANGELOG.md` §0.8.0); not yet wired into `execution/`'s actual pre-trade risk gate (that gate itself doesn't exist yet); formal `tests/news/test_blackout_enforcement.py` pending Phase 9 | IMPLEMENTED (partial — logic only, not yet wired to a risk gate) |
| RQ-018 | Secrets (broker credentials, API keys) shall never be committed to source control and shall be sourced from environment/`.env` only, and the system shall refuse to boot if any required configuration key is missing. | `config/` | `docs/RISK_REGISTER.md` (RR-001, RR-012), `docs/DEPLOYMENT.md` | `config/config_manager.py`'s `ConfigManager.load()` implemented and manually verified Phase 1 (see `CHANGELOG.md` §0.2.0); `tests/config/test_config_manager.py` formal suite pending a future phase | IMPLEMENTED |
| RQ-019 | CI shall block merge on any Ruff lint failure, Mypy strict-mode failure, or Pytest failure, and shall report coverage. | `.github/workflows/` | this document | CI pipeline itself is the verification | SPECIFIED |
| RQ-020 | Every ADR, API contract change, and risk register update shall be reflected in `CHANGELOG.md` with a correct SemVer bump. | project-wide | `CHANGELOG.md` policy | manual release-checklist review (`docs/RUNBOOK.md`) | IMPLEMENTED (process, Phase 0) |

## Coverage Summary (as of Phase 8)

| Category | Requirements Specified | Implemented | Verified |
|---|---|---|---|
| Architecture / Core | RQ-001–RQ-006 | 3 (RQ-001, RQ-002 partial, RQ-005 partial) | 0 |
| Strategy / Indicators | RQ-007–RQ-008 | 2 (RQ-007; RQ-008 partial — no `ParameterUpdate` yet) | 0 |
| Execution / Risk | RQ-009–RQ-011 | 1 (RQ-011; RQ-009/RQ-010 not started — see Non-Goals in `execution/README.md`) | 0 |
| Optimizer / Backtest | RQ-012–RQ-015 | 1 (RQ-012, partial — cadence/isolation only; RQ-013/RQ-014 deliberately not built, see `optimizer/README.md`) | 0 |
| Analytics / News | RQ-016–RQ-017 | 1 (RQ-017, partial — logic only, not wired to a risk gate) | 0 |
| Platform / Process | RQ-018–RQ-020 | 2 (RQ-018 code + manual verification; RQ-020 process) | 0 |

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
module's README. No row is yet `VERIFIED`, since that status requires a
formal automated test suite (Phase 9) and observed paper-trading behavior
(post Phase 10), neither of which exist yet.
