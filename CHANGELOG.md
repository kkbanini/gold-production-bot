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

Nothing yet. Phase 9 will introduce `tests/test_unit.py` and
`tests/test_integration.py` (the project's first formal automated test
suite, targeting >90% coverage).

## [0.9.0] - 2026-07-05

### Added — Phase 8: Isolated Weekend Learning Optimization & Monte Carlo Validator

- `storage/db_engine.py` / `storage/state_manager.py` — new append-only
  `parameter_history` table plus `StateManager.get_closed_trades()` and
  `StateManager.record_parameter_change()`. This table is the *only*
  write target `optimizer/self_learning.py` is permitted to touch.
- `optimizer/self_learning.py`:
  - `is_market_closed_for_optimization()` / `create_weekend_optimizer_scheduler()`
    — a `BackgroundScheduler` job locked to Saturdays via
    `CronTrigger(day_of_week="sat", hour=3, timezone="UTC")`, plus a second,
    independent runtime check re-evaluated inside
    `run_weekly_optimization_cycle()` itself, so a direct/manual call
    outside the scheduler still cannot run on a non-Saturday.
  - `compute_ledger_metrics()` — trade count, win rate, profit factor,
    total profit from closed-trade history.
  - `decide_parameter_shift()` — rule-based, at most one parameter change
    per call: win rate below 40% tightens `ADX_TREND_THRESHOLD`;
    otherwise profit factor below 1.0 widens `TRAILING_ATR_MULTIPLIER`;
    otherwise no change. Below 10 trades, no change regardless.
  - `run_monte_carlo_bootstrap()` — 1000-iteration (default) resampling
    of the closed-trade P&L sequence with replacement, reporting the
    5th/95th percentile of resampled final P&L and the fraction of
    resamples that were profitable.
  - `run_weekly_optimization_cycle()` — the single entry point tying the
    above together: reads closed trades, computes metrics, decides and
    persists at most one shift, runs the bootstrap.
- Added a `[[tool.mypy.overrides]]` entry for `apscheduler.*`
  (`ignore_missing_imports = true`) — like `MetaTrader5`, it ships no
  type stubs and none exist on PyPI (`types-apscheduler` does not exist).
- `optimizer/README.md`, `storage/README.md` updated to describe the
  landed implementation.

### Flagged — this is deliberately simpler than ADR-0004's original design

`docs/RESEARCH.md`/ADR-0004 describe anchored walk-forward optimization
against historical OHLC data via a `backtester/` module, a
Deflated-Sharpe-weighted objective function, and multi-gate promotion
criteria. None of that exists — `backtester/` was flagged **unscheduled**
in the roadmap back in Phase 2. This phase's directive describes something
categorically simpler (a single rule-based shift against live/paper ledger
metrics, plus a standalone bootstrap validator), and that's what's
implemented. See `optimizer/README.md`'s "Simplification" section. Also
flagged: the specific rule thresholds (10 trades, 40% win rate, 1.0 profit
factor) and which parameter each rule adjusts were not specified in the
phase directive — this implementation's choice, fully overridable.

### Verified

- `ruff check .` and `ruff format --check .` — all checks passed (22 files).
- `mypy --strict .` — no issues found in 22 source files (mypy 2.1.0
  locally, per the Phase 4 toolchain note).
