"""Pure-function FSM drawdown breaker (`docs/PRODUCTION_SPEC.md` §6): the
single, centralized place equity-drawdown-driven state transitions are
decided. No other module branches on drawdown severity independently —
`main.py`'s `run_bar_close_cycle()` only calls into this module and
applies the resulting predicates (`blocks_new_entries()`/
`blocks_position_management()`) to gate its own entry/position-management
branches, mirroring how it already consumes `strategy/`'s pure functions
without that being "dispersed" strategy logic.

Supersedes Phase 10's single-tier `TradingState.HALTED` with a graduated
5-state machine (`docs/CHANGELOG.md` §0.10.0's drawdown hard locks were the
predecessor). `ACTIVE`/`WARNING`/`SOFT_LOCK` are all recoverable — each
bar-close cycle re-classifies the *current* drawdown fresh, so a state can
step back down as equity recovers. Only a `HARD_LOCK` breach is sticky
(`MANUAL_RESET_REQUIRED`), requiring an explicit human
`MANUAL_RESET_CONFIRMED` event to clear. This is a deliberate evolution
beyond Phase 10's "never auto-resumes at any severity" posture — flagged
in `docs/ARCHITECTURE_SUMMARY.md` §3.

`seed_equity_baselines()`/`roll_equity_baselines()` (below) supersede the
former "seed once at process boot, never touch again" behavior
(`docs/ARCHITECTURE_SUMMARY.md` §5's previously-flagged gap): `main()` now
rolls each tier forward independently the first bar-close cycle that
crosses its UTC-day/ISO-week/calendar-month boundary, sourcing "now" from
`ClockProvider` (broker server time) rather than the host machine clock.
`BaselineEpoch` — the day/week/month each tier was last seeded for — is
kept out of `EquityBaselines` itself so `classify_drawdown_event()` and
every existing caller keep dealing with exactly three equity floats.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum

# SOFT_LOCK thresholds: unchanged from Phase 10's original 5%/10%/20%
# daily/weekly/monthly hard locks (RQ-022).
DAILY_SOFT_LOCK_LIMIT = 0.05
WEEKLY_SOFT_LOCK_LIMIT = 0.10
MONTHLY_SOFT_LOCK_LIMIT = 0.20

# HARD_LOCK thresholds: double each SOFT_LOCK limit. No catastrophic-tier
# numeric threshold was specified anywhere in the phase directive; this is
# a made-up-but-documented default (same pattern as risk/risk_manager.py's
# compounding tiers), flagged for review.
DAILY_HARD_LOCK_LIMIT = 0.10
WEEKLY_HARD_LOCK_LIMIT = 0.20
MONTHLY_HARD_LOCK_LIMIT = 0.40

# WARNING fires once drawdown reaches this fraction of the *nearest*
# SOFT_LOCK limit on any tier — an early-attention signal, not a
# functional restriction. Also a made-up-but-documented default.
WARNING_RATIO_OF_SOFT_LOCK = 0.6


class DrawdownState(str, Enum):
    """The 5 operational states `docs/PRODUCTION_SPEC.md` §6 names
    verbatim."""

    ACTIVE = "ACTIVE"
    WARNING = "WARNING"
    SOFT_LOCK = "SOFT_LOCK"
    HARD_LOCK = "HARD_LOCK"
    MANUAL_RESET_REQUIRED = "MANUAL_RESET_REQUIRED"


class DrawdownEvent(str, Enum):
    """The sole inputs to `transition_drawdown_state()`: either a
    classified equity-drawdown severity (`classify_drawdown_event()`) or
    an explicit external control event (a human clearing a lock)."""

    WITHIN_TOLERANCE = "WITHIN_TOLERANCE"
    WARNING_THRESHOLD_BREACHED = "WARNING_THRESHOLD_BREACHED"
    SOFT_LOCK_THRESHOLD_BREACHED = "SOFT_LOCK_THRESHOLD_BREACHED"
    HARD_LOCK_THRESHOLD_BREACHED = "HARD_LOCK_THRESHOLD_BREACHED"
    MANUAL_RESET_CONFIRMED = "MANUAL_RESET_CONFIRMED"


@dataclass(frozen=True, slots=True)
class EquityBaselines:
    """Reference equity captured at the start of the current UTC
    day/ISO week/calendar month, against which drawdown is measured."""

    daily_start_equity: float
    weekly_start_equity: float
    monthly_start_equity: float


@dataclass(frozen=True, slots=True)
class BaselineEpoch:
    """The UTC calendar day/ISO week/month each `EquityBaselines` tier was
    last seeded for — the sole bookkeeping `roll_equity_baselines()` needs
    to decide whether a tier's period boundary has been crossed since."""

    daily_date: date
    weekly_iso_year_week: tuple[int, int]
    monthly_year_month: tuple[int, int]


