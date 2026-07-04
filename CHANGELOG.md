# Changelog

All notable changes to the Gold Production Bot (XAUUSD Algorithmic Trading System) are
documented in this file. The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning 2.0.0](https://semver.org/).

Version tokens are stored canonically in `VERSION` and must match the latest header
below at all times. CI enforces this invariant (see `.github/workflows/ci.yml`'s
`docs-consistency` job).

## Versioning Policy

| Segment | Bumped When |
|---|---|
| MAJOR | Breaking change to a public interface contract in `docs/API_SPEC.md` (e.g. `Order`, `Signal`, `Tick` schema change, broker gateway contract change). |
| MINOR | Backward-compatible functionality added (new module, new indicator, new strategy filter, new optimizer capability). |
| PATCH | Backward-compatible bug fix, documentation correction, or internal refactor with no interface change. |

Pre-1.0.0 releases (`0.y.z`) are considered pre-production. Per SemVer §4, the public
API must not be considered stable until `1.0.0`. `MINOR` bumps during the `0.y.z` line
may contain breaking changes if, and only if, the ADR introducing the change is marked
`Status: Accepted` and the Traceability Matrix is updated in the same phase commit.

## [Unreleased]

Nothing yet. Phase 2 will introduce `broker/` (MT5 gateway and server-time
alignment).

## [0.2.0] - 2026-07-04

### Added — Phase 1: Environment Scaffolding, Secrets Architecture & Linter Rule Enforcement

- `requirements.txt` — exact, pinned production dependency versions
  (`MetaTrader5==5.0.4500`, `pandas==2.2.2`, `numpy==1.26.4`, `scipy==1.13.1`,
  `requests==2.32.3`, `python-dotenv==1.0.1`, `apscheduler==3.10.4`).
- `.env.template` — declares the full set of required runtime environment
  variables (`MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`,
  `ECONOMIC_CALENDAR_API_KEY`, `STRATEGY_MAGIC_NUMBER`, `ENVIRONMENT_MODE`)
  with empty placeholder values; never populated with real credentials.
- `pyproject.toml` — project metadata/dependencies (PEP 621) plus static
  quality tool configuration: Ruff (`target-version = "py312"`,
  `line-length = 100`, `select = ["E", "F", "B", "I", "C90"]`), Mypy
  (`disallow_untyped_defs`, `disallow_incomplete_defs`,
  `warn_unused_ignores`, all `true`), and Pytest (`testpaths = ["tests"]`).
- `config/config_manager.py` — `ConfigManager`, a frozen dataclass loaded via
  `ConfigManager.load()`. Reads `.env` through `python-dotenv`, validates that
  every required key is present, validates `ENVIRONMENT_MODE` is one of
  `DEMO`/`LIVE`, and type-coerces `MT5_LOGIN`/`STRATEGY_MAGIC_NUMBER` to
  `int`. Raises `ConfigurationError` (never returns a partial config) on any
  validation failure, enforcing RQ-018 and RR-012 at boot time.
- `config/README.md` updated to describe the landed implementation.
- `docs/RISK_REGISTER.md` (RR-012) and `docs/DEPLOYMENT.md`/`docs/RUNBOOK.md`
  updated to reference the concrete `ENVIRONMENT_MODE` (`DEMO`/`LIVE`)
  environment variable implemented this phase, in place of the earlier
  placeholder `TRADING_MODE` naming from the Phase 0 baseline.

### Verified

- `ruff check .` — all checks passed.
- `ruff format --check .` — all files already formatted.
- `mypy --strict config/config_manager.py` — no issues found.
- `pytest` — 0 tests collected against the (still-empty) `tests/` layout,
  confirming `testpaths` wiring is correct; no test files were part of this
  phase's deliverables.
- `ConfigManager.load()` manually exercised against three scenarios: all
  required keys missing (raises `ConfigurationError` naming every missing
  key), an invalid `ENVIRONMENT_MODE` value (raises with the invalid value
  and the valid set), and a fully valid environment (returns a correctly
  typed `ConfigManager` instance).

## [0.1.0] - 2026-07-04

### Added — Phase 0: Institutional Scaffolding, ADRs & Research Validation Specification

- Repository directory topology established per the enterprise module layout
  (`.github/workflows/`, `docs/`, `config/`, `storage/`, `broker/`, `indicators/`,
  `strategy/`, `execution/`, `optimizer/`, `backtester/`, `analytics/`, `news/`,
  `tests/`).
- `docs/adr/ADR-0001-event-driven-core-architecture.md` — event-driven, single-writer
  core loop as the concurrency model for the trading engine.
- `docs/adr/ADR-0002-mt5-broker-gateway-abstraction.md` — MetaTrader5 Python API
  wrapped behind an internal `BrokerGateway` port/adapter boundary.
- `docs/adr/ADR-0003-sqlite-wal-transactional-ledger.md` — SQLite (WAL mode) as the
  embedded, ACID-compliant state and transaction ledger.
- `docs/adr/ADR-0004-anchored-walk-forward-validation.md` — anchored walk-forward
  optimization (WFO) as the sole model-validation methodology; forbids
  non-anchored/random k-fold validation on time-series price data.
- `docs/API_SPEC.md` — canonical interface contracts (`Tick`, `Bar`, `Signal`,
  `Order`, `Position`, `Fill`, `BrokerGateway`, `EventBus`) with full Python type
  hints (typing-only; no executable trading logic).
- `docs/TRACEABILITY_MATRIX.md` — requirement → module → test → ADR traceability
  seed matrix for Phase 0 artifacts.
- `docs/RISK_REGISTER.md` — enumerated operational, market, and technical risks with
  severity taxonomy `INFO → LOW → MEDIUM → HIGH → CRITICAL → FATAL`.
- `docs/RUNBOOK.md` — operational runbook skeleton: startup/shutdown sequencing,
  incident response procedures per severity level, and escalation paths.
- `docs/DEPLOYMENT.md` — target deployment topology (Windows VPS + MT5 terminal),
  environment promotion strategy (`dev → paper → live`), and rollback procedure.
- `docs/RESEARCH.md` — quantitative research specification: objective functions,
  data provenance/specs for XAUUSD, and anchored WFO window parameterization.
- `docs/diagrams/component_diagram.puml` — PlantUML C4-style component diagram of
  the full system.
- `docs/diagrams/sequence_diagram.puml` — PlantUML sequence diagram of the
  tick-to-fill event pipeline.
- `docs/diagrams/fsm_diagram.puml` — PlantUML finite-state machine of the
  Order/Position lifecycle.
- `.github/workflows/ci.yml` — CI pipeline stub wiring Ruff → Mypy → Pytest →
  Coverage gates (no trading code exists yet to execute against).
- Per-module `README.md` contract stubs in `config/`, `storage/`, `broker/`,
  `indicators/`, `strategy/`, `execution/`, `optimizer/`, `backtester/`,
  `analytics/`, `news/`, `tests/` documenting each module's single responsibility,
  inbound/outbound dependencies, and explicit non-goals for this phase.
- `VERSION` initialized to `0.1.0`.

### Notes

- No trading, execution, indicator, or order-routing logic was written in this
  phase, by directive. All Python-adjacent content in this phase is limited to
  type-hinted interface *contracts* (`docs/API_SPEC.md`) with no executable
  function bodies.
- This phase requires explicit human approval before Phase 1 (`config/` — Secret &
  Environment Managers) begins.
