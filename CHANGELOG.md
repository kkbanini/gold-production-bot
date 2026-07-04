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

Nothing yet. Phase 5 will introduce entry-trigger logic in `strategy/`
(breakout patterns, pullback logic, wick-fill analytics, tick-volume filter).

## [0.5.0] - 2026-07-04

### Added — Phase 4: Pure Math Indicator Engine & Trend Processing

- `indicators/math_engine.py` — `ema()` (standard EMA), `atr()` (Wilder's
  smoothing, default period 14), `adx()` (full Wilder ADX: +DM/-DM ->
  Wilder-smoothed +DI/-DI -> DX -> Wilder-smoothed ADX, default period 14).
  All three are pure numpy functions with no I/O/broker dependency (RQ-007)
  and raise `ValueError` on insufficient history or mismatched array
  lengths. Exports `FloatArray` (`npt.NDArray[np.float64]`) as a shared type
  alias.
- `strategy/trend_filter.py` — `evaluate_master_trend()`: validates trend
  alignment across D1 EMA(200), H4 EMA(50), and H1 EMA(40) (each timeframe
  compared against its own EMA), gated by H1 ADX(14) > 25
  (`ADX_TREND_THRESHOLD`). Returns a `TrendAlignment` dataclass with a
  `direction` (`BULLISH`/`BEARISH`/`NONE`) and an `is_valid` property
  requiring both full alignment and ADX confirmation.
- `pyproject.toml` — added `numpy.typing`/`TypeAlias` usage in
  `indicators/math_engine.py`; no new config needed beyond what Phase 1
  already specified.
- `indicators/README.md`, `strategy/README.md` updated to describe the
  landed implementation.

### Fixed — real bug caught during verification

- `adx()`'s first implementation Wilder-smoothed the DX line using the same
  "smoothed sum" convention as True Range/+DM/-DM, but omitted the final
  `/ period` normalization that convention requires (exactly the
  normalization `atr()` already applies to `smoothed_tr`). This let ADX
  exceed its mathematically required `[0, 100]` bound — a synthetic strong
  uptrend produced `ADX = 1400.0`. Caught by cross-checking against two
  independently-derived pure-Python Wilder-ADX reference implementations
  (sum-then-divide vs. direct step-by-step averaging) plus an explicit
  bound assertion; the first reference initially shared the same missing
  division (derived from the same flawed mental model) and did not catch
  it alone — the second, structurally different derivation did. Fixed by
  adding the missing `/ period` division. See `indicators/README.md`
  "Verification note" for the full account.

### Noted — local toolchain artifact, not a codebase issue

- This development sandbox has only Python 3.14 installed (no 3.12, no C
  compiler to build numpy from source), so `numpy==1.26.4` (the pinned
  production version, which has no Python 3.14 wheel) could not be
  installed locally; `MetaTrader5`'s own dependency resolution pulled
  `numpy==2.5.0` instead for local verification. `mypy==1.11.2` (the pinned
  dev version) does not understand numpy 2.5's typing stubs and produced
  spurious `FloatArray? is not indexable`-style errors against otherwise
  correct, properly-annotated code; upgrading `mypy` to `2.1.0` locally
  resolved this cleanly. `pyproject.toml`'s pinned `mypy==1.11.2` dev
  dependency was **not** changed, since the project's actual target
  toolchain (Python 3.12, where `numpy==1.26.4` installs normally per its
  published wheels) is not expected to hit this incompatibility — CI runs
  on Python 3.12 (`.github/workflows/ci.yml`). This is an artifact of this
  sandbox's Python version, not a defect in the pinned dependency set.

### Verified

- `ruff check .` and `ruff format --check .` — all checks passed (16 files).
- `mypy --strict .` — no issues found in 16 source files (using mypy 2.1.0
  locally per the note above).