- `pytest` — 0 tests collected against the still-empty `tests/` layout, as
  expected (this project's first formal automated suite lands next phase).
- Pure-logic checks: Saturday-only gating (with naive-datetime rejection),
  `compute_ledger_metrics()` across empty/mixed/all-win/all-loss trade
  sets, `decide_parameter_shift()` across all five branches (insufficient
  trades, low win rate, low profit factor, healthy metrics, parameter
  already at its bound, missing parameter), and
  `run_monte_carlo_bootstrap()`'s reproducibility under a seeded RNG plus
  its all-positive/all-negative sanity bounds.
- **Isolation guarantee, verified against a real SQLite database** (not
  just asserted): seeded an FSM-state snapshot and an open position,
  seeded closed trades engineered to trigger a parameter shift, ran a
  full weekly optimization cycle on a Saturday timestamp, and confirmed
  byte-for-byte that the FSM snapshot and the open trade were completely
  unchanged afterward, with exactly one new `parameter_history` row
  written. A parallel run on a Tuesday timestamp confirmed the cycle is
  skipped with zero side effects (no `parameter_history` row at all).
- `create_weekend_optimizer_scheduler()`: confirmed the registered job's
  `CronTrigger` carries `day_of_week='sat', hour='3'`.

## [0.8.0] - 2026-07-05

### Added — Phase 7: News API Calendar Engine & Defensive Circuit Breakers

- `news/news_engine.py`:
  - `fetch_calendar_events()` — GETs an economic calendar feed with
    independent connect/read timeouts (defaults 5s/10s), parsing the
    response into `EconomicEvent` objects. Raises
    `NewsFeedConnectionError` on any connection failure, timeout, non-2xx
    response, or invalid JSON.
  - `EconomicEvent.is_core_macro_event` — keyword classification for
    NFP/CPI/FOMC releases.
  - `is_trade_entry_locked()` — ±30-minute (inclusive) trade-entry
    blackout window around any core macro event.
  - `apply_news_feed_fail_safe()` — the News-API-down defensive circuit
    breaker: halves risk size and doubles the spread tolerance limit when
    the feed is unreachable, otherwise passes both through unchanged.
  - Added `types-requests==2.33.0.20260518` to `pyproject.toml`'s dev
    dependencies (proper stub package for `mypy --strict`, unlike
    `MetaTrader5`'s `ignore_missing_imports` override — `requests` has an
    actively maintained stub package, so no override was needed here).
- `news/README.md` updated to describe the landed implementation.

### Flagged — two design choices without a prior spec to follow

- **No calendar provider was ever named** anywhere in this project
  (`docs/DEPLOYMENT.md` and `config/.env.template` only declare a generic
  `ECONOMIC_CALENDAR_API_KEY`). `fetch_calendar_events()` assumes a
  generic REST/JSON shape (`GET {base_url}?from=...&to=...` returning
  `{title, country, impact, date}` objects); `_parse_event()` is the only
  function that would need to change for a concrete vendor.
- **"Double spread limits" was implemented literally.** Taken literally,
  doubling a "maximum spread tolerated" threshold makes the filter *more*
  permissive, which reads as counterintuitive for a "defensive circuit
  breaker" (the alternative — halving it, becoming stricter — arguably
  fits "defensive" framing better but contradicts the literal word
  "double"). Implemented as literally specified and documented explicitly
  in `news/README.md`'s "Flagged" section, since silently inverting an
  explicit instruction based on my own risk-management judgment would be
  a bigger overreach than following it and flagging the ambiguity.

### Verified

- `ruff check .` and `ruff format --check .` — all checks passed (21 files).
- `mypy --strict .` — no issues found in 21 source files (mypy 2.1.0
  locally, per the Phase 4 toolchain note; `requests==2.32.3` itself
  installs cleanly even in this sandbox's Python 3.14, unlike
  `MetaTrader5`/`numpy`).
- `pytest` — 0 tests collected against the still-empty `tests/` layout, as
  expected (automated `tests/news/` coverage deferred to the project's
  dedicated testing phase).
- `fetch_calendar_events()` (against a faked `requests.get`, no live
  calendar provider credentials in this environment): successful parse
  with the correct `(connect, read)` timeout tuple passed through;
  `NewsFeedConnectionError` on connection error, timeout, HTTP 503, and
  invalid JSON; `ValueError` on naive `from_utc`/`to_utc`.
- `EconomicEvent.is_core_macro_event`: correctly classifies NFP/CPI/FOMC
  title variants vs. unrelated events.
- `is_trade_entry_locked()`: locked exactly at the event, exactly 30
  minutes before, and exactly 30 minutes after (inclusive boundary on both
  sides); not locked at 31 minutes before/after; a non-macro event at the
  same timestamp never locks; `ValueError` on naive `now_utc`.
- `apply_news_feed_fail_safe()`: unchanged output for a healthy feed;
  correct 0.5× risk / 2.0× spread-limit adjustment for an unhealthy one.

