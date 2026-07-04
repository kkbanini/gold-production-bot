# Risk Register

Enumerates market, operational, and technical risks for the Gold (XAUUSD)
production trading system. Every risk has a stable ID (`RR-nnn`), a severity
classification, a detection mechanism, and a mitigation owned by a specific module.
This register is a living document; every phase that touches a risk's owning module
must review and, if necessary, update that risk's rows.

## Severity Taxonomy

| Level | Definition | Response SLA | Example Trigger |
|---|---|---|---|
| `INFO` | Expected, logged operational event; no action required. | None — logged only | Normal order fill, scheduled optimizer run completes |
| `LOW` | Deviation from expected behavior with no capital or data-integrity impact. | Review within 24h | Minor slippage within tolerance, a single tick gap under 2s |
| `MEDIUM` | Deviation that could develop into capital impact if left unaddressed. | Review within 4h, same trading day | Slippage guard triggered repeatedly, news window near-miss |
| `HIGH` | Active or imminent capital impact, or a data-integrity concern requiring manual review before next trading action. | Immediate review, within 1h | Broker reconnect after disconnect, unexpected position size mismatch vs. ledger |
| `CRITICAL` | Capital-at-risk event requiring the system to autonomously de-risk (flatten/halt) pending human review. | Immediate autonomous action + page operator | Margin level below threshold, optimizer WFO gate failure attempted to bypass, duplicate order detected |
| `FATAL` | System integrity cannot be guaranteed; trading must halt entirely and requires manual restart after root-cause review. | Immediate halt, no autonomous restart | Event log / derived-state divergence detected, broker account mismatch (wrong account/demo-live confusion), corrupted database file |

Severity determines the automated response defined in `docs/RUNBOOK.md`'s incident
response procedures; this register defines *what* can go wrong and *how it's
mitigated*, `RUNBOOK.md` defines *what humans/the system do when it does*.

## Risk Rows

