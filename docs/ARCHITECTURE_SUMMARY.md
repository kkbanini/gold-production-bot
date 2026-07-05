# Architecture Summary — End of Phase 11 (1.0.0-RC1)

| Field | Value |
|---|---|
| Status | Capstone document — read this first before touching a real account |
| Covers | Phases 0–11 (all sub-phases 11a–11e landed) |

This document is the single place to understand what this system actually
does, what it deliberately doesn't do yet, and exactly what to check before
connecting it to a real MT5 account — demo or live.

> **Phase 11 / `1.0.0-RC1` note**: `docs/PRODUCTION_SPEC.md` introduced a
> set of production-hardening contracts (secrets/boot validation, dynamic
> calendar failover, a normalized clock abstraction, pre-flight
> idempotency, event sourcing, a pure-function drawdown FSM, and
> resiliency/disaster recovery), implemented as gated sub-phases (11a
> through 11e), each with its own `CHANGELOG.md` entry, exactly like
> Phases 0–10. `VERSION` now reads `1.0.0-RC1` (a **release candidate**,
> not a stable `1.0.0`) — every §7 spec contract this sub-phase covers is
> genuinely implemented and tested, but §5 below lists real, unclosed
> gaps (no pre-trade risk gate, no backtester, the news feed/clock
> provider/manual-reset channel still not wired into the live loop, SLO
> metrics never built). `-RC1` signals "ready for structured human
> review and paper-trading observation," not "ready for live capital."
> Dropping the suffix to a plain `1.0.0` requires those gaps to actually
> close and `docs/DEPLOYMENT.md`'s promotion gates to be observed, not
> just documented.

## 1. What was built, phase by phase

