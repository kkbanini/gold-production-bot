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

A `-RC<N>` pre-release suffix (e.g. `1.0.0-RC1`, per SemVer §9's pre-release
identifier syntax) marks a **release candidate**: every `docs/PRODUCTION_SPEC.md`
contract scheduled for that release is implemented and tested, but
`docs/ARCHITECTURE_SUMMARY.md` §5's open gaps are not yet closed and no
paper-trading observation has occurred. Dropping the suffix to a plain
`1.0.0` requires those gaps to close and `docs/DEPLOYMENT.md`'s promotion
gates to actually be observed, not just documented — the same "not on the
strength of a specification alone" standard this project has held since
Phase 11a.

## [Unreleased]

### Fixed — Exness Cent Account's Gold Symbol (`XAUUSDc`) Wasn't in the Candidate List

- Found live connecting the new Exness Cent account (`Exness-MT5Real37`):
  `resolve_gold_symbol()` raised `BrokerSymbolUnavailableError` — none of
  `GOLD_SYMBOL_CANDIDATES` matched. Confirmed via a direct `mt5.symbols_get()`
  query against the live connection that this broker's actual gold symbol
  is `XAUUSDc` (a `c` suffix for Cent accounts — distinct from the
  already-covered `m` micro-account suffix). Since `ApplicationContainer.build()`
  calls `gateway.connect()` (which raises this) *before* `main()`'s
  try/except crash-alert boundary is even entered, this failure crashed
  `main.py` **silently** — no Telegram notification, just a dead process
  (confirmed: no heartbeat update for ~57 minutes, no running process).
- Fixed: added `"XAUUSDc"` to `GOLD_SYMBOL_CANDIDATES`
  (`broker/mt5_gateway.py`). Confirmed live: reconnects, resolves
  `XAUUSDc`, reads `balance=9997.6`, `currency="USC"` (matching the
  Cent-account normalization fix above), `leverage=1000`,
  `trade_mode="REAL"`.
- Still an open gap, not fixed here: a `resolve_gold_symbol()` failure (or
  any other exception during `ApplicationContainer.build()`) crashes with
  no Telegram alert at all, unlike every in-loop failure once the
  container exists. Worth a follow-up if broker/account misconfiguration
  at boot becomes a recurring failure mode.

### Fixed — Regular Mode's Lot-Size Compounding Assumed a USD-Denominated Account

- Found before connecting a new Exness Cent account (deposit currency
  `USC`, where 1 real USD = 100 USC — `equity`/`balance` report ~100x
  larger for the same real capital than on a standard USD account).
  `risk.risk_manager.calculate_compounded_lot_size()`'s tier formula
  ("+0.01 lot per `$1000` of equity") compares raw `equity` directly
  against a hardcoded absolute `1000.0` — correct only if `equity` is
  actually USD. On a Cent account, `equity=1000` (really just $10) would
  have reached the *same* compounding tier as a real $1000 balance,
  sizing the regular (`WAIT_FOR_CONDITIONS`/`BOTH`) mode's entries ~100x
  too large relative to real capital at stake. Percentage-based uses of
  equity (drawdown thresholds) and tick_value-normalized price-distance
  formulas (`calculate_price_distance_for_target_profit()`, and the
  short-term mode's equity-fraction SL/TP cap added earlier this session)
  were already unaffected — both sides of those ratios scale together
  automatically; only this one hardcoded-absolute-amount comparison
  wasn't.
- Fixed: `broker.mt5_gateway.AccountState` gained a `currency` field (read
  from `mt5.account_info().currency`, defaults to `""` for backward
  compatibility). New `risk.risk_manager.normalize_cent_denominated_equity(equity,
  account_currency)` divides by 100 when `account_currency` is a known
  Cent-account code (`CENT_ACCOUNT_CURRENCY_CODES = {"USC"}` today —
  confirmed against Exness specifically; an unrecognized code safely
  falls back to unchanged/pre-existing behavior rather than guessing) —
  wired into `main.py`'s `calculate_compounded_lot_size()` call site only.
- Flagged for re-verification once actually connected to the new
  account: `mt5.account_info().currency`'s exact reported string should
  be confirmed live (read-only check) against `CENT_ACCOUNT_CURRENCY_CODES`
  before trading the regular/`BOTH` mode on it.

### Fixed — `AutoTrading Disabled` Crashed the Whole Process on Every New-Order/Modify Attempt

- Live crash: `BrokerOrderRejectedError("order_send failed opening a new
  SELL position: retcode=10027, last_error=(1, 'Success')")`. Retcode
  `10027` is MT5's `TRADE_RETCODE_CLIENT_DISABLES_AT` (the terminal's
  "Algo Trading" toggle switched off); `10026`
  (`TRADE_RETCODE_SERVER_DISABLES_AT`, a server-side disable) is the same
  class of condition. Both are unambiguous, purely external — nothing
  about the specific order was wrong, and no local state was left
  dangling (`submit_with_pre_flight_ledger()` already records `REJECTED`
  before the exception propagates) — but `submit_market_order()`/
  `submit_position_action()` treated every non-`DONE` retcode as an
  equally fatal rejection, so it propagated all the way to `main()`'s
  crash-alert boundary and halted the whole process, requiring a manual
  restart even though the fix (re-enabling AutoTrading) needs no restart
  at all once applied.
