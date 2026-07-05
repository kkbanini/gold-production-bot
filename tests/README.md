# tests/

## Responsibility

The project's first formal automated test suite (Phase 9), replacing the
ad hoc verification scripts used in Phases 1–8 with committed, CI-runnable
tests. **Structure is flatter than the Phase 0 vision** — see
"Simplification vs. the Phase 0 layout" below.

## Implementation

- `conftest.py` — shared fixtures: `FakeMT5` (a drop-in substitute for the
  `MetaTrader5` module surface `broker/mt5_gateway.py` uses — no live
  terminal exists in this environment or in CI) plus `FakeSymbolInfo`,
  `FakeTick`, `FakePosition`, `FakeOrderResult`; and `state_manager`, a
  `tmp_path`-backed `StateManager` fixture for any test needing real
  SQLite persistence.
- `test_unit.py` — pure-function/logic-level tests, no network, no live
  MT5, no cross-boundary I/O: `config/`, `storage/db_engine.py`'s
  connection/PRAGMA behavior, `indicators/math_engine.py` (every function
  cross-checked against an independent pure-Python reference
  implementation), `strategy/trend_filter.py` and
  `strategy/execution_triggers.py` ("M5 candle flags" — breakout/pullback/
  wick-fill), `risk/risk_manager.py` ("lot math metrics"),
  `execution/position_manager.py`, `broker/mt5_gateway.py`'s pure-logic
  functions, `news/news_engine.py`'s pure-logic functions, and
  `optimizer/self_learning.py`'s pure-logic functions.
- `test_integration.py` — exercises that cross a real boundary, per the
  phase directive's four named scenarios:
  - **MT5 server dropouts** — `MT5Gateway.connect()`'s exponential
    backoff against a `FakeMT5` simulating transient failures then
    recovery, and total exhaustion; plus `submit_position_action()`'s
    full request-building (BUY/SELL-position-close order-type/price
    mapping, modify-SLTP shape) and both its rejection paths.
  - **Socket disconnections** — `news/news_engine.py`'s
    `fetch_calendar_events()` against a faked `requests.get`: connection
    error, timeout, non-200 status, invalid JSON, and the success path
    with correct timeout-tuple propagation.
  - **Database rollbacks** — a real SQLite constraint violation forced
    mid-`with connection:` block, proving the whole transaction (not just
    the failing statement) rolls back; plus `record_trade()`'s
    idempotency under a simulated retry.
  - **Data state validation processes** — FSM-state crash recovery (a
    `StateManager` instance dropped without `close()`, reopened fresh);
    WAL mode/integrity verification; `audit_open_positions()`'s
    broker/ledger reconciliation; and the big one —
    `optimizer/self_learning.py`'s isolation guarantee, verified against
    a real SQLite database: an FSM-state snapshot and an open position
    are confirmed byte-for-byte unchanged after a full weekly
    optimization cycle that does trigger a parameter shift, with exactly
    one `parameter_history` row appended, and zero side effects on a
    non-Saturday run.

## Simplification vs. the Phase 0 layout

The original Phase 0 vision described per-module test directories
(`tests/broker/`, `tests/storage/`, etc.) growing incrementally alongside
each module. Phase 9's actual directive named two flat files —
`tests/test_unit.py` and `tests/test_integration.py` — and that's what
was built, with `conftest.py` for shared fixtures. `tests/__init__.py` was
also added (not originally planned) purely to resolve a `mypy --strict`
module-resolution ambiguity once `test_unit.py`/`test_integration.py`
began importing `tests.conftest` by dotted path — the same class of fix
applied to `storage/`, `broker/`, etc. in earlier phases.

## Coverage

`pytest --cov=. --cov-report=term-missing --cov-fail-under=90` (the exact
`.github/workflows/ci.yml` invocation) passes at **97% total coverage**
across all nine source modules (`config/`, `storage/`, `broker/`,
`indicators/`, `strategy/`, `execution/`, `risk/`, `optimizer/`, `news/`),
every one individually above 90%. `[tool.coverage.run]`/`[tool.coverage.report]`
in `pyproject.toml` scope measurement to those source packages and exclude
`__init__.py` files.

## Depends On

Every module under test, plus `pytest`, `pytest-cov`, and `requests` (for
monkeypatching `requests.get` in integration tests). No module depends on
`tests/`.

## Governing Docs

`docs/TRACEABILITY_MATRIX.md` (requirement → test mapping — rows for
modules covered here are updated to reference this suite instead of "ad
hoc, no automated tests yet"). `.github/workflows/ci.yml` (execution +
coverage gate).

## Non-Goals (This Phase)

FSM simulation tests driving the full Order/Position/Parameter lifecycle
state machine (`docs/diagrams/fsm_diagram.puml`) are not built — no
`core`/`EventBus` orchestrating that lifecycle exists yet (deferred to
whichever phase builds `main.py`). `backtester/` and `analytics/` have no
tests since those modules don't exist (flagged unscheduled since Phase 2).