| ID | Risk | Category | Severity | Detection | Mitigation | Owning Module |
|---|---|---|---|---|---|---|
| RR-001 | Broker credentials or API keys committed to source control or logged in plaintext. | Security | CRITICAL | Pre-commit secret scan (Phase 1 CI addition), code review | Secrets sourced only from `.env`/environment via `config/`; `.env` is gitignored; RQ-018 test enforces no secret literals in repo. | `config/` |
| RR-002 | MT5 terminal disconnects during an open-position holding period. | Operational | HIGH (escalates to CRITICAL if reconnection fails past backoff budget) | `BrokerConnectionError` raised by `broker/` after exhausted retry/backoff. | Exponential backoff reconnect in `broker/` (ADR-0002); on exhaustion, publish `RiskBreach(severity=CRITICAL)` and de-risk per `RUNBOOK.md`. | `broker/` |
| RR-003 | Broker server time / host clock skew causes session or news-window boundaries to be evaluated against the wrong wall-clock time. | Market / Technical | HIGH | Periodic re-validation of `broker_utc_offset`; alert if unexpected offset delta detected. | Centralized UTC normalization at the `BrokerGateway` boundary (ADR-0002); no other module performs broker-timezone arithmetic. | `broker/` |
| RR-004 | Look-ahead bias or non-anchored validation leaks future information into a promoted strategy parameter set. | Model / Research | CRITICAL | Automated fold-construction assertion (`test_start > train_end` invariant); DSR/IS-OOS gate check. | Anchored WFO is the sole sanctioned methodology (ADR-0004); optimizer refuses to emit `ParameterUpdateEvent` on any gate failure. | `optimizer/`, `backtester/` |
| RR-005 | Backtest/live behavioral divergence ("parity gap") causes live performance to differ materially from validated expectations. | Model / Technical | HIGH | Scheduled parity check: replay a recent live event window through `backtester/`'s event-driven mode and diff decisions. | Shared `strategy/`/`execution/` code path against a common `BrokerGateway` `Protocol` (ADR-0002); only the adapter differs. | `backtester/`, `broker/` |
| RR-006 | Order fill price slips beyond acceptable tolerance due to spread widening or low liquidity (common around XAUUSD news events). | Market | MEDIUM (HIGH if repeated within a session) | Slippage guard compares `requested_price` vs. `Fill.fill_price`. | Execution-side slippage guard rejects/derisks fills outside configured tolerance (RQ-010); pre-trade `NewsWindow` blackout reduces exposure window. | `execution/` |
| RR-007 | Duplicate order submission (e.g. retry-after-timeout submits twice) causes unintended double exposure. | Technical | CRITICAL | `client_order_id` idempotency key check against `OrderRepository` before submit. | Every `Order` carries a locally-generated idempotent `client_order_id`; `execution/` checks `OrderRepository.get_by_client_order_id` before resubmitting on retry. | `execution/`, `storage/` |
| RR-008 | Event log and derived state (positions/orders tables) diverge due to a partial write or a bug in the materialization step. | Technical / Data Integrity | FATAL | Periodic consistency check: replay event log from genesis/snapshot and diff against live derived tables. | Single transaction wraps event-append + derived-table update (ADR-0003); consistency-check job halts trading on divergence. | `storage/` |
| RR-009 | Trading through a high-impact economic news release (e.g. US CPI, FOMC) causes abnormal slippage or gap risk. | Market | MEDIUM (HIGH during top-tier releases: FOMC, NFP, CPI) | `NewsWindow` event active for the symbol at signal-evaluation time. | Pre-trade risk gate blocks/derisks within `blackout_before`/`blackout_after` windows sourced from `news/`. | `news/`, `execution/` |
| RR-010 | Optimizer overfits via multiple-comparisons bias across a large parameter sweep, producing an inflated in-sample Sharpe. | Model / Research | HIGH | Deflated Sharpe Ratio computation accounts for number of trials per ADR-0004. | DSR gate is a mandatory, non-bypassable promotion check; raw in-sample Sharpe is never used as a promotion criterion. | `optimizer/` |
| RR-011 | Margin level falls toward stop-out threshold due to adverse excursion on open position(s). | Market / Capital | CRITICAL | Continuous `AccountState.margin_level_pct` monitoring against configured floor. | Pre-trade risk gate sizing (`RiskGateDecision.max_position_size_lots`) and autonomous de-risk action when floor breached. | `execution/`, `broker/` |
| RR-012 | Bot connects to the wrong MT5 account (e.g. live account when demo/paper was intended, or vice versa). | Operational | FATAL | Startup-time assertion comparing configured expected account ID/type against `AccountState`/terminal login info. | `config/` requires an explicit, human-confirmed `TRADING_MODE` (`paper`/`live`) and account-ID allowlist; mismatch halts startup, does not warn-and-continue. | `config/`, `broker/` |
| RR-013 | SQLite database file corruption (disk failure, improper shutdown on unsupported filesystem). | Technical / Data Integrity | FATAL | Startup integrity check (`PRAGMA integrity_check`); scheduled backup verification. | WAL mode + documented supported-filesystem constraint (ADR-0003); scheduled `VACUUM INTO` backups per `docs/RUNBOOK.md`. | `storage/` |
| RR-014 | Weekend optimizer parameter update is applied mid-week or mid-signal-evaluation, corrupting an in-flight decision. | Technical | CRITICAL | Core loop asserts `ParameterUpdateEvent` application only occurs at a bar-close boundary with no open signal evaluation in flight. | `ParameterUpdateEvent` applied atomically between bar closes only (ADR-0001 §5). | core, `optimizer/` |
| RR-015 | Indicator or strategy code contains a numerical bug (e.g. off-by-one on lookback window, NaN propagation) that silently degrades signal quality. | Model / Technical | MEDIUM (HIGH if it causes anomalous order flow) | Property-based/unit tests on `indicators/` for known reference values; NaN/Inf guards on all published `Signal`s. | Indicators specified as pure numpy functions with mandatory reference-value tests (RQ-007). | `indicators/`, `strategy/` |
| RR-016 | Dependency supply-chain compromise (malicious package in `requirements`/`pyproject` dependency tree). | Security | HIGH | Dependency pinning + hash verification; `pip-audit`/equivalent in CI (Phase 1+ CI hardening). | Pinned, hash-locked dependencies; CI dependency vulnerability scan gate. | `.github/workflows/`, `config/` |
| RR-017 | Windows VPS host-level failure (power/network outage) during an open position. | Operational | HIGH | Heartbeat/watchdog external monitoring (`docs/RUNBOOK.md` §Monitoring). | Broker-side stop-loss is always attached to every open position (never mental-stop / software-only), so a host outage cannot leave a position unprotected. | `execution/`, `docs/DEPLOYMENT.md` |

## Review Cadence

- This register is reviewed at the close of every phase for rows touching modules
  delivered in that phase, and quarterly thereafter for rows belonging to modules
  already in production.
- Any `CRITICAL`/`FATAL` row's mitigation must have a corresponding test in
  `docs/TRACEABILITY_MATRIX.md` before the owning module can be marked
  `VERIFIED`.