- Fixed: both methods now raise a new, narrower
  `BrokerTradingDisabledError` (`broker/mt5_gateway.py`, a
  `BrokerOrderRejectedError` subclass — existing catch sites unaffected)
  for retcodes `10026`/`10027` specifically. `main.py`'s bar-close loop
  (`_run_cycle_with_resilience()`, factored out of `_run_trading_loop()`
  to keep it under this project's `ruff` complexity limit) catches this
  narrower type, logs + sends an actionable Telegram alert ("enable the
  'Algo Trading' button"), and continues to the next bar close instead of
  halting — the very next cycle just works again on its own once a human
  flips the switch, no reconnect step needed (unlike the existing
  `BrokerConnectionError` guard, which does need to reconnect). Every
  other non-`DONE` retcode still raises the plain
  `BrokerOrderRejectedError` and still halts the process, unchanged.

### Changed — Short-Term (Scalp) Mode's SL/TP Now Capped at a Fraction of Current Equity

- Found live: the account is currently ~$7-9. Short-term mode's SL/TP
  (`SHORT_TERM_SL_ATR_MULTIPLIER`/`SHORT_TERM_TP_ATR_MULTIPLIER`, both
  `1.0` — i.e. 1x the H1 ATR(14)) risked **~$13-14 per 0.01-lot trade**
  at the real ATR observed live — 0.01 is the broker's own `volume_min`,
  already the smallest lot the bot can place, so there was no
  smaller-lot lever to fall back on. That risk already exceeds the
  entire account. A fixed ATR-multiplier tweak wouldn't have durably
  fixed this either: whatever multiple fit today's equity becomes wrong
  again the next time equity moves (it moved twice — $8.64 -> $7.39 ->
  $7.39-ish — just while investigating this).
- Fixed: `_evaluate_short_term_entry()` now caps both SL and TP distance
  at `min(ATR-based distance, SHORT_TERM_RISK_FRACTION_OF_EQUITY of
  current equity converted to a price distance)`, reusing the existing
  `risk.risk_manager.calculate_price_distance_for_target_profit()`
  (originally built for the currently-unwired fixed-$5-TP feature — same
  USD-to-price-distance conversion via the broker's real
  `tick_value`/`tick_size`, just applied to a *risk* cap now instead of a
  profit target). `SHORT_TERM_RISK_FRACTION_OF_EQUITY = 0.10` — a
  user-chosen risk-per-trade figure (offered 10/20/30%+; 10% chosen),
  not a spec default.
- Self-adjusting by design: on a small account the equity fraction is
  the binding (tighter) constraint, so risk-per-trade tracks the
  account's actual size instead of a fixed ATR multiple that can exceed
  the whole balance; once equity grows enough that the ATR-based
  distance becomes tighter than 10% of equity, the `min()` naturally
  hands control back to ATR alone — no separate regime-detection logic.
- Live-verified: at the real account equity/ATR observed while making
  this change, the new formula's risked-per-trade figure was confirmed
  by hand-computing the same formula against the live snapshot (see
  verification notes; not committed to a script since it's a one-off
  sanity check, not a repeatable research artifact like this session's
  other backtester work).
- Only affects `TRADING_MODE=SHORT_TERM`/`BOTH` — the account's actual
  running config is `WAIT_FOR_CONDITIONS`, so no live behavior changes
  until/unless that's switched.

### Added — ML Follow-Up to the `/condition` Signal (`backtester/ml_signal_model.py`) — Result: NOT PROMOTED

- Follow-up to the naive-heuristic finding just below: does *learning* how
  to weight the same 4 indicators (rather than a hand-picked -1/0/+1 vote)
  find a real edge? Built `backtester/ml_signal_model.py`: continuous
  feature transforms of the same MA/RSI/MACD/Bollinger Bands (normalized
  MA distance, RSI as-is, normalized MACD histogram, Bollinger %B — no new
  indicators), the same forward-return label and chronological 70%/30%
  IS/OOS split as `backtester/signal_validation.py` (factored its label
  logic out into a shared `build_forward_labels()` for a fair comparison),
  `TimeSeriesSplit`-selected logistic regression as the primary candidate
  (deployable live without a runtime `scikit-learn` dependency — see
  below) plus a `HistGradientBoostingClassifier` as a nonlinear comparison
  point. `scikit-learn==1.9.0` added to `pyproject.toml`'s `dev` extras
  (research-only; a new `sklearn.*` mypy override follows the existing
  `MetaTrader5`/`apscheduler.*` template).
- Ran against the real ~3.3-year XAUUSD H1 history (19,806 bars; 13,838
  train / 5,931 OOS). First pass looked promising — logistic regression's
  OOS accuracy (52.00%) beat both the majority-class baseline (51.98%,
  i.e. gold's own directional drift over this window) and the naive
  heuristic (49.74%) — **but its OOS ROC-AUC sat at 0.5007: zero real
  discrimination.** `TimeSeriesSplit` had selected maximum regularization
  (`C=0.001`) because no learnable in-sample signal made "mostly predict
  the majority class" the safest cross-validated choice — recall came out
  ~99.97%, confirming the model had collapsed to near-constant
  prediction. Accuracy alone couldn't tell "found a real edge" apart from
  "learned to mostly guess the more common direction" here; **this
  exposed a real gap in the original 3-gate promotion bar** (CI lower
  bound, beats majority baseline, beats naive heuristic — all technically
  passed), so a 4th gate (`OOS ROC-AUC > 0.53`) was added specifically to
  catch this failure mode before any live wiring decision, per
  `backtester/ml_signal_model.py`'s `evaluate_promotion_bar()`. Gradient
  boosting (nonlinear, not limited to a linear combination) fared no
  better (ROC-AUC 0.5122).
- Honest conclusion: **no real edge found**, same as the naive heuristic.
  `/condition`'s displayed signal (`summarize_indicator_signal()`) is
  unchanged; its disclaimer now also reports this ML attempt and result
  directly (`monitoring/telegram_bot.py`'s `ML_SIGNAL_OOS_ROC_AUC`
  constant), rather than silently trying and discarding it.

### Changed — `/condition`'s BUY/SELL Signal Now Shows Its Own Measured (Real) Accuracy Instead of an Unqualified Recommendation

- Empirically validated `summarize_indicator_signal()`'s equal-weight
  MA/RSI/MACD/Bollinger vote against real H1 XAUUSD history
  (~3.3 years) via a new vectorized harness (`backtester/signal_validation.py`),
  using the same in-sample (IS) / out-of-sample (OOS) chronological split
  discipline as Phase 2's WFO (design decision below): swept
  `min_abs_score` (1-4) x `horizon_bars` (2/4/8/12 bars) on the IS 70%,
  then confirmed the current default and the best-looking IS candidate
  against the untouched OOS 30%.
- Result: `min_abs_score` 3-4 essentially never fire (all four
  indicators rarely align that strongly at once in real data); every
  `min_abs_score` 1-2 x horizon combination clusters at **48-51%
  directional accuracy** on both IS and OOS — statistically
  indistinguishable from a coin flip (current default,
  `min_abs_score=1, horizon_bars=4`: 49.66% OOS, n=4,446; best IS
  candidate, `horizon_bars=12`: 50.72% OOS, n=4,438 — ~1.3 standard
  errors apart, not a real difference). No parameter within this simple
  additive-vote architecture shows a genuine predictive edge.
- Rather than tune a knob to manufacture an "improvement" that isn't
  real, left the heuristic's weights/threshold unchanged and instead
  added `MEASURED_SIGNAL_OOS_ACCURACY = 0.497` to
  `monitoring/telegram_bot.py`, displayed directly in `/condition`'s own
  output so any reader sees the real, measured number rather than an
  unqualified "BUY"/"SELL" label. This signal remains monitoring-only
  (`indicators/math_engine.py`'s module docstring) — it was never
  consulted by `main.py`'s actual trading decisions before this change
  either.

### Added — Phase 2: Anchored Walk-Forward Validation (`backtester/walk_forward.py`) — Result: NOT PROMOTED

- Implements `docs/adr/ADR-0004-anchored-walk-forward-validation.md` /
  `docs/RESEARCH.md` §§1-8 in full: a fixed anchor with a growing
  in-sample (IS) training window, an embargoed out-of-sample (OOS) test
  window per fold, a parameter sweep over `ADX_TREND_THRESHOLD` /
  `TRAILING_ATR_MULTIPLIER` scored by Deflated Sharpe Ratio (DSR;
  `analytics/performance.py`'s new `deflated_sharpe_ratio()`,
  `skewness()`, `kurtosis()`), and `docs/RESEARCH.md` §5's 5 promotion
  gates (DSR, IS/OOS efficiency, MAR floor, max-OOS-drawdown ceiling,
  minimum OOS trade count per fold and in total). Every fold's IS
  parameter search and OOS evaluation reuse Phase 1's `run_backtest()`
  unmodified — no second, parity-risking engine.
- Ran against the real ~3.3-year XAUUSD history (15 monthly folds, a
  reduced 35-combo grid: `ADX_TREND_THRESHOLD` in [20, 35] step 2.5,
  `TRAILING_ATR_MULTIPLIER` in [1.0, 3.0] step 0.5 — the full
  `docs/RESEARCH.md`-spec grid, ~7 hours, was out of scope for this
  session). Nearly every fold selected the grid's own lower-boundary
  values (`ADX=20.0, Trailing=1.0`) — a classic overfitting/
  boundary-chasing red flag, not a real optimum. Two further ad-hoc
  re-runs with the grid shifted progressively lower confirmed the same
  pattern persisted at every level tried, with no plateau or reversal —
  the metrics kept "improving" toward the new boundary each time instead
  of converging, exactly what boundary-chasing on noise looks like
  rather than a genuine signal.
- Final committed-grid result: DSR, IS/OOS efficiency, MAR, and
  max-drawdown gates all pass, but the trade-count gate fails (one fold
  had zero OOS trades, below the required 30/fold minimum) —
  `all_gates_passed=False`. Combined with the boundary-chasing pattern
  above, the honest conclusion is that **no parameter set tested in
  Phase 2 should be deployed**; the strategy's current parameters remain
  unvalidated by this methodology regardless of which grid is chosen.

### Added — Phase 1: Event-Driven Backtester and Performance Analytics (`backtester/`, `analytics/performance.py`)

- `backtester/simulator.py`'s `run_backtest()`: an event-driven
  simulator that reuses `main.py`'s actual live decision code
  (`run_bar_close_cycle()`, `_fetch_market_snapshot()`) unmodified, fed
  by `backtester/replay_gateway.py`'s `HistoricalReplayGateway` (a
  historical stand-in satisfying `main.py`'s new `MarketDataGateway`
  Protocol) instead of live MT5 — chosen specifically to avoid a second,
  parity-risking decision engine.
- `backtester/historical_data.py`'s `fetch_audited_history()` /
  `audit_bar_series()`: validates fetched history for gaps/duplicates/
  non-monotonic timestamps before any backtest run trusts it, tolerating
  ordinary weekend closures and short holiday gaps (up to 4 days) while
  still raising on anything larger or unexplained.
- `analytics/performance.py`: Sharpe/Sortino/CAGR/MAR/max-drawdown
  (+duration)/profit-factor/win-rate formulas and a `PerformanceReport`
  DTO (`docs/API_SPEC.md` §5's shape, adapted to plain `float`).
- Ran a single in-sample pass over the real ~3.3-year XAUUSD history
  (2023-03-20 to today): 361 trades, Sharpe 1.62, MAR 1.50 — but also
  surfaced a real, serious near-catastrophic loss (-$3,650 on
  2026-01-29, a violent flash-crash after a parabolic gold rally) that
  triggered the exact same `HARD_LOCK`-then-freeze bug pattern
  (`docs/ARCHITECTURE_SUMMARY.md` §5's `MANUAL_RESET_REQUIRED` gap)
  already encountered live earlier this session. This single in-sample
  pass is explicitly *necessary, not sufficient* — see Phase 2 above,
  which is the actual promotion methodology.

### Added — Monitoring-Only Technical Indicators (RSI/MACD/Bollinger Bands/MA) and a `/condition` Signal Summary

- `indicators/math_engine.py`: pure-numpy `rsi()`, `macd()`,
  `bollinger_bands()` (validated against independent pure-Python
  reference implementations), alongside the existing `sma()`. Explicitly
  monitoring/display-only — never consulted by `main.py`'s actual entry/
  exit decisions, by deliberate choice (kept the live strategy's decision
  surface unchanged rather than risk it on unvalidated new signals).
- `monitoring/telegram_bot.py`'s `/condition` now renders all four
  indicators plus an additive-vote BUY/SELL/HOLD summary
  (`summarize_indicator_signal()`) — see the empirical-accuracy entry
  above for why this summary now ships with a measured-accuracy
  disclaimer rather than being presented as an unqualified
  recommendation.

### Fixed — `partial_closed`/`breakeven_set` Never Actually Flipped to `True`, Even Within a Single Continuous Run

- The real, deeper root cause behind the 5.11-lot -> 0.01-lot cascade
  documented just below: persisting FSM state across restarts (that fix)
  only closed *half* the gap. `run_bar_close_cycle()`'s position-
  management branch called `evaluate_partial_close_and_breakeven()` and
  `calculate_trailing_stop()`, got back the right `OrderActionPayload`s
  for `main()` to submit, but then returned `updated_context` with
  **the exact same, unmodified `PositionState` it started with** —
  `_evaluate_drawdown_transition()`'s own docstring says as much
  ("`state`/`position` pass through unchanged"). So even with the
  restart-persistence fix in place, `partial_closed`/`breakeven_set`
  never became `True` in the very same process that just fired them —
  meaning `evaluate_partial_close_and_breakeven()` re-fired on *every
  single subsequent cycle* for as long as price stayed at or beyond
  Base_TP, repeatedly halving whatever volume remained, with no restart
  required at all to trigger it.
- Fixed: when `evaluate_partial_close_and_breakeven()` returns actions,
  `run_bar_close_cycle()` now derives an updated `PositionState`
  (`partial_closed=True`, `breakeven_set=True`, `stop_loss=entry_price`,
  `volume` reduced by the actual close volume) and returns it as part of
  `updated_context`. Symmetrically, when `calculate_trailing_stop()`
  fires, the returned context's `stop_loss` now advances to the new
  trailing level too — previously *that* never advanced either, so every
  later cycle compared a fresh candidate against a stale reference
  instead of the level actually just set at the broker (masked mostly by
  the earlier `NO_CHANGES` fix, but still logically wrong).
- New regression tests prove a second `run_bar_close_cycle()` call fed
  the first call's own returned context, at the same Base_TP-reached
  price, does *not* fire a second partial-close — the exact scenario
  that silently halved a live position roughly nine times over.

### Added — Trailing Stop Now Active From Entry, Not Just After Breakeven

- Live consequence: a real position swung from **+$17,000 of unrealized
  profit** (price came within ~7 points of Base_TP) all the way to an
  **-$8,000+ loss**, because between entry and Base_TP the stop-loss
  never moved at all — `calculate_trailing_stop()` returned `None`
  unconditionally whenever `breakeven_set` was `False`, so the only
  protection was the fixed initial 2x-ATR stop no matter how far price
  ran in favor first.
- Fixed: `calculate_trailing_stop()` is now active for the position's
  entire life. Before breakeven, it uses a new, wider
  `PRE_BREAKEVEN_TRAILING_ATR_MULTIPLIER` (`2.5`, vs. the existing
  post-breakeven `TRAILING_ATR_MULTIPLIER` `1.5`) — loose enough not to
  compete with Base_TP's own 2x-ATR target on ordinary fluctuations, but
  finite, so a large favorable excursion that reverses before Base_TP no
  longer gives back the entire gain with zero protection. The candidate
  stop still only ever tightens, exactly as before.
- Considered and deliberately not built: discrete staged partial-close
  tiers (e.g. lock 25% at 1x ATR, another 25% at 2x ATR). Rejected as
  more complexity than warranted for one live occurrence with no
  backtested evidence behind any specific tier levels, and more new
  per-position state to keep correctly persisted — exactly the class of
  bug the two fixes above just closed for the existing state.

### Fixed — Position-Management State Was Never Persisted, Re-Arming Partial-Close on Every Restart

- Live consequence, found the hard way: a real position partial-closed
  itself down from **5.11 lots to 0.01 lots** across a day of repeated
  restarts (crashes, `/killbot`, deliberate reverts), then crashed
  `main.py` outright on an MT5 `retcode=10013` (`INVALID`) trying to
  partial-close an already-minimum-sized position. Root cause:
  `PositionState.partial_closed`/`breakeven_set` aren't derivable from
  broker-reported fields, and `storage/state_manager.py`'s
  `save_fsm_state()`/`load_fsm_state()` existed (and were tested) but
  were **never actually called anywhere in `main.py`** — every restart
  mid-position re-seeded both flags to `False`
  (`_seed_initial_fsm_context()`), so `evaluate_partial_close_and_breakeven()`
  saw an already-partial-closed, already-breakeven position as if
  neither had ever happened, and closed another 50% of whatever
  remained.
- Fixed: `main.py`'s bar-close loop now calls `save_fsm_state()` at the
  end of every cycle (new pure `_fsm_context_to_dict()` serializes the
  full `FSMContext`, including `partial_closed`/`breakeven_set`).
  `_seed_initial_fsm_context()` now restores those two flags at boot via
  new `_persisted_position_flags()`, **ticket-matched** against the
  broker's currently-open position — a snapshot for a different
  (since-closed) position can never leak its flags onto a new one, and
  every other `PositionState` field (`ticket`/`volume`/`entry_price`/
  `stop_loss`/etc.) still comes exclusively from the broker's live
  report, never the snapshot. `last_sequence_id` is saved as a
  placeholder `0` — nothing reads it back yet (crash recovery here is
  single-snapshot, not full event-log replay, per
  `docs/ARCHITECTURE_SUMMARY.md` §5).
- The short-term mode's profit-peak lock (`short_term_peak_price`) has
  the same class of gap and is **not** fixed by this change — it's a
  plain loop-local variable, never part of `FSMContext`. Documented as a
  still-open, separate gap in `docs/ARCHITECTURE_SUMMARY.md` §5.

### Fixed — A Benign `NO_CHANGES` SL/TP-Modify Retcode Crashed the Whole Process

- Live crash: `BrokerOrderRejectedError("order_send failed for ticket
  1803973820: retcode=10025, last_error=(1, 'Success')")`. Retcode
  `10025` is MT5's `TRADE_RETCODE_NO_CHANGES` — returned when a
  `TRADE_ACTION_SLTP` modify request's SL/TP already equals what's
  currently set on the position. This isn't a real rejection (a stale
  local `FSMContext` recomputing the same trailing-stop level a previous
  cycle's modify already applied at the broker, or two cycles
  independently landing on the same tick-rounded value), but
  `submit_position_action()` treated every non-`DONE` retcode as fatal,
  so it raised `BrokerOrderRejectedError` — which, per this session's
  earlier resilience work, is *not* one of the errors the bar-close
  loop's reconnect guard absorbs (only `BrokerConnectionError` is), so
  it propagated all the way up and killed the process (with a Telegram
  crash alert, at least, rather than silently).
- Fixed: `submit_position_action()` now special-cases
  `TRADE_RETCODE_NO_CHANGES` for `TRADE_ACTION_SLTP` requests only —
  logs it and returns normally rather than raising. A
  `TRADE_ACTION_DEAL` (partial-close) request returning the same
  retcode is unaffected and still raises, since "no changes" has no
  sensible benign reading for a close order. Unlike the documented
  idempotency-gap posture (`docs/ARCHITECTURE_SUMMARY.md` §5: an
  ambiguous/genuinely-rejected order must never be blindly retried),
  there is nothing here a retry or a process halt would ever fix — the
  desired state is already in effect.

### Fixed — `TRADING_MODE=SHORT_TERM` Never Actually Disabled the Regular Strategy

- `run_bar_close_cycle()` evaluated `decide_entry_signal()` (the
  original D1+H4+H1-aligned WAIT_FOR_CONDITIONS strategy) and `main()`
  submitted its result unconditionally, regardless of `trading_mode` —
  despite the module docstring's claim that `trading_mode` "selects
  between" the two strategies. `_evaluate_short_term_entry()` already
  gated its own evaluation on `trading_mode`; the regular strategy had
  no equivalent gate at all. In practice this never caused a live
  regular-mode fill (0 real trades ever recorded under
  `STRATEGY_MAGIC_NUMBER` — its D1+H4+H1+ADX conditions are far
  stricter than short-term's relaxed H1-only ones, so the gap simply
  hadn't been hit yet), but it could fire at any moment while
  `TRADING_MODE=SHORT_TERM` was configured, contradicting the
  documented and expected behavior.
- Fixed: `run_bar_close_cycle()` now returns `entry_decision=None`
  immediately when `trading_mode == TRADING_MODE_SHORT_TERM` and the
  regular position is flat — mirroring `_evaluate_short_term_entry()`'s
  own gate. Only gates *new* entries: a regular position already open
  keeps being managed (trailing stop, partial close, breakeven)
  regardless of `trading_mode`, the same way short-term's own open
  position is always managed independent of `trading_mode` too.
  `WAIT_FOR_CONDITIONS` and `BOTH` are unaffected.

### Reverted — Short-Term Mode Back to the Original ATR-Based SL/TP

- The user chose to return to the original design: both dollar-target
  experiments (`SHORT_TERM_TP_TARGET_USD` — $5, then $1 — and
  `SHORT_TERM_SL_TARGET_USD` $1.50) are removed. `main.py` is back to
  `SHORT_TERM_TP_ATR_MULTIPLIER = 1.0` / `SHORT_TERM_SL_ATR_MULTIPLIER
  = 1.0` (risk:reward 1:1, both scaling with current volatility), and
  the profit-peak lock is back to its ATR-retracement trigger
  (`SHORT_TERM_PROFIT_LOCK_RETRACEMENT_ATR_MULTIPLIER = 0.5`;
  `decide_short_term_profit_lock()` takes `atr_value` again,
  `_evaluate_short_term_position_management()` takes `atr_value` instead
  of `SymbolConstraints`). No rule closes an order at any fixed dollar
  profit or loss anymore.
- `risk.risk_manager.calculate_price_distance_for_target_profit()` stays
  (pure, tested, no longer imported by `main.py`) — available if a
  dollar-target variant is ever wanted again.

### Added — Resilience + Push Alerts + Two §5 Gap Closures (news wiring, RR-012)

- **The bar-close loop now survives transient MT5 connection losses.**
  `main()` split into a thin crash-alert boundary plus
  `_run_trading_loop()`, whose per-cycle broker work (`run_one_cycle()`,
  a closure over the loop state) runs inside a `BrokerConnectionError`
  guard: a mid-cycle connection loss (terminal restart, network blip)
  logs, notifies, reconnects via `gateway.connect()`'s existing
  exponential backoff, and resumes on the next bar close instead of
  killing the process. Only *connection* errors are absorbed —
  `BrokerOrderRejectedError` still propagates and halts (the documented
  safe default: an ambiguous/rejected order must never be blindly
  retried, per `docs/ARCHITECTURE_SUMMARY.md` §5's idempotency-gap
  entry). An exception the guard can't absorb now triggers a final
  Telegram crash alert before re-raising unchanged — the process still
  halts fail-closed, it just no longer halts *silently*.
- **New `monitoring/notifier.py`: optional fire-and-forget Telegram push
  alerts.** The inverse of `monitoring/telegram_bot.py`'s pull model:
  `main.py`'s own process now pushes a message the moment something
  noteworthy happens — bot started, entry filled (regular and
  short-term), short-term close reconciled (with real profit),
  `HARD_LOCK` emergency liquidation, MT5 reconnect, crash.
  `build_notifier_from_env()` returns `None` when `TELEGRAM_*` isn't
  configured (optional add-on, never a boot requirement) and
  `TelegramNotifier.send()` swallows every exception after logging — a
  notification failure can never take down or delay the trading loop.
  Held on the container as `ApplicationContainer.notifier`.
- **Real news events now reach the live loop (§5 gap closed).** `main()`
  passed a hardcoded `[]` to `_fetch_market_snapshot()` despite
  `container.calendar_provider` being fully wired since Phase 11b — the
  NFP/CPI/FOMC blackout logic was implemented and tested but never
  received real events. New `_fetch_news_events()` fetches exactly the
  ±`MACRO_BLACKOUT_WINDOW` range `is_trade_entry_locked()` evaluates,
  every cycle, degrading open (log + `[]`) if the whole provider chain
  fails — the documented News-API-down posture is a degraded mode, not
  a hard halt (`apply_news_feed_fail_safe()`'s risk-halving side remains
  unwired, matching the standing §5 note).
- **`ENVIRONMENT_MODE`'s broker-side cross-check landed (RR-012, §5 gap
  closed).** New `MT5Gateway.get_account_trade_mode()` (maps
  `account_info().trade_mode` to `DEMO`/`CONTEST`/`REAL`);
  `ApplicationContainer.build()` now refuses to start (fail-closed
  `ConfigurationError`) when the configured `ENVIRONMENT_MODE` doesn't
  match the account class the broker actually reports — previously
  nothing prevented booting with `ENVIRONMENT_MODE=DEMO` while pointed
  at a live account, or vice versa. A `CONTEST` account matches neither
  mode and is always refused.

### Changed — Short-Term Mode's Stop-Loss Converted to a Fixed Dollar Target + Profit-Lock Re-Based Off the TP

- `main.py`'s `SHORT_TERM_SL_ATR_MULTIPLIER` (ATR-based SL) replaced with
  `SHORT_TERM_SL_TARGET_USD = 1.5` — the short-term (scalp) mode's
  stop-loss is now a fixed dollar target, converted to a price distance
  via `risk.risk_manager.calculate_price_distance_for_target_profit()`
  the same way the take-profit already was. Previously the SL scaled
  with ATR while the TP stayed fixed at $1 — in live conditions this
  meant SL routinely risked $12-16 against the $1 target (a ~90%+ win
  rate needed to break even), while the real observed win rate was only
  ~60-66%. A $1.50 SL against the $1 TP needs a much more achievable
  ~60% breakeven win rate, matching what's actually being observed.
- `main.py`'s `SHORT_TERM_PROFIT_LOCK_RETRACEMENT_ATR_MULTIPLIER`
  (ATR-relative retracement trigger) replaced with
  `SHORT_TERM_PROFIT_LOCK_RETRACEMENT_FRACTION_OF_TP = 0.5` — the
  profit-peak lock's trigger is now a fraction of the TP's own price
  distance instead of ATR. An ATR-relative threshold could end up
  larger *or* smaller than the TP's distance depending on current
  volatility, letting the lock fire before a position ever had a real
  chance to reach TP — observed live, several positions closed at
  $0.47-0.97, short of the full $1 target, purely because ATR happened
  to be small that cycle. `decide_short_term_profit_lock()`'s signature
  changed from `atr_value: float` to `tp_distance: float`;
  `_evaluate_short_term_position_management()`'s from `atr_value: float`
  to `constraints: SymbolConstraints` (computes `tp_distance` internally
  from the position's own volume).
- Both changes replace the two remaining ATR-driven parameters of
  short-term mode with dollar-driven ones, so the mode's entire
  risk/reward shape now stays constant regardless of current volatility
  — previously only the TP was dollar-fixed, so the ratio silently
  drifted with whatever ATR happened to be at entry.

### Changed — Short-Term Mode's Take-Profit Lowered to $1

- `main.py`'s `SHORT_TERM_TP_TARGET_USD` changed from `5.0` to `1.0` —
  the short-term (scalp) mode's fixed dollar-profit take-profit now
  closes at $1.00 of profit instead of $5.00, converted to a price
  distance the same way as before via
  `risk.risk_manager.calculate_price_distance_for_target_profit()`.
  Real ledger data (22 closed trades: 12 wins totaling +$49.22, 10
  losses totaling -$131.95, profit factor ≈0.37) showed losses running
  far larger than wins — a smaller, faster-hit profit target is a
  direct response, taking gains sooner rather than giving trades more
  room to reverse into a loss. The stop-loss and profit-peak lock at the
  time were unchanged (both later replaced — see above).

### Added — Telegram `/checkhisorder` Recent Order History

- `monitoring/telegram_bot.py` gained `/checkhisorder`: reports the 7
  most recently opened `trade_ledger` rows (both still-`OPEN` and
  `CLOSED`), most recent first — side, volume, symbol, status, profit
  (once closed), and both opened/closed timestamps. Never shows the
  internal `client_order_id`.
- New `StateManager.get_recent_trades(limit=7)` (read-only): the `limit`
  most recent `trade_ledger` rows ordered by the table's own
  `AUTOINCREMENT` id (not `opened_at_utc`, a stored string with no
  collision guarantee).
- New pure `format_order_history_message(trades)` renders the reply;
  `/checkhisorder`, like `/check`, is read-only and records nothing to
  the Audit Trail (unlike `/killbot`).

### Fixed — `/check` Could Misreport a Killed Process as Still Running

- After `/killbot` terminates `main.py`, its last heartbeat can still be
  well within `HEARTBEAT_STALE_AFTER` (20 minutes) — `/check` would keep
  reporting "✅ กำลังทำงาน" for up to that long after the process was
  actually confirmed gone.
- `format_status_message()` gained a `process_alive: bool | None`
  param, sourced from a new `_is_main_process_alive()` (the same PID-file
  + `Get-CimInstance` check `_kill_main_process()` already verifies
  against before killing, reused here read-only). `process_alive is
  False` now reports "🛑 หยุดทำงาน (main.py ถูกปิดไปแล้ว)" immediately,
  taking priority over heartbeat freshness; `None` (non-Windows, or no
  PID file ever written) falls back to heartbeat staleness alone, as
  before.

### Added — Telegram `/killbot` Emergency Process Termination

- `monitoring/telegram_bot.py` gained `/killbot`: terminates `main.py`'s
  OS process outright — an emergency stop for when the operator wants
  the whole process gone. Windows-only (`taskkill`, matching this
  project's Windows-deployed environment) and not reversible from
  Telegram: once killed, restarting requires manually running
  `python main.py` again on the host.
- `main.py` now writes its own PID to `storage/main.pid`
  (`storage/db_engine.py`'s new `MAIN_PID_PATH`) once at boot — the only
  way a separate process (`monitoring/telegram_bot.py`) can find it to
  terminate. Overwritten fresh on every restart.
- Before killing, `/killbot` re-queries the OS for the PID's actual
  command line (`Get-CimInstance Win32_Process`) and confirms it still
  contains `main.py` — guards against a stale/reused PID (main.py has no
  graceful-shutdown path today, so `main.pid` can outlive the process
  that wrote it) — refusing to act rather than risk terminating an
  unrelated process that happened to reuse the same PID.
- Recorded to the Audit Trail (`AuditActionType.MANUAL_OVERRIDE`,
  `parameter_name="main_process_killed"`) — the same mechanism any other
  state-altering administrative command uses; `actor` is the Telegram
  chat_id, hashed before storage by the existing `record_audit_event()`.
  This required `monitoring/telegram_bot.py` to become a writer:
  `StateManager()` is opened normally (no longer `read_only=True`), its
  only write being this Audit Trail entry.

### Added — Telegram `/check` Status Bot

- New `monitoring/` package (carved out the same way `resilience/` was —
  a concern that doesn't belong to any single existing package):
  `monitoring/telegram_bot.py` long-polls Telegram's `getUpdates` and
  replies to a `/check` message from an allowed chat with whether
  `main.py`'s bar-close loop is alive and which `TRADING_MODE` it's
  running under. Runs as its own standalone process — never imported by
  `main.py`/`container.py` — and opens its `StateManager` with
  `read_only=True` (ADR-0003's single-writer principle: `main.py` stays
  the sole writer), so it can be started, stopped, or restarted
  independently with zero risk of contending for or corrupting the
  ledger. Built on `requests` (already a project dependency) against
  Telegram's plain HTTPS Bot API — no `python-telegram-bot` package
  added.
- New single-row `bot_heartbeat` table (`storage/db_engine.py`, same
  pinned-singleton pattern as `system_state`) +
  `StateManager.record_heartbeat(trading_mode)`/`get_heartbeat()`.
  `main.py`'s bar-close loop calls `record_heartbeat()` once per
  iteration — including its weekend-market-closed skip branch, so a
  closed weekend market is never mistaken for a stopped process.
  `format_status_message()` (pure) treats a heartbeat older than
  `HEARTBEAT_STALE_AFTER` (20 minutes, made-up-but-documented — larger
  than both cadences the loop ever heartbeats at) as "not running".
- `StateManager.__init__` gained a `read_only: bool = False` param,
  threaded to `storage/db_engine.py`'s existing `connect(read_only=...)`
  support; schema initialization/migrations (DDL writes) are skipped in
  read-only mode.
- New `config/telegram_config.py` (`TelegramConfig.from_env()`) owns the
  optional `TELEGRAM_BOT_TOKEN`/`TELEGRAM_ALLOWED_CHAT_ID` environment
  variables — `main.py`'s trading loop never depends on them; only the
  separate monitoring process does.

### Fixed — `get_closing_deal()` Queried Broker Deal History Using the Host Clock

- Even after the previous fix wired `MT5Gateway.reconcile_short_term_closes()`
  into boot, three fresh short-term `trade_ledger` rows stayed stuck `OPEN`
  despite their positions having closed hours earlier on the broker side —
  the boot-time reconciliation and the mid-loop detection in
  `_fetch_short_term_position()` both silently failed to find the closing
  deal every single time.
- Root cause: `get_closing_deal()` built its `mt5.history_deals_get()` query
  window from `datetime.now(timezone.utc)` — the **host** clock — but
  `deal.time` is stamped in **broker server** wall-clock terms (same
  convention as `tick.time`, per `_resolve_broker_utc_offset()`). This demo
  account's broker server runs ~3 hours ahead of host UTC, so the window's
  upper bound (`host_now + 5 minutes`) landed ~3 hours before where the
  actual closing deals were stamped, and the lookup returned `None` for
  every recent close. This is exactly the class of bug `broker_utc_offset`
  (ADR-0002) exists to prevent — `get_closing_deal()` was simply the one
  call site that never applied it, unlike `MT5ClockProvider.get_server_time()`.
- Fix: `get_closing_deal()` now builds its query window from
  `datetime.now(timezone.utc) + self.broker_utc_offset`, the same
  adjustment `MT5ClockProvider.get_server_time()` already applies.
- New regression test (`test_get_closing_deal_query_window_is_broker_clock_not_host_clock`)
  asserts the query window sits at broker-clock "now", not host-clock
  "now", under a 3-hour synthetic offset — large enough that a host-UTC
  regression can't pass by coincidence.
- Verified live: reran `reconcile_short_term_closes()` against the real IC
  Markets demo account after the fix — it immediately found all three
  stuck positions' real closing deals (previously invisible), and the
  three stale `OPEN` rows were reconciled to `CLOSED` with their real
  outcomes.

### Changed — Short-Term Mode's Take-Profit Is a Fixed Dollar Target

- `risk/risk_manager.py` — new `calculate_price_distance_for_target_profit()`
  (pure, broker-agnostic like its siblings): converts a target dollar
  profit into the price distance that realizes it at a given lot size,
  using the broker's own `tick_value`/`tick_size`
  (`broker.mt5_gateway.SymbolSpec`) — a fixed price distance means a
  different dollar amount at 0.01 lots than at 0.1, so the conversion has
  to go through the broker's real contract spec, not a fixed constant.
- `main.py` — the short-term mode's take-profit is `SHORT_TERM_TP_TARGET_USD`
  ($5.00, a made-up-but-documented default) converted via the above,
  replacing the previous 1x-ATR-based TP. The stop-loss is unchanged
  (still 1x ATR) and the profit-peak lock still applies independently —
  whichever of TP/SL/profit-lock fires first closes the position.
  `SymbolConstraints` gained `tick_value`/`tick_size` fields (sourced
  from `container.gateway.symbol_spec`) to carry this through.
- Verified live against the real IC Markets demo account: reverse-checking
  the computed price distance against the real `tick_value`/`tick_size`
  reproduces exactly $5.00 of profit at the fixed minimum lot size.

### Fixed — Short-Term Closes Missed While the Process Was Stopped

- `main.py`'s `_fetch_short_term_position()` only detects a short-term
  position's close by comparing broker state cycle-to-cycle — a close
  that happens while the process is stopped (as opposed to mid-loop) was
  never caught once the process restarted, leaving a stale `OPEN`
  `trade_ledger` row forever (found via two real historical closes on
  the demo account that should have reconciled but hadn't). Unlike the
  regular position, which already gets this exact catch-up for free from
  the existing Disaster Recovery reconciliation
  (`audit_open_positions()`/`resolve_position_audit()`, both boot-time),
  the short-term mode had no boot-time equivalent.
- `broker/mt5_gateway.py`'s new `MT5Gateway.reconcile_short_term_closes()`
  closes this: at boot, `container.py` now checks every still-`OPEN`
  short-term-magic ledger row against the broker's real open positions,
  and for any that closed while the process was down, looks up the real
  outcome via the existing `get_closing_deal()` and records it `CLOSED`
  — the same reconciliation `_fetch_short_term_position()` already does
  mid-loop, just also run once at boot. Does not affect
  `initial_drawdown_state` (the short-term position is stateless, never
  tracked in `FSMContext` — this is ledger bookkeeping for
  `optimizer/self_learning.py`'s analytics, not a safety gate).
- Verified live: two stale `OPEN` rows on the real IC Markets demo
  account (from real SL-hit closes that happened while the process
  wasn't running) were manually reconciled using the same logic this fix
  now runs automatically at every boot.

### Added — Self-Learning Optimizer Wired Live + Short-Term Profit-Peak Lock

- **The weekend self-learning optimizer now actually runs.**
  `optimizer/self_learning.py`'s `create_weekend_optimizer_scheduler()`
  was fully built and tested (Phase 8) but never started anywhere —
  `optimizer/README.md`'s own "Depended On By" section flagged this exact
  wiring as missing. `container.py`'s `ApplicationContainer.build()` now
  constructs the weekly job (`_build_weekly_optimization_job()`) and
  starts it (new `optimizer_scheduler: BackgroundScheduler` field).
- **Its shifts now actually change live behavior.** Previously,
  `decide_parameter_shift()`'s output was only ever recorded to
  `parameter_history` — nothing read it back, so a Saturday shift was a
  no-op forever after. New `StateManager.get_latest_parameter_value()`
  (read) + `optimizer.self_learning.get_effective_parameter_value()` (the
  shared "latest shift, or the hardcoded default" resolution point) close
  this: `strategy/trend_filter.py`'s `evaluate_master_trend()` gained an
  `adx_trend_threshold` override param (mirroring
  `execution/position_manager.py`'s existing `calculate_trailing_stop()`
  pattern), and `main.py` resolves both `ADX_TREND_THRESHOLD` and
  `TRAILING_ATR_MULTIPLIER` fresh every cycle — a Saturday shift takes
  effect the very next cycle, no restart needed.
- **Short-term trades now feed the optimizer real data.** Previously
  documented as a deliberate gap (no mechanism existed to detect an
  MT5-side SL/TP close), short-term entries are now recorded to
  `trade_ledger` as `OPEN` (`broker_ticket` set), and
  `broker/mt5_gateway.py`'s new `get_closing_deal()` (queries MT5 deal
  history by `position_id`/`DEAL_ENTRY_OUT`) plus
  `StateManager.get_open_trade_by_ticket()` let `main.py` reconcile the
  real close outcome once `_fetch_short_term_position()` detects the
  position is gone — turning the same row `CLOSED` with the real
  `close_price`/`profit`. Short-term trades are no longer invisible to
  `optimizer/self_learning.py`'s analytics.
- **New: a profit-peak trailing lock, short-term mode only.** Tracks the
  best favorable price reached while a short-term position is open
  (`main.py`'s `_update_short_term_peak_price()`); if price retraces from
  that peak by `SHORT_TERM_PROFIT_LOCK_RETRACEMENT_ATR_MULTIPLIER` (0.5x
  ATR, made-up-but-documented) while still in profit
  (`decide_short_term_profit_lock()`), closes immediately rather than
  riding it back down to the fixed SL or waiting for the fixed TP.
  Independent of, and orthogonal to, the regular position's own
  management — uses a distinct `SHORT_TERM_PROFIT_LOCK_COMMENT` (not
  `EMERGENCY_LIQUIDATION_COMMENT`) so it never touches the regular
  position's `FSMContext`.
  `execution/position_manager.py`'s `build_short_term_liquidation_action()`
  gained an optional `comment` override to support this (default
  preserves the existing `HARD_LOCK` call site unchanged).

### Flagged

- If a `HARD_LOCK` freezes (rather than liquidates) a short-term position
  that's mid-profit-peak-tracking, the tracked peak resets to `None`
  while frozen (`run_bar_close_cycle()`'s `blocks_position_management`
  gate returns before the profit-lock ever runs). If a human later clears
  `MANUAL_RESET_REQUIRED` with that same position still open, peak
  tracking restarts fresh from the current price rather than resuming the
  true historical peak — the same class of limitation already documented
  for the regular position's partial-close/breakeven state after a crash
  restart.

### Verified

- `ruff check .` / `ruff format --check .` / `mypy .` — clean across all
  43 source files.
- `pytest --cov=. --cov-fail-under=90` — 342 tests pass (32 new),
  96.03% total coverage; `container.py` at 100%.
- Verified live against the real IC Markets demo account: the scheduler
  starts with the exact intended `CronTrigger(day_of_week='sat',
  hour='3')`; `get_effective_parameter_value()` correctly falls back to
  the hardcoded default with an empty `parameter_history`;
  `get_closing_deal()` correctly reproduced the real `close_price`/
  `profit` for three actual historical short-term closes on this
  account (two TP hits, one SL hit, one from earlier today) by
  `position_id`, confirming the deal-lookup logic against real MT5 data,
  not just the fake test double.

### Fixed — Order Comments Clamped to the MT5 Wrapper's Real Length Limit

- `broker/mt5_gateway.py` — every order-request builder now clamps the
  `comment` field to `MAX_ORDER_COMMENT_LENGTH` (25). The MetaTrader5
  Python wrapper rejects long comments outright (`order_send()` returns
  `None`, `last_error` `(-2, 'Invalid "comment" argument')`) before
  anything reaches the broker, and `main.py` passes a UUIDv4
  `client_order_id` (36 chars) as the comment on every real entry — so
  the first-ever live entry (a short-term one) crashed the process, and
  the regular entry path carried the same latent bug (it had simply
  never fired live before). The real limit was empirically bisected
  against `MetaTrader5==5.0.4500` on a live demo connection: 29 chars
  accepted, 30+ rejected — NOT the 31 MT5's own docs suggest — hence 25
  with margin. The full client_order_id is still recorded in the Event
  Store; the broker-side comment is informational only. Verified live:
  a full-UUID-comment order opened (SL/TP intact, short-term magic) and
  closed cleanly.

### Added — Short-Term (Scalp) Trading Mode

- New `TRADING_MODE` env var (`WAIT_FOR_CONDITIONS`/`SHORT_TERM`/`BOTH`,
  `config/config_manager.py`, mirrors `ENVIRONMENT_MODE`'s validation
  pattern) selects between the original D1+H4+H1-aligned strategy and a
  second, independent short-term entry mode intended to trade more often
  for smaller, quicker gains. A new required `SHORT_TERM_MAGIC_NUMBER`
  (validated distinct from `STRATEGY_MAGIC_NUMBER`) tags its orders so the
  two modes' positions never collide.
- `strategy/trend_filter.py` — `ShortTermTrendAlignment`/
  `evaluate_short_term_trend()`: H1 direction alone (no D1/H4 alignment
  requirement) plus a lower ADX bar (`SHORT_TERM_ADX_THRESHOLD = 15.0` vs.
  the regular `25.0`) — deliberately relaxed to fire more often. Fully
  independent of `TrendAlignment`/`evaluate_master_trend()`, which are
  untouched.
- `main.py` — `decide_short_term_entry_signal()` (shares
  `decide_entry_signal()`'s breakout/pullback/wick-fill trigger-selection
  tail, extracted into `_select_trigger_signal()`), fixed minimum lot size
  (`constraints.volume_min`, not equity-compounded), and a fixed 1:1
  ATR-based TP/SL (`SHORT_TERM_TP_ATR_MULTIPLIER`/
  `SHORT_TERM_SL_ATR_MULTIPLIER = 1.0`) that MT5 closes automatically — so,
  unlike the regular position, a short-term position is never tracked in
  `FSMContext` (stateless; `main()` re-queries it from the broker every
  cycle via the distinct magic number) and needs no per-cycle
  trailing/partial-close management. `run_bar_close_cycle()`'s new
  `trading_mode`/`short_term_position` parameters both default to values
  that reproduce the exact prior behavior, so every existing test and
  call site is unaffected. A fresh `HARD_LOCK` liquidates (or freezes,
  symmetrically) both positions together when both are open.
- `broker/mt5_gateway.py` — `get_open_positions_by_magic()` and
  `submit_market_order()` both gained an optional `magic_number`
  parameter (default: the gateway's own), so the short-term mode's orders
  can be queried/submitted under its own distinct magic number through
  the same shared MT5 connection, without a second `MT5Gateway` instance.
- `execution/position_manager.py` — `build_short_term_liquidation_action()`,
  a sibling of `build_emergency_liquidation_action()` taking plain scalar
  fields instead of a `PositionState` (short-term positions are never
  tracked as one), avoiding a circular import with `broker/mt5_gateway.py`
  (which already imports `OrderActionPayload` from this module).

### Flagged

- Short-term fills are **not** written to `trade_ledger` (unlike the
  regular entry path) — `audit_open_positions()`/Disaster Recovery
  reconciliation only ever covers the regular magic number, and nothing
  in this codebase learns when MT5 closes a position via SL/TP outside
  `main()`'s own submission path; a ledger row would sit `OPEN` forever
  with no way to ever mark it `CLOSED`. The audit trail
  (`submit_with_pre_flight_ledger`'s REQUESTED/SENT/FILLED events) is
  still recorded. Consequence: short-term trades are invisible to
  `optimizer/self_learning.py`'s analytics. A real close-tracking
  mechanism for this mode is unscheduled.

### Verified

- `ruff check .`, `mypy .` — clean across all 43 source files.
- `pytest -q` — 309 tests pass (30 new: config validation, gateway
  magic-number overrides, `evaluate_short_term_trend()`,
  `decide_short_term_entry_signal()`, `build_short_term_liquidation_action()`,
  and `TestBarCloseCycleShortTermMode`'s mode/collision/HARD_LOCK cases).
- Verified live against the real IC Markets demo account: with the
  regular strategy's D1+H4+H1 trend mismatched (no entry), the short-term
  path independently found H1 ADX (19.3) above its lower 15.0 threshold
  and proposed a real BUY entry on a pullback trigger — confirming the
  relaxed condition fires exactly as intended where the original
  wouldn't.

### Fixed — Closed-Bar Signal Evaluation

- `broker/mt5_gateway.py` — `get_bars()` now fetches from MT5 position 1
  instead of 0: position 0 is the currently *forming* bar, so every
  signal consumer (the 2-candle breakout `docs/RESEARCH.md` §8.1 defines
  "on the latest two closed bars", the wick-fill ratios, ATR, each
  timeframe's EMA-vs-close trend check) was silently evaluating a
  partially-formed bar as if it were final — the documented strategy was
  never actually the one running. New `get_current_price()` (latest tick
  bid — bid, not ask/mid, because MT5 bars are bid-built, keeping the
  live price on the same basis as every bar-derived indicator) supplies
  the live price that `h1_bars.close[-1]` previously leaked from the
  forming bar.
- `main.py` — `_fetch_market_snapshot()`'s `current_price` now comes from
  `gateway.get_current_price()`, since the latest *closed* H1 bar's close
  can be up to an hour old — stale for entry-stop/trailing math.
- `tests/conftest.py` — `FakeMT5.copy_rates_from_pos` now honors the
  `start` argument with real MT5 semantics (position 0 = the last seeded
  element, i.e. the forming bar) instead of ignoring it, so the
  closed-bars contract is actually pinned by tests.

### Fixed — Equity Baseline Rollover

- `risk/drawdown_fsm.py` — `seed_equity_baselines()`/
  `roll_equity_baselines()` and the new `BaselineEpoch` dataclass:
  daily/weekly/monthly `EquityBaselines` tiers now roll forward
  independently the first bar-close cycle whose broker-server "now"
  crosses that tier's UTC-day/ISO-week/calendar-month boundary, instead of
  being seeded once at process start and never refreshed
  (`docs/ARCHITECTURE_SUMMARY.md` §5). `BaselineEpoch` is kept separate
  from `EquityBaselines` so `classify_drawdown_event()` and every existing
  caller keep dealing with exactly three equity floats. Both functions
  are pure and reject naive datetimes.
- `main.py` — the bar-close loop now calls `container.clock_provider
  .get_server_time()` every cycle (broker server time, not the host
  machine clock) to seed/roll `EquityBaselines`, closing part of the
  previously-flagged "`clock_provider` built but never consumed" gap —
  the weekend-closure check and bar-close-wait cadence still read the
  host clock directly, a narrower remaining gap.

### Verified

- `pytest tests/unit tests/integration` — all tests pass, including 8 new
  `TestEquityBaselineRollover` cases covering same-period no-op, each
  tier's isolated boundary crossing, and the naive-datetime error path.

## [1.0.0-RC2] - 2026-07-05

### Added — Weekly Market Closure Gate

- `broker/mt5_gateway.py` — `is_weekend_market_closed(now_utc)`: `True`
  during the weekly forex/CFD market closure (Friday 22:00 UTC through
  Sunday 22:00 UTC — a common broker convention, not a universal,
  per-broker-verified fact; flagged the same way as this project's other
  invented-but-documented numeric defaults). A separate axis from the
  existing `is_within_execution_window()` (that one is a daily 07:00-22:00
  GMT filter, repeating every day; this one is the once-a-week closure).
- `main.py` — the bar-close loop now checks `is_weekend_market_closed()`
  first and skips the cycle entirely (no MT5 calls at all) during
  closure, rechecking every `WEEKEND_RECHECK_SECONDS` (900s/15min) instead
  of every 5-minute bar close, since nothing changes for hours during a
  weekend closure. Logs the skip at `INFO` level so it's visible in
  operator-facing logs rather than silent.
- `container.py` — two new boot-time `INFO` log lines (connection
  success with server/symbol/magic, and container-build completion with
  environment mode/drawdown state) so a successful boot is observable
  without needing to infer it from the *absence* of a warning/critical
  log line.

### Flagged

- `is_within_execution_window()` (the daily GMT-hour filter) remains
  unwired into `main.py`'s live loop — this fix only addresses the
  weekly closure, a distinct gap tracked separately in
  `docs/ARCHITECTURE_SUMMARY.md` §5.

### Verified

- `ruff check .`, `ruff format --check .`, `mypy --strict .` — all pass.
- `pytest --cov=. --cov-fail-under=90` — 270 tests pass, 96.48% total
  coverage. `is_weekend_market_closed()` fully covered, including both
  boundary conditions for Friday/Sunday and the naive-datetime error path.
- Verified live against a real IC Markets demo account
  (`ICMarketsSC-Demo`, `XAUUSD`) — confirmed successful connect, clean
  Disaster Recovery reconciliation, and multiple healthy bar-close cycles
  end-to-end via the new boot/loop log lines.

## [1.0.0-RC1] - 2026-07-05

### Added — Phase 11e: Bifurcated Resiliency, Audit Trail, Disaster Recovery & Test Suite Reorganization

- `resilience/backoff.py` (new package) — the canonical exponential
  backoff + retry budget for network I/O (§7): `compute_backoff_delays()`
  (pure, defaults to the spec's exact `2s, 4s, 8s, 16s, 32s`) and
  `retry_with_backoff()` (retries an operation up to `max_attempts`
  additional times — a "retry budget" — sleeping the backoff delay
  between each; `sleep` is injectable for deterministic tests; raises
  `RetryBudgetExhaustedError`, chained, once exhausted). Applied to
  `news/calendar_provider.py`'s `NetworkCalendarProvider`, deliberately
  overridden to a small 1-retry budget so Phase 11b's fast
  provider-failover guarantee isn't undermined by a ~62s worst-case
  stall; `RetryBudgetExhaustedError` is caught and re-raised as
  `NewsFeedConnectionError` so `CalendarProviderChain`'s existing
  fallback contract still applies. `broker/mt5_gateway.py`'s
  `MT5Gateway.connect()` (its own independently-tuned, already-tested
  backoff since Phase 3) was deliberately *not* refactored onto this
  module — see Flagged.
- `storage/db_engine.py` — `connect()` now sets `PRAGMA busy_timeout`
  (default 5000ms, overridable via `busy_timeout_ms=`): SQLite's native
  wait-on-lock-contention mechanism, so a transient writer/reader lock
  resolves without any Python-level sleep-and-retry loop. Every write
  already used `with connection:` (atomic, immediate rollback on any
  exception) since Phase 2.
- `storage/migrations.py` — migration version 2: the `audit_trail` table,
  made **structurally** append-only by `trg_audit_trail_no_update`/
  `_no_delete` triggers (the same pattern Phase 11c's `order_events`
  established).
- `storage/state_manager.py` — `AuditActionType` (convenience constants
  for the spec's 3 illustrative categories — not a closed, DB-enforced
  set, unlike `OrderLifecycleState`), `AuditEvent`, `record_audit_event()`
  (SHA-256-hashes the caller-supplied `actor` before storage — the
  spec's literal "actor hashes" — and stringifies `old_value`/`new_value`
  for the delta columns), `get_audit_trail()`.
- `broker/mt5_gateway.py` — `DisasterRecoveryPlan`/`resolve_position_audit()`:
  turns a `PositionAuditReport` into a concrete settlement plan.
  Broker-only positions become new reconciled `trade_ledger` rows
  (`f"disaster-recovery-{ticket}"` `client_order_id`, idempotent across
  repeated runs); ledger-only entries are marked `CLOSED_RECONCILED` at
  the reconciliation moment. Any divergence sets
  `requires_manual_review=True`, honoring `docs/RUNBOOK.md`'s
  pre-existing `HIGH`-severity policy for a position-audit mismatch
  (RR-008) instead of silently auto-resuming.
- `container.py` — `ApplicationContainer.build()` now applies
  `resolve_position_audit()`'s plan (upserting the settled ledger rows),
  records a `DISASTER_RECOVERY_RECONCILIATION` Audit Trail entry on any
  divergence, and computes a new `initial_drawdown_state` field
  (`MANUAL_RESET_REQUIRED` on divergence, `ACTIVE` otherwise).
- `main.py` — `_seed_initial_fsm_context()`: seeds `main()`'s starting
  `FSMContext` from the broker's *live* open positions
  (`get_open_positions_by_magic()`) rather than a possibly-stale local
  snapshot, using `container.initial_drawdown_state` for the drawdown
  axis. `partial_closed`/`breakeven_set` default to `False` when resuming
  mid-position (not derivable from broker-reported fields alone — a
  known limitation, flagged).
- Test suite reorganized (§7's explicit instruction): `tests/test_unit.py`
  → `tests/unit/test_unit.py`; `tests/test_integration.py` split into
  `tests/integration/test_integration.py` (boundary-crossing tests that
  aren't fault simulations) and `tests/chaos/test_chaos.py` (MT5 server
  dropouts, socket/HTTP disconnections, an abrupt process crash);
  `tests/stress/` added as an honest, currently-empty placeholder (see
  its own README). `pyproject.toml`'s `testpaths` now scopes the default
  `pytest`/CI invocation to `tests/unit` + `tests/integration` only;
  `tests/chaos`/`tests/stress` run via an explicit separate invocation
  and are `omit`-excluded from the coverage report so an unexecuted file
  never drags down the standard gate.
- `.github/workflows/ci.yml` — the `docs-consistency` job's version regex
  now accepts an optional SemVer pre-release suffix (`-RC1`, etc.) — a
  plain `\d+\.\d+\.\d+` pattern would silently skip a suffixed header and
  match an older entry instead of failing loudly.
- `VERSION` / `CHANGELOG.md` → `1.0.0-RC1`; `pyproject.toml`'s
  `[project] version` → `1.0.0rc1` (PEP 440's canonical pre-release
  form — no hyphen, lowercase `rc` — a Python-packaging requirement, not
  a style choice; distinct from `VERSION`'s SemVer-style `-RC1`).

### Flagged

- SLO Metrics (§7's fourth bullet — a background daemon thread tracking
  `trade_latency`/`spread`/`order_reject_rate`/`mt5_latency`/`retry_count`/
  `heartbeat_failures`) were **not built this sub-phase**. The phase
  directive's explicit 4-item instruction list substituted the test-suite
  reorganization for this bullet. Tracked as its own row
  (`docs/TRACEABILITY_MATRIX.md` RQ-034, `SPECIFIED`, not `IMPLEMENTED`)
  rather than silently folded into an "implemented" row.
- The duplicate-order retry loop (flagged since Phase 11c) still cannot
  be safely built: `broker/mt5_gateway.py`'s `submit_market_order()`/
  `submit_position_action()` fold "MT5 explicitly rejected this" and
  "MT5 returned nothing (ambiguous — possibly a transient timeout)" into
  the same `BrokerOrderRejectedError`. Wrapping either call in
  `retry_with_backoff()` today would retry indiscriminately, including
  genuine, permanent rejections a retry can never fix.
- `resilience.backoff`'s `max_attempts` counts retries *after* the
  initial attempt (so the default of 5 permits up to 6 total attempts) —
  a "5 total tries" reading of the spec would leave the last listed delay
  (32s) always unused.
- Test-suite reorganization measurably lowered two files' *standard-pipeline*
  coverage: `news/news_engine.py` (97%→77%) and `broker/mt5_gateway.py`
  (95%→93%), both because their only HTTP/reconnect-fault tests moved to
  `tests/chaos/`. The overall `--cov-fail-under=90` gate still passes
  (95%+ total); both files are fully covered again by `pytest tests/chaos`.
  Flagged so this isn't mistaken for a regression later.

### Verified

- `ruff check .` and `ruff format --check .` — all checks passed.
- `mypy --strict .` — no issues found.
- `pytest --cov=. --cov-report=term-missing --cov-fail-under=90` (now
  scoped by `pyproject.toml`'s `testpaths` to `tests/unit` + `tests/integration`)
  — **265 tests pass, 96.65% total coverage**. `resilience/backoff.py`
  reached 100% coverage; `storage/state_manager.py`'s new Audit Trail
  methods and `broker/mt5_gateway.py`'s new `resolve_position_audit()`
  are both fully covered. `pytest tests/chaos tests/stress` separately:
  **11 more tests pass** (0 in `tests/stress/`, an intentional gap).
  Combined (`pytest tests/unit tests/integration tests/chaos tests/stress`):
  **276 tests pass**.

## [0.15.0] - 2026-07-05

### Added — Phase 11d: Pure-Function FSM Drawdown Breaker & Feature Flags

- `risk/drawdown_fsm.py` — the single, centralized pure-function drawdown
  FSM (§6): `DrawdownState` (`ACTIVE`/`WARNING`/`SOFT_LOCK`/`HARD_LOCK`/
  `MANUAL_RESET_REQUIRED`, the 5 states named verbatim), `DrawdownEvent`,
  `classify_drawdown_event()` (the sole numeric-to-symbolic boundary —
  daily/weekly/monthly drawdown against `EquityBaselines`, classified into
  the worst severity), and `transition_drawdown_state()` (the exact
  `FSM(current_state, event) -> new_state` signature the spec requires).
  `ACTIVE`/`WARNING`/`SOFT_LOCK` are recoverable (re-evaluated fresh every
  cycle); only `HARD_LOCK` is sticky, always advancing to
  `MANUAL_RESET_REQUIRED`, cleared solely by an explicit
  `DrawdownEvent.MANUAL_RESET_CONFIRMED`. `blocks_new_entries()`/
  `blocks_position_management()` are the two predicates `main.py` gates
  its entry/position-management branches on — `SOFT_LOCK` freezes new
  entries only, explicitly leaving trailing-stop/breakeven/partial-close
  running. `decide_hard_lock_response()` is the `FeatureFlagManager`-driven
  decision the instant `HARD_LOCK` is freshly entered.
- `config/feature_flags.py` — `FeatureFlags`/`FeatureFlagManager`:
  `FLAG_LIQUIDATE_ON_HARD_LOCK` (defaulting to `false` — freeze, the
  safer choice), the config flag `docs/PRODUCTION_SPEC.md` §6 names as
  `config.flags.liquidate_on_hard_lock`.
- `execution/position_manager.py` — `build_emergency_liquidation_action()`:
  a full-volume `TRADE_ACTION_DEAL` close, the payload
  `liquidate_on_hard_lock=True` triggers. No new broker-side method was
  needed — `submit_position_action()` already translates any
  `TRADE_ACTION_DEAL` into a real close.
- `main.py` — `run_bar_close_cycle()` now transitions through the
  drawdown FSM every cycle (via the new `_evaluate_drawdown_transition()`/
  `_handle_hard_lock_response()` helpers, factored out to stay under this
  project's `ruff`-enforced cyclomatic-complexity limit) and gates its
  entry/position-management branches on the resulting predicates. A fresh
  `HARD_LOCK` with `liquidate_on_hard_lock=True` and an open position emits
  `build_emergency_liquidation_action()`'s payload through the existing
  `submit_with_pre_flight_ledger()` wrapper (Phase 11c); `main()` clears
  `FSMContext.position` back to `None` on a confirmed liquidation, since
  unlike a partial close it leaves nothing open. `TradingState.HALTED` is
  removed — `FSMContext` now carries `drawdown_state`/`drawdown_reason` as
  a fully orthogonal axis from `state`/`position` (a position can be
  `IN_POSITION` while simultaneously `SOFT_LOCK`ed).
- `container.py` — `ApplicationContainer` gains a `feature_flags`
  (`FeatureFlagManager`) field, constructed via `FeatureFlags.from_env()`.
- `.env.template` — documents the new optional `FLAG_LIQUIDATE_ON_HARD_LOCK`.

### Flagged

- `HARD_LOCK` thresholds (10%/20%/40%, double each `SOFT_LOCK` tier) and
  the `WARNING` ratio (60% of the nearest `SOFT_LOCK` limit) are new,
  made-up-but-documented defaults — the spec names the 5 states but gives
  no catastrophic-tier percentage. `SOFT_LOCK`'s 5%/10%/20% is unchanged
  from Phase 10's original hard locks (RQ-022).
- `ACTIVE`/`WARNING`/`SOFT_LOCK` recovering automatically as equity
  improves, with only `HARD_LOCK` requiring a human to clear, is a
  deliberate evolution beyond Phase 10's "once halted, never auto-resumes
  at any severity" posture — flagged in case every tier was intended to
  stay sticky like the old single `HALTED` state.
- No live control channel exists for a human to actually send
  `MANUAL_RESET_CONFIRMED` — `run_bar_close_cycle()`'s
  `manual_reset_confirmed` parameter is fully wired and tested, but
  `main()` always passes `False`; there is no API/CLI/admin signal for an
  operator to set it against a running process yet.
- Equity-baseline rollover (carried over from RQ-022, Phase 10) is still
  not implemented — the "daily"/"weekly"/"monthly" framing still degrades
  the longer the process runs past its first UTC day, applying identically
  to the new `WARNING`/`SOFT_LOCK`/`HARD_LOCK` tiers.

### Verified

- `ruff check .` and `ruff format --check .` — all checks passed.
  `run_bar_close_cycle()` initially exceeded this project's
  `max-complexity = 10` mccabe limit; factored into
  `_evaluate_drawdown_transition()`/`_handle_hard_lock_response()` helpers
  to bring it back under the limit.
- `mypy --strict .` — no issues found.
- `pytest --cov=. --cov-fail-under=90` — **251 tests pass, 97.14% total
  coverage**. `risk/drawdown_fsm.py`, `config/feature_flags.py`, and
  `execution/position_manager.py`'s new function all reached 100%
  coverage, including every FSM transition-table branch, both
  `liquidate_on_hard_lock` values, `SOFT_LOCK` continuing position
  management while blocking new entries, `HARD_LOCK`'s freeze/liquidate
  split, `MANUAL_RESET_REQUIRED`'s stickiness, and
  `MANUAL_RESET_CONFIRMED` clearing the lock.

## [0.14.0] - 2026-07-05

### Added — Phase 11c: Pre-Flight Idempotency & Event-Sourced Order Ledger

- `storage/migrations.py` — a lightweight internal schema-migration
  framework (§4's "track db status securely" requirement):
  `apply_pending_migrations()`/`get_applied_migrations()` track applied
  versions in a `schema_migrations` table and apply any pending
  `Migration` in ascending order. Seeded with one migration: the
  `order_events`/`order_ledger` DDL, plus two SQLite triggers
  (`trg_order_events_no_update`/`_no_delete`) making `order_events`
  **structurally** append-only (`sqlite3.IntegrityError` at the database
  engine level, not just Python-side convention). Phase 2/3/8's original
  tables remain outside this framework — see `storage/README.md`'s
  Simplification note for why.
- `storage/state_manager.py` — `OrderLifecycleState` (the 11 institutional
  lifecycle states §5 names verbatim: `REQUESTED`/`VALIDATED`/`SENT`/
  `PENDING`/`PARTIALLY_FILLED`/`FILLED`/`MODIFIED`/`CANCELLED`/`REJECTED`/
  `EXPIRED`/`CLOSED`), `OrderEvent` (one immutable Event Store row), and
  `record_order_event()`/`get_order_ledger_state()`/`get_order_events()`/
  `get_latest_order_event()`. `record_order_event()` appends to
  `order_events` and folds the new state into the `order_ledger`
  projection atomically — the same transaction — satisfying §4's literal
  "atomic transaction block" requirement. `StateManager.__init__` now also
  calls `apply_pending_migrations()`.
- `broker/mt5_gateway.py` — `MT5Gateway.is_ticket_still_open(ticket)`: the
  "query the server cache" half of §4's pre-retry audit.
- `execution/validation.py` — `SeverityLevel` (`INFO`/`WARNING`/`ERROR`/
  `CRITICAL`) and `ValidationResult` (`is_valid`/`reason_code`/`severity`/
  `is_retryable`/`metadata`), §5's exact rich validator payload (`metadata`
  typed `dict[str, Any]` rather than the spec's literal bare `dict`, since
  `mypy --strict`'s `disallow-any-generics` forbids the latter).
  `check_duplicate_order_before_retry()` — the `PreTradeValidator`
  pipeline's concrete duplicate-order gate (RR-007): reduces the local
  Event Store's latest recorded state and whether the broker still
  confirms a previously-recorded ticket open into a `ValidationResult`.
  Ambiguous post-`SENT` states with no confirmed-open ticket are refused
  (not assumed safe) — the conservative reading of "strictly mitigating
  duplicate order anomalies".
- `main.py` — `submit_with_pre_flight_ledger()` wraps both real
  broker-submission call sites (a new market order; every position
  action) with the pre-flight `REQUESTED` write, then `SENT` + a terminal
  `FILLED`/`MODIFIED` event on success or `REJECTED` (re-raised unchanged)
  on failure. This is the one part of this phase that changes `main.py`'s
  actual runtime behavior — additively; the happy path and rejection path
  are otherwise unchanged.

### Flagged

- `order_ledger.timestamp` uses the spec's literal column name rather
  than this project's usual `_utc`-suffixed convention, since §4's SQL
  example names it verbatim — only the column name deviates, not its
  format (still a UTC ISO8601 string).
- The retry-audit gate (`check_duplicate_order_before_retry()` +
  `is_ticket_still_open()`) is fully built and tested but **not wired
  into an actual retry loop** — `main.py` has no automated
  order-submission retry mechanism today; a single
  `BrokerOrderRejectedError` still propagates uncaught. That retry loop's
  backoff cadence is `docs/PRODUCTION_SPEC.md` §7's explicit domain (Max 5
  attempts: 2s/4s/8s/16s/32s), a later sub-phase.
- Unlike Phase 11b's precedent (build the abstraction, leave `main.py`
  untouched), this phase does wire the pre-flight ledger write directly
  into `main.py`'s live loop — justified because the two real submission
  call sites, and the `client_order_id` generation itself, already existed
  there; wrapping them is a small, bounded, behavior-preserving addition,
  unlike inventing a new retry loop would be.

### Verified

- `ruff check .` and `ruff format --check .` — all checks passed.
- `mypy --strict .` — no issues found. Two lambdas passed to
  `submit_with_pre_flight_ledger()` in `main.py` were rewritten as
  annotated nested `def`s (`mypy` cannot infer a lambda's parameter types
  against a `TypeVar`-generic `Callable` parameter); `ruff`'s B023 (loop
  variable not bound in a nested function) was resolved the same way,
  binding narrowed locals as default-parameter values.
- `pytest --cov=. --cov-fail-under=90` — **193 tests pass, 96.95% total
  coverage**. `storage/migrations.py` and `execution/validation.py`
  reached 100% coverage, including the append-only triggers' rejection of
  a raw `UPDATE`/`DELETE`, the CHECK constraint rejecting an invalid
  lifecycle state, migration idempotency on a re-run, and every branch of
  `check_duplicate_order_before_retry()`'s decision table.
  `submit_with_pre_flight_ledger()` is exercised directly against a real
  temp-file `StateManager` with a fake `submit` callable (no MT5 needed),
  covering both its success and `BrokerOrderRejectedError` paths.

## [0.13.0] - 2026-07-05

### Added — Phase 11b: Dynamic Calendar Feed Priority & Normalized Clock Abstraction

- `news/calendar_provider.py` — `CalendarProvider` (§2): a `Protocol`
  (`name` + `fetch_events(from_utc, to_utc)`) unifying every calendar
  source behind one interface. `NetworkCalendarProvider` wraps
  `news_engine.fetch_calendar_events()` for HTTP-backed providers;
  `OfflineSnapshotCalendarProvider` reads a local JSON snapshot file
  (`news/offline_calendar_snapshot.json`, ships as an empty `[]`) as the
  network-independent final fallback. `RateLimiter` is a non-blocking
  sliding-window limiter (`allow()` refuses rather than sleeps once
  `max_calls_per_minute` is exhausted in the trailing 60 seconds — this
  system's bar-close loop runs under a 200ms processing cap and must
  never block on a rate limit). `CalendarProviderChain.fetch_events()`
  tries each configured provider in priority order, falling through to
  the next on any `NewsFeedConnectionError` or exhausted rate limit, and
  only raising once every provider has failed.
- `config/calendar_config.py` — `CalendarConfig.from_env()` loads the new
  optional `CALENDAR_*` environment variables (`CALENDAR_PROVIDER_PRIORITY`,
  `CALENDAR_TIMEOUT_MS`, `CALENDAR_RATE_LIMIT_PER_MIN`,
  `CALENDAR_<PROVIDER>_BASE_URL`, `CALENDAR_OFFLINE_SNAPSHOT_PATH`).
  Defaults to a safe `offline_snapshot`-only chain requiring no additional
  configuration; raises `ConfigurationError` — the same fail-closed
  exception `config_manager.py` raises — if an unknown provider name is
  listed or a network provider is listed without its base URL configured.
  No vendor base URL is ever hardcoded or guessed (`news/README.md`'s
  provenance note: no real `tradingeconomics`/`finnhub` API contract has
  been verified in this codebase).
- `broker/clock_provider.py` — `ClockProvider` (§3): a `Protocol`
  (`get_server_time(symbol) -> AwareDatetime`). `MT5ClockProvider` derives
  server time strictly from the connected `MT5Gateway`'s own
  `broker_utc_offset` (ADR-0002) — never a hardcoded DST table or the
  host machine's local clock.
- `news/news_engine.py` — `_parse_event()` renamed to public
  `parse_calendar_event()`, since `calendar_provider.py`'s offline
  snapshot fallback now parses the same event shape from a local file.
- `container.py` — `ApplicationContainer` gains `calendar_provider`
  (`CalendarProviderChain`) and `clock_provider` (`MT5ClockProvider`)
  fields, constructed in `build()` via
  `news.calendar_provider.build_calendar_provider_chain()` and
  `MT5ClockProvider(gateway=gateway)`.
- `.env.template` — documents the new optional `CALENDAR_*` variables.

### Flagged

- Default `CALENDAR_PROVIDER_PRIORITY` is `("offline_snapshot",)` alone,
  not the spec's illustrative `['tradingeconomics', 'finnhub',
  'offline_snapshot']` — see `docs/ARCHITECTURE_SUMMARY.md` §3 for why an
  unconditional 3-provider default was rejected (it would require every
  deployment to configure two unverified vendor base URLs just to boot).
  The full chain remains fully supported, opt-in via
  `CALENDAR_PROVIDER_PRIORITY`.
- `CalendarConfig.timeout_ms` is applied to both the HTTP connect and read
  phase of a network provider's request — the spec gives one unified
  timeout, while `fetch_calendar_events()` (Phase 7) takes a separate
  connect/read pair.
- `main.py`'s live loop does not yet consume either new provider —
  `_fetch_market_snapshot()` still passes a hardcoded empty event list and
  `run_bar_close_cycle()`/the bar-close sleep still call
  `datetime.now(timezone.utc)` directly. Both are held by
  `ApplicationContainer`, ready to be wired in; see
  `docs/ARCHITECTURE_SUMMARY.md` §5.

### Verified

- `ruff check .` and `ruff format --check .` — all checks passed (32 files).
- `mypy --strict .` — no issues found in 32 source files. `CalendarProvider`'s
  `name` had to be declared as a read-only `@property` rather than a plain
  `name: str` attribute — mypy's Protocol structural-typing rules require a
  settable attribute for the latter, which the frozen dataclass providers
  (`NetworkCalendarProvider`, `OfflineSnapshotCalendarProvider`) don't have.
- `pytest --cov=. --cov-fail-under=90` — **168 tests pass, 96.97% total
  coverage**. `config/calendar_config.py`, `broker/clock_provider.py`, and
  `news/calendar_provider.py` all reached 100% coverage, including the
  rate limiter's sliding-window expiry, the provider chain's fallback and
  full-exhaustion paths, the offline snapshot's missing-file/malformed-JSON
  errors, and `ApplicationContainer.build()`'s default (`offline_snapshot`-
  only) and misconfigured-network-provider paths exercised against a real
  `FakeMT5` + temp SQLite database.

## [0.12.0] - 2026-07-05

### Added — Phase 11a: Secrets Hardening & Dependency-Injection Composition Root

- `docs/PRODUCTION_SPEC.md` — new production-hardening engineering
  contracts (§1 secrets/boot validation, §2 calendar feed priority, §3
  clock abstraction, §4 pre-flight idempotency, §5 event sourcing, §6
  FSM drawdown breaker, §7 resiliency/SLO/disaster recovery), being
  implemented as gated Phase 11 sub-phases rather than one combined pass
  — see the note at the top of `docs/ARCHITECTURE_SUMMARY.md`.
- `config/config_manager.py` — `ConfigValidator` (§1): `check_presence()`,
  `check_no_placeholder_leak()` (curated substring detection for
  un-replaced template values like `CHANGEME`/`your_`/`REPLACE_ME`),
  `check_environment_mode()`, `check_integer()`. `ConfigManager.load()`
  now delegates to it; any check failure's `ConfigurationError` is the
  "fatal application panic" §1 requires.
- `config/secret_redaction.py` — `SecretRedactingFilter`, a `logging.Filter`
  that replaces configured secret values with a fixed marker in every log
  record before any handler sees it (§1's "exclude sensitive values from
  structured logging" requirement).
- `container.py` — `ApplicationContainer`, a constructor-based dependency-
  injection composition root ("Core Orchestration Directive" #1):
  `ApplicationContainer.build()` loads config, attaches the secret-redaction
  filter to the root logger, opens storage, connects the broker (with
  backoff), and reconciles broker-reported positions against the ledger —
  replacing the `RuntimeHandles`/`bootstrap_system()` logic that
  previously lived inline in `main.py`.
- `main.py` — refactored to construct an `ApplicationContainer` instead of
  calling the now-removed `bootstrap_system()`; no behavior change.

### Verified

- `ruff check .` and `ruff format --check .` — all checks passed (29 files).
- `mypy --strict .` — no issues found in 29 source files (mypy 2.1.0
  locally, per the Phase 4 toolchain note).
- `pytest --cov=. --cov-fail-under=90` — **147 tests pass, 96.50% total
  coverage**. `config/config_manager.py`, `config/secret_redaction.py`,
  and `container.py` all reached 100% coverage, including the
  placeholder-leak detection (password, API key, and case-insensitivity
  cases), the redaction filter's overlapping-substring and blank-secret
  edge cases, and `ApplicationContainer.build()`'s divergence-warning
  branch (a broker-only position with no matching ledger row) exercised
  against a real `FakeMT5` + temp SQLite database.

## [0.11.0] - 2026-07-05

### Added — Phase 10: Final Integration & Main FSM Orchestration Loop

- `main.py` — the master FSM orchestration loop:
  - `run_bar_close_cycle()` — pure decision function (no I/O): checks the
    200ms processing cap (logs, doesn't halt), the 5%/10%/20%
    daily/weekly/monthly drawdown hard locks (halts new entries, never
    auto-resumes), the news blackout, and either proposes a sized new
    entry or evaluates partial-close/breakeven/trailing-stop actions for
    an existing position.
  - `decide_entry_signal()` — combines the master trend filter with the
    three independent entry triggers (breakout/pullback/wick-fill) into
    one `BUY`/`SELL`/`NONE` decision, requiring trend confirmation, no
    news lock, and at least one trigger agreeing with the trend direction.
  - `seconds_until_next_bar_close()`, `evaluate_processing_time()`,
    `check_drawdown_breach()` — the individually-testable pieces above.
  - `bootstrap_system()`/`main()` — the impure I/O layer: boot sequence
    (config → storage → broker connect + position audit), then loop
    forever, acting once per M5 bar close. Reviewed but **not executed**
    in this environment — no live MT5 credentials exist here, and
    connecting to even a demo account requires the user's real-time
    presence, not an autonomous turn (see `docs/ARCHITECTURE_SUMMARY.md`
    §7).
- `broker/mt5_gateway.py` additions needed to make the loop real:
  `AccountState`/`get_account_state()` (the drawdown checks' input),
  `BarSeries`/`get_bars()` (D1/H4/H1 bar fetching, with `TIMEFRAME_*`
  constants re-exported so `main.py` never imports `MetaTrader5` itself),
  and `submit_market_order()` (opens new positions — does **not**
  implement the pre-trade risk gate or slippage guard, RQ-009/RQ-010,
  still an open gap).
- `docs/ARCHITECTURE_SUMMARY.md` — capstone document consolidating every
  phase's flagged design choices, the full list of known gaps (no risk
  gate, no backtester, equity-baseline rollover not wired, news feed not
  connected in the live loop, no `ENVIRONMENT_MODE` broker-side
  cross-check), and a concrete checklist before a first live/demo run.
- `tests/test_unit.py`/`tests/test_integration.py` extended with 34 new
  tests covering `main.py`'s pure logic and the three new broker methods.

### Fixed — a real bug caught in this phase's own wiring

`_fetch_market_snapshot()`'s first draft passed `h1_bars.close` as both
the price array and the pullback trigger's `reference_level` — since
`detect_pullback()`'s bullish condition requires `level < close` and both
were the same array, this comparison (`close[-1] < close[-1]`) could never
be `True`, silently disabling the pullback signal in live operation
(breakout/wick-fill would still work). Caught by re-reading the wiring
against `strategy/trend_filter.py`'s own logic — no test exercised this,
since it's in the untested impure I/O layer. Fixed by computing the H1
EMA(40) directly (`indicators.math_engine.ema`) and passing that as the
reference level, matching what `trend_filter` itself evaluates alignment
against. Documented in `docs/ARCHITECTURE_SUMMARY.md` §4 as a cautionary
note about that layer's test coverage.

### Verified

- `ruff check .` and `ruff format --check .` — all checks passed (27 files).
- `mypy --strict .` — no issues found in 27 source files (mypy 2.1.0
  locally, per the Phase 4 toolchain note).
- `pytest --cov=. --cov-report=term-missing --cov-fail-under=90` — **131
  tests pass, 95.68% total coverage**. `main.py` itself sits at 78%
  coverage — the uncovered lines are exactly `bootstrap_system()`,
  `_fetch_market_snapshot()`, and `main()`'s loop body, i.e. precisely the
  impure I/O layer intentionally not executed in this environment; every
  pure decision-logic branch in `run_bar_close_cycle()` and
  `decide_entry_signal()` is covered, including the drawdown-halt
  transition, the halted-stays-halted invariant, both the partial-close
  and trailing-stop position-management branches, and the
  processing-cap-breach log line.

## [0.10.0] - 2026-07-05

### Added — Phase 9: Automated Unit Testing & Integration Harness

- `tests/conftest.py` — shared fixtures: `FakeMT5` (drop-in
  `MetaTrader5` module substitute, with `FakeSymbolInfo`/`FakeTick`/
  `FakePosition`/`FakeOrderResult`) and a `tmp_path`-backed `StateManager`
  fixture.
- `tests/test_unit.py` — 66 tests covering `config/`, `storage/db_engine.py`,
  `indicators/math_engine.py` (every function cross-checked against an
  independent pure-Python reference implementation — the same technique
  that caught the Phase 4 ADX bug, now permanently regression-tested),
  `strategy/trend_filter.py`, `strategy/execution_triggers.py`
  ("M5 candle flags"), `risk/risk_manager.py` ("lot math metrics"),
  `execution/position_manager.py`, and the pure-logic portions of
  `broker/mt5_gateway.py`, `news/news_engine.py`, and
  `optimizer/self_learning.py`.
- `tests/test_integration.py` — 18 tests covering exactly the four
  scenarios the phase directive named: MT5 server dropouts (backoff +
  recovery, backoff exhaustion, full order-action request-building and
  rejection paths), socket disconnections (`news_engine.py`'s
  `fetch_calendar_events()` against a faked `requests.get` — connection
  error, timeout, HTTP error, invalid JSON), database rollbacks (a real
  SQLite constraint violation forced mid-transaction, proving the whole
  transaction rolls back, not just the failing statement), and data state
  validation processes (crash recovery, WAL/integrity checks, broker/ledger
  reconciliation, and the optimizer's isolation guarantee verified against
  a real SQLite database).
- `tests/__init__.py` — added to resolve a `mypy --strict` module-resolution
  ambiguity once test files began importing `tests.conftest` by dotted
  path (the same fix applied to `storage/`, `broker/`, etc. in earlier
  phases).
- `pyproject.toml` — added `[tool.coverage.run]`/`[tool.coverage.report]`
  scoping coverage measurement to the nine source packages.

### Verified

- `ruff check .` and `ruff format --check .` — all checks passed (26 files).
- `mypy --strict .` — no issues found in 26 source files (mypy 2.1.0
  locally, per the Phase 4 toolchain note).
- `pytest --cov=. --cov-report=term-missing --cov-fail-under=90` (the
  exact `.github/workflows/ci.yml` invocation) — **97 tests pass, 97.27%
  total coverage**, every one of the nine source modules individually
  above 90% (`storage/db_engine.py` reached 100% after two small
  additions: a read-only-connection test and a `checkpoint_wal()`
  smoke test).

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