def _current_epoch(now_utc: datetime) -> BaselineEpoch:
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    aware = now_utc.astimezone(timezone.utc)
    iso_year, iso_week, _ = aware.isocalendar()
    return BaselineEpoch(
        daily_date=aware.date(),
        weekly_iso_year_week=(iso_year, iso_week),
        monthly_year_month=(aware.year, aware.month),
    )


def seed_equity_baselines(
    current_equity: float, now_utc: datetime
) -> tuple[EquityBaselines, BaselineEpoch]:
    """Construct a fresh `EquityBaselines` with all three tiers set to
    `current_equity`, stamped with the UTC day/ISO week/month `now_utc`
    falls in. `main()`'s boot-time seed, called exactly once on its very
    first bar-close cycle.
    """
    baselines = EquityBaselines(
        daily_start_equity=current_equity,
        weekly_start_equity=current_equity,
        monthly_start_equity=current_equity,
    )
    return baselines, _current_epoch(now_utc)


def roll_equity_baselines(
    baselines: EquityBaselines,
    epoch: BaselineEpoch,
    current_equity: float,
    now_utc: datetime,
) -> tuple[EquityBaselines, BaselineEpoch]:
    """Roll over whichever tier(s) of `baselines` have crossed their period
    boundary as of `now_utc` (broker server time — `main()` sources this
    from `ClockProvider`, never the host machine clock), resetting the
    rolled tier(s) to `current_equity`. A tier whose period hasn't changed
    since `epoch` passes through unchanged — this is a per-cycle idempotent
    check, not a per-cycle reset, so calling it every bar-close cycle is
    safe and required.
    """
    new_epoch = _current_epoch(now_utc)

    daily_equity = (
        current_equity if new_epoch.daily_date != epoch.daily_date else baselines.daily_start_equity
    )
    weekly_equity = (
        current_equity
        if new_epoch.weekly_iso_year_week != epoch.weekly_iso_year_week
        else baselines.weekly_start_equity
    )
    monthly_equity = (
        current_equity
        if new_epoch.monthly_year_month != epoch.monthly_year_month
        else baselines.monthly_start_equity
    )

    return (
        EquityBaselines(
            daily_start_equity=daily_equity,
            weekly_start_equity=weekly_equity,
            monthly_start_equity=monthly_equity,
        ),
        new_epoch,
    )


@dataclass(frozen=True, slots=True)
class DrawdownClassification:
    """The full computed picture behind one `classify_drawdown_event()`
    call. `event` is what actually drives the FSM; the raw percentages are
    retained for logging/observability only."""

    daily_drawdown_pct: float
    weekly_drawdown_pct: float
    monthly_drawdown_pct: float
    event: DrawdownEvent


def classify_drawdown_event(
    current_equity: float, baselines: EquityBaselines
) -> DrawdownClassification:
    """Compute daily/weekly/monthly drawdown against `baselines` and
    classify the *worst* (most severe) tier into a single `DrawdownEvent`
    — the sole numeric-to-symbolic boundary in this module; every
    transition decision downstream operates on the symbolic event alone.
    """
    for label, baseline in (
        ("daily", baselines.daily_start_equity),
        ("weekly", baselines.weekly_start_equity),
        ("monthly", baselines.monthly_start_equity),
    ):
        if baseline <= 0:
            raise ValueError(f"{label}_start_equity must be > 0, got {baseline}")
    if current_equity < 0:
        raise ValueError(f"current_equity must be >= 0, got {current_equity}")

    daily_dd = max(
        0.0, (baselines.daily_start_equity - current_equity) / baselines.daily_start_equity
    )
    weekly_dd = max(
        0.0, (baselines.weekly_start_equity - current_equity) / baselines.weekly_start_equity
    )
    monthly_dd = max(
        0.0, (baselines.monthly_start_equity - current_equity) / baselines.monthly_start_equity
    )

    hard_breach = (
        daily_dd >= DAILY_HARD_LOCK_LIMIT
        or weekly_dd >= WEEKLY_HARD_LOCK_LIMIT
        or monthly_dd >= MONTHLY_HARD_LOCK_LIMIT
    )
    soft_breach = (
        daily_dd >= DAILY_SOFT_LOCK_LIMIT
        or weekly_dd >= WEEKLY_SOFT_LOCK_LIMIT
        or monthly_dd >= MONTHLY_SOFT_LOCK_LIMIT
    )
    warning_breach = (
        daily_dd >= DAILY_SOFT_LOCK_LIMIT * WARNING_RATIO_OF_SOFT_LOCK
        or weekly_dd >= WEEKLY_SOFT_LOCK_LIMIT * WARNING_RATIO_OF_SOFT_LOCK
        or monthly_dd >= MONTHLY_SOFT_LOCK_LIMIT * WARNING_RATIO_OF_SOFT_LOCK
    )

    if hard_breach:
        event = DrawdownEvent.HARD_LOCK_THRESHOLD_BREACHED
    elif soft_breach:
        event = DrawdownEvent.SOFT_LOCK_THRESHOLD_BREACHED
    elif warning_breach:
        event = DrawdownEvent.WARNING_THRESHOLD_BREACHED
    else:
        event = DrawdownEvent.WITHIN_TOLERANCE

    return DrawdownClassification(
        daily_drawdown_pct=daily_dd,
        weekly_drawdown_pct=weekly_dd,
        monthly_drawdown_pct=monthly_dd,
        event=event,
    )