## [0.7.0] - 2026-07-05

### Added — Phase 6: Position Compounding & Advanced Trailing Mechanics

- New `risk/` module (not part of the original Phase 0 scaffold — created
  this phase per its directive). `risk/risk_manager.py`:
  - `clamp_lot_size(raw_lots, volume_min, volume_max, volume_step)` —
    rounds down to the nearest broker volume step and clamps to
    `[volume_min, volume_max]`.
  - `calculate_compounded_lot_size(equity, volume_min, volume_max, volume_step, ...)`
    — equity-based tiered lot sizing: one `lot_increment` (default 0.01)
    added per `equity_per_lot_increment` (default 1000.0) of equity, on
    top of `base_lot_size` (default 0.01).
- `execution/position_manager.py`:
  - `calculate_base_take_profit()` — `Base_TP = entry ± ATR × 2`.
  - `evaluate_partial_close_and_breakeven()` — once price reaches
    `Base_TP`, returns a two-step action list: close 50% of volume, then
    move the remaining volume's stop-loss to breakeven (exact entry
    price).
  - `calculate_trailing_stop()` — dynamic ATR(14) × 1.5 trailing stop,
    active only once breakeven is set; never returns a candidate that
    would loosen the existing stop.
  - All three produce `OrderActionPayload` — a dataclass mirroring a
    MetaTrader5 `order_send()` request shape without importing
    `MetaTrader5`.
- `broker/mt5_gateway.py` — added `BrokerOrderRejectedError` and
  `submit_position_action()`, which translates an `OrderActionPayload`
  into a real `mt5.order_send()` request: a `TRADE_ACTION_DEAL` partial
  close (looks up the live position to determine side, computes the
  opposite closing order type, reads bid/ask for the closing price) or a
  `TRADE_ACTION_SLTP` modify request. Raises `BrokerOrderRejectedError` on
  a non-DONE retcode or an unresolvable position/tick.
- `risk/README.md` (new), `execution/README.md`, `broker/README.md`
  updated to describe the landed implementation.

### Design note — preserved the ADR-0002 import boundary

The phase directive describes building the partial-close/breakeven logic
in `execution/position_manager.py` "using the MT5 Python library payload
configurations," which read literally could mean constructing
`mt5.order_send()` requests directly in that file. Doing so would violate
ADR-0002/RQ-001 (only `broker/` — and `backtester/`'s future test double —
may import `MetaTrader5`), a boundary verified since Phase 3. Instead,
`position_manager.py` produces `OrderActionPayload`, which mirrors the
shape of an MT5 request dict (`action`/`position`/`symbol`/`volume`/`sl`/`tp`/
`magic`/`comment`) as plain data, and `broker/mt5_gateway.py`'s new
`submit_position_action()` is the only place that actually imports and
calls into `MetaTrader5` to submit it. This satisfies the phase's intent
(payload-shaped position management) without reopening a settled
architectural boundary.

### Flagged — compounding tier parameters were not specified

Unlike earlier phases (which gave precise numbers — the 50-point breakout
filter, `SMA(20) × 1.5`), this phase's directive asked for equity-based
lot compounding without specifying exact tier numbers. The defaults above
are this implementation's choice among reasonable conventions, flagged in
`risk/README.md` for review, and fully overridable via keyword arguments.

### Verified

- `ruff check .` and `ruff format --check .` — all checks passed (20 files).
- `mypy --strict .` — no issues found in 20 source files (mypy 2.1.0
  locally, per the Phase 4 toolchain note).