| Phase | Delivered | Key files |
|---|---|---|
| 0 | Institutional scaffolding: ADRs, API contracts, risk register, runbook, research spec | `docs/adr/*`, `docs/API_SPEC.md`, `docs/RESEARCH.md` |
| 1 | Environment/secrets loading, linter enforcement | `config/config_manager.py`, `pyproject.toml` |
| 2 | SQLite (WAL) persistence: trade ledger, FSM-state crash recovery | `storage/db_engine.py`, `storage/state_manager.py` |
| 3 | MT5 broker gateway: dynamic Gold symbol resolution, GMT session window, reconnect backoff, position audit | `broker/mt5_gateway.py` |
| 4 | Pure numpy indicators (EMA/ATR/ADX) and the D1/H4/H1 master trend filter | `indicators/math_engine.py`, `strategy/trend_filter.py` |
| 5 | Entry triggers: 2-candle breakout, pullback, wick-fill rejection | `strategy/execution_triggers.py` |
| 6 | Equity-based lot compounding, partial-close/breakeven, ATR trailing stop | `risk/risk_manager.py`, `execution/position_manager.py` |
| 7 | Economic calendar client, NFP/CPI/FOMC blackout, News-API-down fail-safe | `news/news_engine.py` |
| 8 | Saturday-gated rule-based parameter shift, Monte Carlo bootstrap, isolated `parameter_history` | `optimizer/self_learning.py` |
| 9 | First formal automated test suite (unit + integration), 97%+ coverage | `tests/` |
| 10 | Master FSM orchestration loop, 200ms processing-cap metric, drawdown hard locks, account/bar/order broker methods | `main.py`, `broker/mt5_gateway.py` |
| 11a | Boot-validation hardening (placeholder-leak detection), secret-redacting log filter, `ApplicationContainer` DI composition root | `config/config_manager.py`, `config/secret_redaction.py`, `container.py` |
| 11b | `CalendarProvider` chain (configurable priority/failover, per-provider rate limiter) and `ClockProvider` (normalized broker server time), wired into `ApplicationContainer` | `news/calendar_provider.py`, `config/calendar_config.py`, `broker/clock_provider.py`, `container.py` |
| 11c | Pre-flight idempotency ledger write + append-only order-lifecycle Event Store/projection, a lightweight schema-migration framework, and the `PreTradeValidator` duplicate-order gate — wired into `main.py`'s two real broker-submission call sites | `storage/migrations.py`, `storage/state_manager.py`, `execution/validation.py`, `broker/mt5_gateway.py`, `main.py` |
| 11d | Centralized pure-function drawdown FSM (`ACTIVE`/`WARNING`/`SOFT_LOCK`/`HARD_LOCK`/`MANUAL_RESET_REQUIRED`, superseding Phase 10's single-tier `HALTED`), `FeatureFlagManager`-driven `HARD_LOCK` liquidate-vs-freeze behavior, wired live into `main.py`'s bar-close cycle | `risk/drawdown_fsm.py`, `config/feature_flags.py`, `execution/position_manager.py`, `main.py` |
| 11e | Exponential backoff/retry budget for network I/O, SQLite `busy_timeout`, the immutable Audit Trail, Disaster Recovery reconciliation (settles broker/ledger divergence, gates FSM startup, seeds `main()`'s context from live broker state) — all wired live; test suite reorganized into `tests/unit`/`tests/integration`/`tests/chaos`/`tests/stress` | `resilience/backoff.py`, `storage/db_engine.py`, `storage/migrations.py`, `storage/state_manager.py`, `broker/mt5_gateway.py`, `container.py`, `main.py`, `tests/` |

Every phase's commit message and the corresponding `CHANGELOG.md` entry
records what was verified and what was explicitly flagged as a design
choice made without a pre-existing spec to follow. This document
consolidates those flags into one place rather than repeating them.

## 2. How boot and a bar-close cycle actually work

**Boot (Phase 11e, `docs/PRODUCTION_SPEC.md` §7's Disaster Recovery
bullet)**: `ApplicationContainer.build()` connects the broker, reads the
locally persisted `trade_ledger` (the "last known state projection"),
reconciles it against the broker's live open positions
(`audit_open_positions()`, Phase 3), then calls the new
`resolve_position_audit()` to *settle* any divergence found — broker-only
positions become new reconciled `trade_ledger` rows, ledger-only entries
are marked `CLOSED_RECONCILED` — and records a `DISASTER_RECOVERY_RECONCILIATION`
Audit Trail entry if anything needed fixing. Any divergence at all sets
`initial_drawdown_state = MANUAL_RESET_REQUIRED` (`docs/RUNBOOK.md`'s own
established policy already treats a position-audit mismatch as
`HIGH`-severity, blocking automated trading pending manual review —
RR-008); a clean reconciliation sets `ACTIVE`. `main()` then calls
`_seed_initial_fsm_context()`, which queries the broker's live open
positions directly (not a stale local snapshot) to decide whether to
start `IN_POSITION` or `IDLE`.

`main.py`'s `run_bar_close_cycle()` is the decision core — a pure function,
fully unit-tested, no I/O. Once per M5 bar close, `main()` (the impure loop)
gathers a `MarketSnapshot` (D1/H4/H1 bars, account equity, trend/entry
signals, news events) and calls it:

1. **Processing-cap check** — measures how long gathering + deciding took;
   logs a warning past 200ms. This is an operational metric, not a circuit
   breaker — a slow cycle doesn't block trading.
2. **Drawdown FSM transition** (Phase 11d, `risk/drawdown_fsm.py`,
   `docs/PRODUCTION_SPEC.md` §6) — `classify_drawdown_event()` reduces
   daily/weekly/monthly equity drawdown against baselines into a single
   severity event, and `transition_drawdown_state()` folds that into one
   of 5 states: `ACTIVE`, `WARNING`, `SOFT_LOCK` (5%/10%/20% — freezes new
   entries only; position management keeps running), `HARD_LOCK`
   (10%/20%/40% — triggers `FeatureFlagManager`'s liquidate-vs-freeze
   decision), `MANUAL_RESET_REQUIRED` (the state `HARD_LOCK` always
   advances into next cycle, sticky until a human sends
   `MANUAL_RESET_CONFIRMED`). `ACTIVE`/`WARNING`/`SOFT_LOCK` are all
   recoverable as equity improves; only a `HARD_LOCK` breach requires
   explicit human intervention to clear — see §3 for how this evolves
   Phase 10's original single-tier "never auto-resumes" posture.
3. **News blackout check** — blocks new entries within ±30 minutes of a
   core macro event.
4. **If flat and not `SOFT_LOCK`/`HARD_LOCK`/`MANUAL_RESET_REQUIRED`**:
   combine the master trend filter with the three independent entry
   triggers (see §3 below) into a `BUY`/`SELL`/`NONE` decision; if
   non-`NONE`, size the position (equity-compounded lots) and compute an
   ATR-based initial stop.
5. **If in a position and not `HARD_LOCK`/`MANUAL_RESET_REQUIRED`**: check
   partial-close-at-`Base_TP`+breakeven first; if that didn't fire, check
   the ATR trailing stop. (`SOFT_LOCK` explicitly still reaches this step.)
6. **If `HARD_LOCK` was just entered and a position is open**: either emit
   `build_emergency_liquidation_action()`'s full-volume close (if
   `config.flags.liquidate_on_hard_lock` is `true`) or emit nothing at all
   (an absolute freeze) — this step pre-empts steps 4/5 entirely for that
   cycle.

`main()` executes whatever the pure function proposed. Every broker
submission — a new market order, a position action, *or* (Phase 11d) an
emergency liquidation — now goes through `submit_with_pre_flight_ledger()`
(Phase 11c, `docs/PRODUCTION_SPEC.md` §4): a `REQUESTED` event is written
to the order-lifecycle Event Store *before* the payload reaches the MT5
gateway, then `SENT` + a terminal `FILLED`/`MODIFIED` event on success, or
`REJECTED` on rejection (re-raising unchanged). A confirmed liquidation
additionally clears `FSMContext.position` back to `None` (unlike a
partial close, a full-volume liquidation leaves nothing open to manage
next cycle). `main()` then persists the `trade_ledger` row, advances
`FSMContext` for the next cycle, and loops back to sleep until the next
M5 boundary.

## 3. Design choices made without a pre-existing spec

Every phase in this project flagged interpretive choices rather than
silently picking one. Phase 10 adds one more, on top of the ones already
recorded in each module's own README and `CHANGELOG.md` entry:

- **Combining the three independent entry-trigger signals into one
  decision** was explicitly deferred by every phase since Phase 5
  ("`execution/`'s concern, a later phase"). `main.py`'s
  `decide_entry_signal()` requires a confirmed trend alignment, no news
  blackout, and at least one of breakout/pullback/wick-fill agreeing with
  the trend direction. This is a reasonable, simple rule — not a
  backtested one. **Before live use, this rule should be validated against
  historical data**, which brings us to the next point.

Recap of earlier phases' flagged choices, still open:

- **Breakout/pullback pattern shapes** (Phase 5, `docs/RESEARCH.md` §8) —
  standard technical-analysis conventions, not independently specified.
- **Compounding tier parameters** (Phase 6, `risk/README.md`) — one
  micro-lot per $1000 of equity, a made-up-but-documented default.
- **"Double spread limits" on news-feed failure** (Phase 7,
  `news/README.md`) — implemented literally (more permissive), which reads
  oddly for a "defensive" mechanism; flagged as possibly meaning the
  opposite.
- **Self-learning rule thresholds** (Phase 8, `optimizer/README.md`) — 10
  trades / 40% win rate / 1.0 profit factor triggers, made up and
  documented.
- **Initial entry stop distance** (Phase 10) — `ENTRY_ATR_STOP_MULTIPLIER`
  mirrors `Base_TP`'s 2.0× ATR multiplier for symmetry; not independently
  derived.
- **Default `CALENDAR_PROVIDER_PRIORITY` is `("offline_snapshot",)` alone,
  not the spec's illustrative `['tradingeconomics', 'finnhub',
  'offline_snapshot']`** (Phase 11b, `news/README.md`,
  `config/README.md`) — the 3-provider chain is fully supported and
  opt-in via `CALENDAR_PROVIDER_PRIORITY`, but making it the unconditional
  default would mean every existing deployment/test fails to boot without
  first obtaining and configuring two vendor base URLs this project has
  never verified. Defaulting to the network-independent fallback alone
  keeps boot safe out of the box; flagged in case the intent was for the
  full chain to be mandatory.
- **`CalendarConfig.timeout_ms` applies to both the HTTP connect and read
  phase** (Phase 11b, `news/calendar_provider.py`) — the spec gives one
  unified `timeout_ms`, while `news_engine.fetch_calendar_events()` (Phase
  7) takes separate connect/read timeouts; this phase applies the same
  derived value to both rather than inventing an unspecified split.
- **`order_ledger.timestamp` uses the spec's literal column name**, not
  this project's usual `_utc`-suffixed convention (`opened_at_utc`,
  `created_at_utc`, etc.) — Phase 11c, `storage/migrations.py` — since
  §4's SQL example names it verbatim (`INSERT INTO order_ledger
  (client_order_id, state, timestamp) ...`). It holds the same UTC ISO8601
  string format as every other timestamp column in this codebase; only the
  column *name* deviates from house style, deliberately, to match the spec
  literally.
- **The schema-migration framework (`storage/migrations.py`) does not
  retrofit Phase 2/3/8's original tables as a synthetic "migration zero"**
  (Phase 11c) — `trade_ledger`/`system_state`/`parameter_history` remain
  under `db_engine.initialize_schema()`'s unconditionally-idempotent static
  DDL, unchanged. The migration framework governs only schema changes from
  Phase 11c forward (currently: `order_events`/`order_ledger`). Retrofitting
  the older tables would add churn/risk to already-tested,
  already-provisioned tables for no behavioral benefit.
- **Ambiguous post-`SENT` retry state is refused, not assumed safe**
  (Phase 11c, `execution/validation.py`) — if an order reached `SENT`/
  `PENDING`/etc. but the broker doesn't currently confirm an open ticket
  for it, `check_duplicate_order_before_retry()` returns
  `AMBIGUOUS_SENT_STATE_MANUAL_REVIEW_REQUIRED` (not retryable) rather than
  treating "no open ticket" as proof no fill occurred — a fill can be
  closed again before the check runs. This is the conservative reading of
  "strictly mitigating duplicate order anomalies", but it means a
  legitimately-safe retry can be blocked pending manual review; flagged in
  case a less conservative policy was actually intended.
- **Phase 11c wires the pre-flight ledger write directly into `main.py`'s
  two real broker-submission call sites**, a deliberate departure from
  Phase 11b's precedent of building an abstraction without touching
  `main.py`'s loop. The difference: `main.py` already generated a
  `client_order_id` and called the broker at exactly these two call sites,
  so adding `REQUESTED`/`SENT`/terminal-event writes around the existing
  calls is a small, bounded, behavior-preserving addition (the happy path
  is unchanged; the rejection path still propagates the same exception,
  just after recording it) — unlike wiring an actual backoff-driven retry
  *loop*, which doesn't exist yet and is `docs/PRODUCTION_SPEC.md` §7's
  explicit, differently-scoped domain (see §5's gap entry above).
- **`HARD_LOCK` thresholds (10%/20%/40%, double each `SOFT_LOCK` tier) and
  the `WARNING` ratio (60% of the nearest `SOFT_LOCK` limit) are new,
  made-up-but-documented defaults** (Phase 11d, `risk/drawdown_fsm.py`,
  `risk/README.md`) — `docs/PRODUCTION_SPEC.md` §6 names the 5 states but
  gives no catastrophic-tier percentage; `SOFT_LOCK`'s 5%/10%/20% is
  unchanged from Phase 10's original hard locks (RQ-022), and `HARD_LOCK`
  simply doubles each, flagged for review like every other invented
  numeric default in this project (compounding tiers, optimizer
  thresholds, etc.).
- **`ACTIVE`/`WARNING`/`SOFT_LOCK` are all recoverable; only `HARD_LOCK`
  requires a human to clear** (Phase 11d) — a deliberate evolution beyond
  Phase 10's "once halted, never auto-resumes at any severity" posture.
  Each bar-close cycle re-classifies drawdown fresh, so a lower tier can
  step back down as equity recovers; only crossing into `HARD_LOCK` is
  sticky (`MANUAL_RESET_REQUIRED`, cleared solely by an explicit
  `DrawdownEvent.MANUAL_RESET_CONFIRMED`). Flagged in case the intent was
  for every tier to remain sticky like Phase 10's single `HALTED` state.
- **No live channel exists for a human to actually send
  `MANUAL_RESET_CONFIRMED`** (Phase 11d) — `run_bar_close_cycle()`'s
  `manual_reset_confirmed` parameter is fully wired and tested, but
  `main()` always passes `False` today; there is no API/CLI/admin signal
  an operator could use to set it against a running process. See §5.
- **`FLAG_LIQUIDATE_ON_HARD_LOCK` defaults to `false` (freeze, not
  liquidate)** (Phase 11d, `config/feature_flags.py`) — the spec describes
  both branches without stating a default; freezing is the safer,
  capital-preserving choice when a deployer hasn't made an explicit
  decision, consistent with this project's fail-closed defaults elsewhere.
- **`resilience.backoff`'s `max_attempts` counts retries *after* the
  initial attempt** (Phase 11e, `resilience/README.md`) — a "5 total
  tries" reading of the spec's "Max 5 attempts: 2s, 4s, 8s, 16s, 32s"
  would leave the last listed delay (32s) permanently unused (5 tries =
  4 gaps between them); counting `max_attempts` as the retry budget
  itself (so the default of 5 permits up to 6 total attempts) is the
  reading that actually exercises every listed value.
- **`NetworkCalendarProvider`'s retry budget is deliberately tiny
  (1 retry, one 2s delay), not `resilience.backoff`'s full default**
  (Phase 11e, `news/README.md`) — applying the full 5-retry/2s–32s budget
  in front of every provider attempt would stack a ~62-second worst case
  in front of `CalendarProviderChain`'s fallback, directly undermining
  Phase 11b's "clean transition between providers" guarantee. Flagged as
  a deliberate, scoped-down application of the same mechanism, not an
  inconsistency.
- **`MT5Gateway.connect()` was not refactored onto `resilience.backoff`**
  (Phase 11e) — it already has its own independently-tuned, already-tested
  exponential backoff (Phase 3/RR-002) with a different retry-counting
  convention and a `max_delay_seconds` cap this module doesn't have.
  Reconciling the two conventions purely for de-duplication would add
  risk to already-verified reconnect behavior for no behavioral benefit.
- **`audit_trail.action_type` has no `CHECK` constraint**, unlike
  `order_events.event_type`'s closed 11-state set (Phase 11e,
  `storage/state_manager.py`'s `AuditActionType`) — the spec's "Circuit
  breaker reset, Manual overrides, Flag alterations" reads as an
  illustrative list, not an exhaustive one, so the column accepts any
  string rather than silently rejecting a legitimate future action type
  the enum doesn't yet name.
- **SLO Metrics (§7's fourth bullet — a background daemon thread tracking
  `trade_latency`/`spread`/`order_reject_rate`/`mt5_latency`/`retry_count`/
  `heartbeat_failures`) were not built this sub-phase** — the phase
  directive's explicit 4-item instruction list for 11e named exponential
  backoff/SQLite resiliency, the Audit Trail, Disaster Recovery, and test
  suite reorganization, substituting the last for §7's own fourth bullet
  rather than including it. Flagged prominently rather than silently
  dropped: this is a real, unclosed gap against the full §7 spec text,
  tracked as its own row in `docs/TRACEABILITY_MATRIX.md` rather than
  folded into an "implemented" row that would overstate coverage.
- **Test-suite reorganization measurably lowered two files' *standard-pipeline*
  coverage** (Phase 11e, `tests/README.md`) — `news/news_engine.py`
  (97%→77%) and `broker/mt5_gateway.py` (95%→93%) both had their only
  HTTP/reconnect-fault tests relocated to `tests/chaos/`, which
  `pyproject.toml`'s `testpaths` now excludes by default. The `--cov-fail-under=90`
  gate still passes (95%+ total), and both files are fully exercised
  again by `pytest tests/chaos`, but this is a real, intentional
  trade-off of the reorganization the phase directive asked for, not an
  accident — flagged so it isn't mistaken for a regression later.

None of these are wrong, exactly — they're reasonable defaults that were
never validated against real Gold price history, because **no backtesting
module exists** (see §5). Treat every number above as a hypothesis to test,
not a result.

## 4. A real bug this phase caught in its own wiring

While writing `_fetch_market_snapshot()`, the pullback trigger's
`reference_level` argument was initially wired to `h1_bars.close` — the
same array as the price being compared against it. `detect_pullback()`'s
bullish condition requires `low <= level < close`; with `level == close`,
`level < close` is `close[-1] < close[-1]`, which is mathematically always
`False`. The pullback signal would never have fired in live operation,
silently reducing the entry-trigger combination to breakout-or-wick-fill
only. Caught by re-reading the wiring against `strategy/trend_filter.py`'s
own logic (it evaluates alignment against each timeframe's EMA, not raw
price) rather than by a failing test — no test previously exercised
`_fetch_market_snapshot()` itself, since it's part of the untested impure
I/O layer (§6). Fixed by computing the H1 EMA(40) directly (`indicators.math_engine.ema`)
and passing that as the reference level, matching what `trend_filter`
itself compares against.

**Takeaway for whoever extends this next**: the impure I/O glue functions
(`bootstrap_system`, `_fetch_market_snapshot`, `main`) are exactly the part
of this codebase least protected by tests. Read them carefully; don't
assume that because the pure functions they call are well-tested, the
wiring between them is correct too.

## 5. Known gaps — what this system does NOT do yet

- **No pre-trade risk gate or slippage guard (RQ-009/RQ-010).**
  `submit_market_order()` and `submit_position_action()` still submit
  directly with no rejection of fills outside a slippage tolerance band.
  This is the single most important gap to close before live trading —
  see `docs/RISK_REGISTER.md` RR-006.
- **The duplicate-order idempotency check (RR-007) is built and tested but
  has no retry loop to protect, since none exists — and building one
  safely needs a broker-side change first.** Phase 11c built
  `main.py`'s `submit_with_pre_flight_ledger()` (writes a `REQUESTED`
  event before every broker submission) and
  `execution.validation.check_duplicate_order_before_retry()` +
  `MT5Gateway.is_ticket_still_open()` (ready to audit the ledger and the
  broker's live position cache before a retry); Phase 11e built the
  generic retry mechanism itself (`resilience.backoff.retry_with_backoff()`,
  the exact 2s/4s/8s/16s/32s sequence). But wiring these together into an
  actual order-submission retry loop is still unsafe today:
  `broker/mt5_gateway.py`'s `submit_market_order()`/`submit_position_action()`
  fold "MT5 explicitly rejected this" and "MT5 returned nothing (ambiguous
  — possibly a transient timeout)" into the same `BrokerOrderRejectedError`.
  Retrying indiscriminately on that exception would retry genuine,
  permanent rejections (e.g. invalid stops) that no retry can ever fix —
  a real correctness risk, not just an unfinished feature. A single
  `BrokerOrderRejectedError` still propagates uncaught and halts the
  process today, which is the safe default in the meantime.
- **No anchored walk-forward optimization or backtester** (RQ-013–RQ-016).
  ADR-0004's full DSR/IS-OOS-gated optimization design was never built;
  `optimizer/self_learning.py` is a deliberately simpler rule-based
  mechanism (Phase 8). `backtester/` and `analytics/` don't exist — flagged
  unscheduled since Phase 2. **This means none of the strategy's numbers
  (trend/entry/risk parameters) have ever been validated against
  historical price data.**
- **No `EventBus`/`core`** (RQ-003, RQ-004, RQ-006). `main.py`'s loop is a
  simple poll-and-decide cycle, not the fully event-driven architecture
  ADR-0001 originally specified. Crash recovery is single-snapshot
  (`storage/state_manager.py`), not full event-log replay.
- **Equity baselines for drawdown checks are seeded once at process start,
  not rolled over at UTC day/week/month boundaries.** `main()`'s loop sets
  `EquityBaselines` (now `risk/drawdown_fsm.py`'s, Phase 11d) from the
  first cycle's equity and never refreshes them. This means the "daily"
  tier is actually measuring drawdown since *process start*, not since the
  start of the current UTC day — it will silently stop meaning "daily"
  after the process has run past midnight UTC. **This must be fixed
  (baseline rollover logic) before relying on the drawdown FSM for real
  risk control**, and applies to Phase 11d's `WARNING`/`SOFT_LOCK`/
  `HARD_LOCK` tiers exactly as it did to Phase 10's single hard lock.
- **No live control channel exists for a human to clear a `HARD_LOCK`
  (Phase 11d).** `run_bar_close_cycle()`'s `manual_reset_confirmed`
  parameter and the underlying `DrawdownEvent.MANUAL_RESET_CONFIRMED`
  transition are fully implemented and tested, but `main()` has no
  API/CLI/admin signal for an operator to actually set it — it's always
  `False` today. In practice, a real `HARD_LOCK` today can only be cleared
  by stopping the process, fixing/reviewing the situation, and either
  restarting with a patched `main()` or manually resetting the persisted
  `FSMContext`. Building the actual control channel is unscheduled.
- **The news feed and normalized clock are never actually connected in the
  live loop, despite both now having a working abstraction (Phase 11b).**
  `main()` still calls `_fetch_market_snapshot(handles.gateway,
  constraints.magic_number, [])` — the empty list is a hardcoded
  placeholder, not real news events — and calls `datetime.now(timezone.utc)`
  directly for `run_bar_close_cycle()`'s `now_utc` and the next-bar-close
  sleep, rather than through `ClockProvider.get_server_time()`. The
  NFP/CPI/FOMC blackout and the News-API-down fail-safe (Phase 7) are both
  fully implemented and tested in isolation; `container.py`'s
  `ApplicationContainer` now holds a fully-wired `calendar_provider`
  (`CalendarProviderChain`, defaulting to a network-independent
  `offline_snapshot` provider) and `clock_provider` (`MT5ClockProvider`,
  Phase 11b), but nothing in `main.py` calls either one yet. Wiring the
  network calendar providers to real endpoints additionally requires a
  real `tradingeconomics`/`finnhub` API contract, which this codebase has
  never verified (Phase 7 flagged that no provider was ever named;
  `CALENDAR_TRADINGECONOMICS_BASE_URL`/`CALENDAR_FINNHUB_BASE_URL` must be
  supplied by a deployer, never guessed).
- **`ENVIRONMENT_MODE`'s broker-side cross-check never landed.** `config/`
  validates `ENVIRONMENT_MODE` is `DEMO`/`LIVE`, but `broker/mt5_gateway.py`
  never cross-checks that value against the actually-connected account's
  real demo/live status (RR-012). Nothing currently prevents starting the
  process with `ENVIRONMENT_MODE=DEMO` while actually connected to a live
  account, or vice versa.
- **SLO Metrics (`docs/PRODUCTION_SPEC.md` §7's fourth bullet) were never
  built.** No background daemon thread tracks `trade_latency`, `spread`,
  `order_reject_rate`, `mt5_latency`, `retry_count`, or
  `heartbeat_failures` against any Service Level Objective. Phase 11e's
  explicit instruction list substituted the test-suite reorganization for
  this bullet rather than including it — see §3's flagged note. There is
  currently no automated, structured signal for "is this system healthy
  right now" beyond the Python `logging` module.

## 6. What is and isn't covered by the automated test suite

95%+ total coverage against `pyproject.toml`'s standard `testpaths`
(`tests/unit` + `tests/integration`, Phase 11e's reorganization;
`pytest --cov=. --cov-fail-under=90`), covering the *pure logic* — every
indicator, every trend/entry signal, every risk/position calculation,
every storage operation, `risk/drawdown_fsm.py`'s full state-transition
table (100% coverage), `resilience/backoff.py`'s full retry-budget
behavior (100% coverage), and `main.py`'s `run_bar_close_cycle()` decision
function (including its `SOFT_LOCK`/`HARD_LOCK`/`MANUAL_RESET_REQUIRED`
branches and the emergency-liquidation path). It does **not** cover:

- `_fetch_market_snapshot()` or the bulk of `main()`'s loop body — the
  impure I/O layer, untestable without a live or heavily-mocked MT5
  terminal, and (per your explicit direction) not executed in this
  environment. `submit_with_pre_flight_ledger()` (Phase 11c) and
  `_seed_initial_fsm_context()` (Phase 11e) are exceptions: both take
  their I/O as injected parameters or operate against a real
  `ApplicationContainer` built with a `FakeMT5`, so they're formally
  tested (`tests/unit/test_unit.py::TestSubmitWithPreFlightLedger`,
  `tests/integration/test_integration.py::TestApplicationContainer`)
  despite living in `main.py`.
- Two files' network/reconnect-fault paths *in the standard pipeline
  specifically* (Phase 11e's test-suite reorganization, see §3's flagged
  note): `news/news_engine.py`'s HTTP error handling and
  `broker/mt5_gateway.py`'s `connect()` backoff/exhaustion paths are fully
  exercised, just by `tests/chaos/` (excluded from `testpaths` by design)
  rather than `tests/unit`/`tests/integration`. Run `pytest tests/chaos`
  to see them covered.
- Anything requiring a real MT5 demo/live connection, a real economic
  calendar API key, or observed paper-trading behavior. Per
  `docs/TRACEABILITY_MATRIX.md`'s own Legend, no requirement in this
  project is marked `VERIFIED` — only `IMPLEMENTED` — because `VERIFIED`
  requires exactly that observation.

## 7. Before your first live/demo run

This is the checklist for a human to work through — not something to
delegate back to an autonomous agent turn, since each item either requires
a real credential this environment doesn't have, or a judgment call about
real capital risk.

1. **Get real MT5 demo credentials** and populate a local `.env` (see
   `.env.template`) — never commit it.
2. **Fix the equity-baseline rollover gap** (§5) — the drawdown breakers as
   currently wired don't reset daily/weekly/monthly, which defeats their
   purpose after the first day of operation.
3. **Wire `main.py`'s live loop to the `calendar_provider`/`clock_provider`
   already built into `ApplicationContainer`** (§5) — `main()` still passes
   a hardcoded empty event list and calls `datetime.now(timezone.utc)`
   directly; the blackout logic is inert and the clock isn't normalized
   until this wiring lands. Enabling the network calendar providers
   (`tradingeconomics`/`finnhub`) additionally requires supplying a real,
   verified base URL for each via `CALENDAR_<PROVIDER>_BASE_URL` — none is
   guessed or defaulted.
4. **Add the broker-side `ENVIRONMENT_MODE` cross-check** (RR-012, §5)
   before trusting the demo/live guard.
5. **Review every flagged design choice in §3** — especially the entry
   signal combination rule and the compounding/risk parameters. None have
   been validated against historical data.
6. **Run in `paper` mode first**, per `docs/DEPLOYMENT.md`'s promotion
   gates, for the minimum observation window defined in
   `docs/RESEARCH.md` §6 (≥100 closed trades, ≥30 calendar days) — with a
   human watching, not unattended.
7. **Set up real alerting** per `docs/RUNBOOK.md` §5 — right now,
   `WARNING`/`SOFT_LOCK`/`HARD_LOCK`/`MANUAL_RESET_REQUIRED` transitions
   and processing-cap breaches only go to the Python `logging` module, not
   to anything that pages a human. This is especially important for
   `HARD_LOCK`, since (§5) no live channel exists yet for a human to clear
   it remotely.
8. **Make a deliberate decision on `FLAG_LIQUIDATE_ON_HARD_LOCK`** (§3) —
   the default (`false`, freeze) means a real `HARD_LOCK` breach halts
   everything and waits for a human; setting it `true` means a `HARD_LOCK`
   breach automatically closes any open position at market. Understand
   which behavior you actually want before a real breach forces the
   question.
9. **Build real SLO metrics and alerting** (§5) — no background daemon
   thread tracks `trade_latency`/`spread`/`order_reject_rate`/
   `mt5_latency`/`retry_count`/`heartbeat_failures` yet, and the
   Audit Trail (`storage.state_manager.get_audit_trail()`) is queryable
   but has no dashboard/export — right now, checking system health means
   reading the Python `logging` output directly.
10. **Review the Disaster Recovery reconciliation's behavior once against
    a real account** (§2) — `ApplicationContainer.build()`'s
    `resolve_position_audit()` auto-settles broker/ledger divergence into
    the `trade_ledger` and blocks the FSM at `MANUAL_RESET_REQUIRED`
    rather than auto-resuming; a human should confirm this is the
    intended behavior (versus, say, requiring confirmation before even
    the ledger write) before relying on it after a real crash.
11. Only after all of the above: promote to `live`, following
    `docs/DEPLOYMENT.md`'s explicit human sign-off gate — and start with
    the smallest real position size the compounding formula allows.