def transition_drawdown_state(current_state: DrawdownState, event: DrawdownEvent) -> DrawdownState:
    """The pure FSM reducer `docs/PRODUCTION_SPEC.md` §6 requires:
    `FSM(current_state, event) -> new_state`. The sole authority for
    drawdown-driven state transitions in this codebase — no other module
    may reimplement or shortcut this decision.
    """
    if event == DrawdownEvent.MANUAL_RESET_CONFIRMED:
        return DrawdownState.ACTIVE

    if current_state == DrawdownState.MANUAL_RESET_REQUIRED:
        return DrawdownState.MANUAL_RESET_REQUIRED

    if current_state == DrawdownState.HARD_LOCK:
        # HARD_LOCK's response (liquidate-or-freeze, decide_hard_lock_response()
        # below) has already been computed and acted upon by the impure
        # caller for this same cycle; every path from HARD_LOCK leads to
        # waiting for a human, regardless of this cycle's new event.
        return DrawdownState.MANUAL_RESET_REQUIRED

    if event == DrawdownEvent.HARD_LOCK_THRESHOLD_BREACHED:
        return DrawdownState.HARD_LOCK
    if event == DrawdownEvent.SOFT_LOCK_THRESHOLD_BREACHED:
        return DrawdownState.SOFT_LOCK
    if event == DrawdownEvent.WARNING_THRESHOLD_BREACHED:
        return DrawdownState.WARNING
    return DrawdownState.ACTIVE


def blocks_new_entries(state: DrawdownState) -> bool:
    """`SOFT_LOCK`/`HARD_LOCK`/`MANUAL_RESET_REQUIRED` all freeze new
    entries."""
    return state in (
        DrawdownState.SOFT_LOCK,
        DrawdownState.HARD_LOCK,
        DrawdownState.MANUAL_RESET_REQUIRED,
    )


def blocks_position_management(state: DrawdownState) -> bool:
    """Only `HARD_LOCK`/`MANUAL_RESET_REQUIRED` freeze existing-position
    management (trailing stop, breakeven, partial close). `SOFT_LOCK`
    explicitly allows it to keep running (`docs/PRODUCTION_SPEC.md` §6:
    "freeze all new entries while allowing active server-side trailing
    parameters to execute")."""
    return state in (DrawdownState.HARD_LOCK, DrawdownState.MANUAL_RESET_REQUIRED)


@dataclass(frozen=True, slots=True)
class HardLockResponse:
    """The `FeatureFlagManager`-driven decision `docs/PRODUCTION_SPEC.md`
    §6 requires the instant `HARD_LOCK` is (freshly) entered."""

    should_liquidate: bool
    reason: str


def decide_hard_lock_response(
    new_state: DrawdownState, *, liquidate_on_hard_lock: bool
) -> HardLockResponse | None:
    """Returns the `HARD_LOCK` response iff `new_state` is the
    freshly-entered `HARD_LOCK` this cycle — by construction,
    `transition_drawdown_state()` never returns `HARD_LOCK` for a
    `current_state` that was already `HARD_LOCK` (it advances straight to
    `MANUAL_RESET_REQUIRED` instead), so `new_state == HARD_LOCK` is
    always exactly the entry moment. Returns `None` for every other state,
    so the caller never re-triggers a liquidation decision on a later
    cycle.
    """
    if new_state != DrawdownState.HARD_LOCK:
        return None
    if liquidate_on_hard_lock:
        return HardLockResponse(
            should_liquidate=True,
            reason="HARD_LOCK breached with liquidate_on_hard_lock=True: emergency liquidation",
        )
    return HardLockResponse(
        should_liquidate=False,
        reason="HARD_LOCK breached with liquidate_on_hard_lock=False: absolute system freeze",
    )