- `pytest` — 0 tests collected against the still-empty `tests/` layout, as
  expected (automated `tests/risk/` and `tests/execution/` coverage
  deferred to the project's dedicated testing phase).
- `clamp_lot_size()`/`calculate_compounded_lot_size()`: rounding, min/max
  clamping, non-positive-input handling, equity-tier scaling, and
  `ValueError` on invalid constraints/non-positive equity.
- `calculate_base_take_profit()`: correct BUY/SELL distance, `ValueError`
  on non-positive ATR.
- `evaluate_partial_close_and_breakeven()`: no action before `Base_TP`;
  correct 50%-volume partial-close + exact-entry-price breakeven once
  reached; no further action once already partial-closed.
- `calculate_trailing_stop()`: inactive before breakeven is set; tightens
  correctly for both BUY and SELL as price moves favorably; correctly
  rejects a candidate that would loosen the existing stop.
- `broker/mt5_gateway.submit_position_action()` (against a fake
  `MetaTrader5` substitute, no live terminal in this environment):
  correct BUY-position-close (SELL order @ bid) and SELL-position-close
  (BUY order @ ask) request shape, correct modify-SLTP request shape,
  `BrokerOrderRejectedError` on a non-DONE retcode, and on a
  missing/already-closed position.

## [0.6.0] - 2026-07-04

### Added — Phase 5: Technical Entry Rules & Wick Fill Processing

- `indicators/math_engine.py` — added `sma()` (simple moving average via a
  vectorized cumulative-sum window; SMA is not an IIR filter like
  EMA/ATR/ADX, so no recursive loop is needed).
- `strategy/execution_triggers.py`:
  - `detect_breakout()` — 2-candle breakout pattern: latest close must
    clear the prior bar's high (bullish) or low (bearish) by ≥ 50 broker
    points (`min_breakout_points`, using the instrument's `point` size);
    `volume_confirmed` requires `tick_volume[-1] > SMA(20)(tick_volume) × 1.5`.
    `BreakoutSignal.is_valid` requires both.
  - `detect_pullback()` — trend-continuation pullback: within an
    established trend direction, the latest bar's low/high must touch or
    cross a `reference_level` (e.g. the trend's own EMA) intrabar but
    close back on the trend side of it.
  - `analyze_wick_fill()` — classifies the latest bar's upper/lower shadow
    as a fraction of its full range; > 60% (`WICK_FILL_THRESHOLD`) on
    either side is a rejection signal. Zero-range bars yield `NONE`
    rather than a division error.
- `docs/RESEARCH.md` §8 ("Entry Trigger Specification") — added to make
  the phase directive's "exactly as mapped in `docs/RESEARCH.md`"
  instruction true going forward.
- `indicators/README.md`, `strategy/README.md` updated to describe the
  landed implementation.

### Flagged — pattern shapes were not previously specified anywhere

The phase directive's 50-point filter and `Tick_Volume > SMA(20) × 1.5`
threshold were fully specified; the *shape* of the "2-candle breakout
pattern" and "pullback logic" was not — `docs/RESEARCH.md` contained no
entry-trigger specification prior to this phase (it only covered the WFO
research spec). Rather than inventing behavior silently, this phase
authored `docs/RESEARCH.md` §8 defining both pattern shapes using
standard, well-documented technical-analysis conventions (prior-bar-range
breakout; EMA-as-support/resistance pullback), marked with an explicit
provenance note inviting correction if a different shape was intended.

### Verified

- `ruff check .` and `ruff format --check .` — all checks passed (17 files).
- `mypy --strict .` — no issues found in 17 source files (mypy 2.1.0
  locally, per the Phase 4 toolchain note; `pyproject.toml`'s pinned
  `mypy==1.11.2` is unchanged).
- `pytest` — 0 tests collected against the still-empty `tests/` layout, as
  expected (automated `tests/strategy/` coverage deferred to the project's
  dedicated testing phase).
- `sma()`: exact match (`rtol=1e-10`) against an independent pure-Python
  windowed-mean reference over 100 random bars.
- `detect_breakout()`: bullish/bearish triggers at the exact 50-point
  boundary, no-trigger just under the boundary, volume-confirmed vs.
  not-confirmed `is_valid` gating, and `ValueError` on `point <= 0` /
  insufficient bars.
- `detect_pullback()`: bullish and bearish pullback detection, `"NONE"`
  trend direction always yielding no signal regardless of price action,
  and the boundary case where close sits exactly at the reference level
  (correctly excluded — strict inequality required).
- `analyze_wick_fill()`: long-lower-wick → `BUY`, long-upper-wick →
  `SELL`, balanced-body → `NONE`, and a zero-range bar handled safely
  (`NONE`, ratios `0.0`, no division error).

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