- `pytest` — 0 tests collected against the still-empty `tests/` layout, as
  expected (automated `tests/indicators/` and `tests/strategy/` coverage
  deferred to the project's dedicated testing phase).
- `ema()`: constant-series sanity check, plus exact match (`rtol=1e-10`)
  against an independent pure-Python reference over 300 random bars.
- `atr()`: exact match (`rtol=1e-9`) against an independent pure-Python
  reference over 300 random synthetic OHLC bars.
- `adx()`: exact match (`rtol=1e-8`) against two independently-derived
  pure-Python references, an explicit `0 <= ADX <= 100` bound assertion,
  qualitative sanity (`ADX > 25` on a strong synthetic uptrend, `ADX < 25`
  on a pure-noise/choppy series), and `ValueError` on insufficient
  history/mismatched array lengths for all three functions.
- `evaluate_master_trend()`: full bullish alignment (all three timeframes
  + ADX confirm) -> `BULLISH`/`is_valid=True`; full bearish alignment ->
  `BEARISH`/`is_valid=True`; mismatched timeframe alignment (D1 bullish, H1
  bearish) -> `NONE`; choppy market on all timeframes -> `adx_confirmed=False`
  even where EMA sides happened to align; insufficient D1 history ->
  `ValueError` propagated from `indicators.math_engine`.

## [0.4.0] - 2026-07-04

### Added — Phase 3: Broker Connection & Position Recovery Gateway

- `broker/mt5_gateway.py`:
  - `resolve_gold_symbol()` — dynamic Gold symbol matching across broker
    naming variants (`XAUUSD`, `XAUUSD.m`, `XAUUSD.a`, `XAUUSDm`, `XAUUSD_i`,
    `GOLD`, `GOLD.m`, `GOLDm`), with `symbol_select()` fallback for symbols
    present but not visible in Market Watch. Returns a `SymbolSpec` with
    point size, digits, tick value/size, and volume constraints read
    directly from the broker.
  - `is_within_execution_window()` — the 07:00–22:00 GMT execution filter,
    requiring a timezone-aware input.
  - `MT5Gateway.connect()` — exponential backoff reconnect (delay doubles
    each attempt, capped, raises `BrokerConnectionError` with the last
    `mt5.last_error()` on exhaustion per RR-002); resolves `symbol_spec` and
    `broker_utc_offset` (from the resolved symbol's latest tick, per
    ADR-0002) before returning successfully.
  - `MT5Gateway.get_open_positions_by_magic()` /
    `audit_open_positions()` — position-recovery path. Filters
    `mt5.positions_get()` by magic number and reconciles against the local
    `trade_ledger`, returning a `PositionAuditReport` (`reconciled_tickets`,
    `broker_only_positions`, `ledger_only_entries`) per RR-008.
- `storage/db_engine.py` / `storage/state_manager.py` — added a nullable
  `broker_ticket` column to `trade_ledger` (plus an index) and a matching
  `TradeLedgerEntry.broker_ticket` field, needed as the reconciliation key
  for `audit_open_positions()`. Backward-compatible additive schema change;
  no existing production database exists yet to migrate.
- `pyproject.toml` — added a `[[tool.mypy.overrides]]` entry for
  `MetaTrader5` (`ignore_missing_imports = true`), since the package ships
  no inline types or stub package; every call site is converted into a
  typed dataclass (`SymbolSpec`, `BrokerPosition`) immediately, so this
  doesn't weaken typing anywhere else.
- `broker/README.md`, `storage/README.md` updated to describe the landed
  implementation.

### Fixed / Noted

- `requirements.txt` pins `MetaTrader5==5.0.4500`, which **does not exist on
  PyPI** (only `5.0.5488` and later are published there). Left the pin
  as-specified rather than silently changing it, since the exact build may
  matter for matching a specific MT5 terminal version on a target VPS; for
  local lint/type-check verification only, `5.0.5488` (the oldest available)
  was installed into the dev venv. This needs reconciling before
  `requirements.txt` can be installed on a fresh machine.
- `docs/TRACEABILITY_MATRIX.md`'s placeholder phase numbers for RQ-001/RQ-002
  (guessed during Phase 0 as "pending Phase 2") corrected to Phase 3 to match
  the approved roadmap (see Phase 2's changelog entry for the first pass at
  this reconciliation).

### Verified

- `ruff check .` and `ruff format --check .` — all checks passed (14 files).
- `mypy --strict .` — no issues found in 14 source files.
- `pytest` — 0 tests collected against the still-empty `tests/` layout, as
  expected (automated `tests/broker/` coverage deferred to the project's
  dedicated testing phase).
- No live MT5 terminal or broker credentials exist in this environment, so
  `MT5Gateway.connect()` cannot be exercised against a real server this
  phase. Instead, all connection-independent logic was verified ad hoc
  against a fake `MetaTrader5` module substituted in place of the real one:
  dynamic symbol resolution (priority order + visibility fallback, and the
  no-candidate-found error path), the GMT execution window's boundary hours
  (06:59/07:00/21:59/22:00 UTC) plus its naive-datetime rejection, the
  exponential backoff delay sequence (`[1.0, 2.0]` before a 3rd-attempt
  success) and its exhaustion path (raises with the last broker error
  embedded), and magic-number position audit/reconciliation (a reconciled
  ticket, a broker-only orphan position, and a ledger-only entry with no
  matching broker position, all correctly classified).

## [0.3.0] - 2026-07-04

### Added — Phase 2: State Persistence & SQLite Database Engine

- `storage/db_engine.py` — `connect()` opens a SQLite connection with
  `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=FULL`, and
  `PRAGMA foreign_keys=ON`; `initialize_schema()` creates the `trade_ledger`
  and `system_state` tables (idempotent, `CREATE TABLE IF NOT EXISTS`).
  Also provides `checkpoint_wal()` and `integrity_check()`.
- `storage/state_manager.py` — `StateManager` provides atomic FSM-state
  persistence (`save_fsm_state()`/`load_fsm_state()`, singleton UPSERT
  against `system_state`) and idempotent trade-ledger bookkeeping
  (`record_trade()`/`get_open_trades()`, UPSERT keyed on `client_order_id`
  per RR-007).
- `config/__init__.py`, `storage/__init__.py`, `broker/__init__.py`,
  `indicators/__init__.py`, `strategy/__init__.py`, `execution/__init__.py`,
  `optimizer/__init__.py`, `backtester/__init__.py`, `analytics/__init__.py`,
  `news/__init__.py` — package markers added project-wide after discovering
  `mypy --strict .` (the exact invocation `.github/workflows/ci.yml` runs)
  failed with "Source file found twice under different module names" once a
  second module (`state_manager.py`) imported a sibling module
  (`storage.db_engine`) by dotted path. This is a real fix to a real CI
  failure, not preventative scaffolding.
- `storage/README.md` updated to describe the landed implementation and to
  explicitly document where it simplifies relative to the original
  multi-repository `docs/API_SPEC.md` §4 design (see the module's own
  "Simplification vs. the Phase 0 API contract" section).

### Verified

- `ruff check .` and `ruff format --check .` — all checks passed (13 files).
- `mypy --strict .` — no issues found in 13 source files (this is the first
  phase where this exact CI invocation was exercised against real code, and
  it caught the module-naming collision above).
- `pytest` — 0 tests collected against the still-empty `tests/` layout;
  automated `tests/storage/` coverage is deferred to the project's dedicated
  testing phase, consistent with Phase 1's precedent.
- Ad hoc functional verification (scratch script, not committed): WAL mode
  confirmed active via `PRAGMA journal_mode`; `integrity_check()` passes on a
  freshly initialized database; a `StateManager` instance's saved FSM state
  is fully recoverable by a second, independently constructed `StateManager`
  against the same file after the first instance is dropped without a
  graceful `close()` (simulated crash); `system_state` always holds exactly
  one row after repeated saves; `record_trade()` called twice with an
  identical entry (simulated retry) leaves exactly one `trade_ledger` row;
  closing a trade removes it from `get_open_trades()`.

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
