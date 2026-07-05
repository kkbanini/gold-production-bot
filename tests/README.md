# tests/

## Responsibility

The project's automated test suite: `tests/unit/` + `tests/integration/`
(the fast suites the standard CI pipeline runs) and `tests/chaos/` +
`tests/stress/` (isolated, opt-in suites — Phase 11e's reorganization,
`docs/PRODUCTION_SPEC.md` §7). Originally built flat in Phase 9
(`test_unit.py`/`test_integration.py` at the `tests/` root); see
"Phase 11e: reorganization into unit/integration/chaos/stress" below for
why and how that changed.

## Implementation

- `conftest.py` (stays at the `tests/` root — pytest cascades a root
  `conftest.py`'s fixtures into every subdirectory automatically, so it is
  not duplicated per-suite) — shared fixtures: `FakeMT5` (a drop-in
  substitute for the `MetaTrader5` module surface `broker/mt5_gateway.py`
  uses — no live terminal exists in this environment or in CI) plus
  `FakeSymbolInfo`, `FakeTick`, `FakePosition`, `FakeOrderResult`; and
  `state_manager`, a `tmp_path`-backed `StateManager` fixture for any test
  needing real SQLite persistence.
- `unit/test_unit.py` — pure-function/logic-level tests, no network, no
  live MT5, no cross-boundary I/O: every source package's pure-logic
  functions (`config/`, `storage/`, `broker/`, `indicators/`, `strategy/`,
  `execution/`, `risk/`, `optimizer/`, `news/`, `resilience/`, and
  `main.py`'s decision functions).
- `integration/test_integration.py` — exercises that cross a real
  boundary but do *not* simulate a fault: order/position-action request
  building against a simulated MT5, account/bar fetching, real SQLite
  transactional-rollback behavior, WAL mode/integrity, broker/ledger
  position-audit reconciliation, the weekend optimizer's isolation
  guarantee, and `container.py`'s full DI wiring.
- `chaos/test_chaos.py` — simulated fault/disruption scenarios: MT5
  server dropouts (`MT5Gateway.connect()`'s exponential backoff against a
  `FakeMT5` simulating transient failures then recovery, and total
  exhaustion), socket/HTTP disconnections
  (`news/news_engine.py`'s `fetch_calendar_events()` against a faked
  `requests.get`, plus `NetworkCalendarProvider`'s retry-with-backoff
  behavior), and an abrupt process crash (a `StateManager` dropped without
  `close()`, reopened fresh).
- `stress/` — currently empty; see `stress/README.md` for why (an honest
  Non-Goal, not a fabricated placeholder test).

## Phase 11e: reorganization into unit/integration/chaos/stress

`docs/PRODUCTION_SPEC.md` §7 asked to "Explicitly isolate fast-running
validation checks under `tests/unit/` and `tests/integration/` for
standard CI pipeline runs" and "Relocate and segregate all long-running
simulated anomalies into `tests/chaos/` and `tests/stress/`." None of this
project's simulated-fault tests are actually slow today (every
sleep/socket call is monkeypatched) — they were grouped into `chaos/` by
*what* they simulate (a fault or disruption), not by measured wall-clock
duration, since there was nothing slow to relocate. `pyproject.toml`'s
`testpaths` now points at `tests/unit`/`tests/integration` only, so a bare
`pytest` invocation (and `.github/workflows/ci.yml`'s standard job) never
touches `chaos/`/`stress/` — those run via an explicit
`pytest tests/chaos tests/stress` invocation.

`tests/__init__.py` and one `__init__.py` per new subdirectory
(`unit/`, `integration/`, `chaos/`, `stress/`) exist purely to resolve a
`mypy --strict` module-resolution ambiguity, since every test file imports
`tests.conftest` by dotted path — the same class of fix `storage/`,
`broker/`, etc. already used in earlier phases, just one level deeper now.

## Coverage

`pytest --cov=. --cov-report=term-missing --cov-fail-under=90` (the exact
`.github/workflows/ci.yml` invocation, now scoped to `tests/unit` +
`tests/integration` via `pyproject.toml`'s `testpaths`) passes well above
90% total coverage across every source package (`config/`, `storage/`,
`broker/`, `indicators/`, `strategy/`, `execution/`, `risk/`, `optimizer/`,
`news/`, `resilience/`). `[tool.coverage.run]`/`[tool.coverage.report]` in
`pyproject.toml` scope measurement to those source packages and exclude
`__init__.py` files.

## Depends On

Every module under test, plus `pytest`, `pytest-cov`, and `requests` (for
monkeypatching `requests.get` in chaos tests). No module depends on
`tests/`.

## Governing Docs

`docs/TRACEABILITY_MATRIX.md` (requirement → test mapping — rows for
modules covered here are updated to reference this suite instead of "ad
hoc, no automated tests yet"). `docs/PRODUCTION_SPEC.md` §7 (Phase 11e's
reorganization). `.github/workflows/ci.yml` (execution + coverage gate).

## Non-Goals (This Phase)

FSM simulation tests driving the full Order/Position/Parameter lifecycle
state machine (`docs/diagrams/fsm_diagram.puml`) are not built — no
`core`/`EventBus` orchestrating that lifecycle exists yet. `backtester/`
and `analytics/` have no tests since those modules don't exist (flagged
unscheduled since Phase 2). `tests/stress/` has no tests yet — see its own
README. This reorganization only *moves* existing tests and adds new
ones for Phase 11e's own code; it does not retroactively rewrite earlier
phases' test content or coverage.
