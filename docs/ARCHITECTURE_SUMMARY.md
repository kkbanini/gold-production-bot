# Architecture Summary — End of Phase 10

| Field | Value |
|---|---|
| Status | Capstone document — read this first before touching a real account |
| Covers | Phases 0–10, VERSION `0.11.0` |

This document is the single place to understand what this system actually
does, what it deliberately doesn't do yet, and exactly what to check before
connecting it to a real MT5 account — demo or live.

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

Every phase's commit message and the corresponding `CHANGELOG.md` entry
records what was verified and what was explicitly flagged as a design
choice made without a pre-existing spec to follow. This document
consolidates those flags into one place rather than repeating them.

## 2. How a bar-close cycle actually works

`main.py`'s `run_bar_close_cycle()` is the decision core — a pure function,
fully unit-tested, no I/O. Once per M5 bar close, `main()` (the impure loop)
gathers a `MarketSnapshot` (D1/H4/H1 bars, account equity, trend/entry
signals, news events) and calls it:

1. **Processing-cap check** — measures how long gathering + deciding took;
   logs a warning past 200ms. This is an operational metric, not a circuit
   breaker — a slow cycle doesn't block trading.
2. **Drawdown check** — daily/weekly/monthly equity drawdown against
   baselines. Any of 5%/10%/20% breached → `TradingState.HALTED`. Once
   halted, the loop **never auto-resumes**, even if equity recovers — per
   `docs/RUNBOOK.md`'s established `CRITICAL` posture, a human must review
   and restart.
3. **News blackout check** — blocks new entries within ±30 minutes of a
   core macro event.
4. **If flat**: combine the master trend filter with the three independent
   entry triggers (see §3 below) into a `BUY`/`SELL`/`NONE` decision; if
   non-`NONE`, size the position (equity-compounded lots) and compute an
   ATR-based initial stop.
5. **If in a position**: check partial-close-at-`Base_TP`+breakeven first;
   if that didn't fire, check the ATR trailing stop.

`main()` executes whatever the pure function proposed (submits the market
order or position actions to the broker, persists the ledger row, advances
`FSMContext` for the next cycle) and loops back to sleep until the next M5
boundary.

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
  `submit_market_order()` and `submit_position_action()` submit directly;
  there is no duplicate-order idempotency check against the ledger before
  submission, and no rejection of fills outside a slippage tolerance band.
  This is the single most important gap to close before live trading —
  see `docs/RISK_REGISTER.md` RR-006/RR-007.
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
  `EquityBaselines` from the first cycle's equity and never refreshes them.
  This means the "daily" breaker is actually measuring drawdown since
  *process start*, not since the start of the current UTC day — it will
  silently stop meaning "daily" after the process has run past midnight
  UTC. **This must be fixed (baseline rollover logic) before relying on the
  drawdown breakers for real risk control.**
- **The news feed is never actually connected in the live loop.**
  `main()` calls `_fetch_market_snapshot(handles.gateway, constraints.magic_number, [])`
  — the empty list is a hardcoded placeholder, not real news events. The
  NFP/CPI/FOMC blackout and the News-API-down fail-safe (Phase 7) are both
  fully implemented and tested in isolation, but nothing in `main.py` calls
  `news.news_engine.fetch_calendar_events()`. Wiring this in requires
  picking a real calendar provider first (Phase 7 flagged that no provider
  was ever named).
- **`ENVIRONMENT_MODE`'s broker-side cross-check never landed.** `config/`
  validates `ENVIRONMENT_MODE` is `DEMO`/`LIVE`, but `broker/mt5_gateway.py`
  never cross-checks that value against the actually-connected account's
  real demo/live status (RR-012). Nothing currently prevents starting the
  process with `ENVIRONMENT_MODE=DEMO` while actually connected to a live
  account, or vice versa.

## 6. What is and isn't covered by the automated test suite

97%+ overall coverage (`pytest --cov=. --cov-fail-under=90`), but that
number describes the *pure logic* — every indicator, every trend/entry
signal, every risk/position calculation, every storage operation, and
`main.py`'s `run_bar_close_cycle()` decision function. It does **not**
cover:

- `bootstrap_system()`, `_fetch_market_snapshot()`, or `main()`'s loop body
  — the impure I/O layer, untestable without a live or heavily-mocked MT5
  terminal, and (per your explicit direction) not executed in this
  environment.
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
3. **Decide on and wire a real news calendar provider** (§5) — currently a
   hardcoded empty list; the blackout logic is inert until this is done.
4. **Add the broker-side `ENVIRONMENT_MODE` cross-check** (RR-012, §5)
   before trusting the demo/live guard.
5. **Review every flagged design choice in §3** — especially the entry
   signal combination rule and the compounding/risk parameters. None have
   been validated against historical data.
6. **Run in `paper` mode first**, per `docs/DEPLOYMENT.md`'s promotion
   gates, for the minimum observation window defined in
   `docs/RESEARCH.md` §6 (≥100 closed trades, ≥30 calendar days) — with a
   human watching, not unattended.
7. **Set up real alerting** per `docs/RUNBOOK.md` §5 — right now, `HALTED`
   and processing-cap breaches only go to the Python `logging` module, not
   to anything that pages a human.
8. Only after all of the above: promote to `live`, following
   `docs/DEPLOYMENT.md`'s explicit human sign-off gate — and start with the
   smallest real position size the compounding formula allows.
