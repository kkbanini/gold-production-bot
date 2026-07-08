"""Centralized composition root: constructs and wires every long-lived
service object (config, storage, broker) via constructor-based dependency
injection (docs/PRODUCTION_SPEC.md "Core Orchestration Directive" #1).

Replaces ad hoc construction previously scattered through `main.py`'s
bootstrap logic with a single object graph assembled in one place: every
dependency a component needs is passed into its constructor explicitly,
rather than each component importing/constructing its own collaborators.
Phase 11b added `calendar_provider`/`clock_provider` (`CalendarProvider`/
`ClockProvider`, docs/PRODUCTION_SPEC.md §2/§3). `main.py`'s live loop now
consumes `clock_provider` for equity-baseline rollover (broker server time
decides UTC-day/ISO-week/calendar-month boundaries, `risk/drawdown_fsm.py`'s
`roll_equity_baselines()`); it still does not consume `calendar_provider`,
nor does it source the bar-close-wait/weekend-check timing from
`clock_provider` — see `docs/ARCHITECTURE_SUMMARY.md` §5.
Phase 11d added `feature_flags` (`FeatureFlagManager`,
docs/PRODUCTION_SPEC.md §6), consumed by `main.py`'s `run_bar_close_cycle()`
to decide `HARD_LOCK`'s liquidate-vs-freeze behavior. Phase 11e added
`initial_drawdown_state`: the Disaster Recovery reconciliation
(docs/PRODUCTION_SPEC.md §7) settles any broker/ledger divergence found at
boot and computes whether the FSM may start `ACTIVE` or must start
`MANUAL_RESET_REQUIRED`, consumed by `main()` when it seeds its first
`FSMContext`. Later sub-phases extend this same container rather than
introducing their own ad hoc wiring.

`optimizer_scheduler` starts `optimizer/self_learning.py`'s weekly
self-learning job (`create_weekend_optimizer_scheduler()`) — previously
built and fully tested but never actually started anywhere, an open gap
`optimizer/README.md` documented explicitly. `main()`'s live loop resolves
the same job's applied parameter shifts every cycle via
`optimizer.self_learning.get_effective_parameter_value()`, so a Saturday
shift changes live behavior starting the next cycle, not just a
`parameter_history` row nothing reads back.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from broker.clock_provider import MT5ClockProvider
from broker.mt5_gateway import MT5Gateway, resolve_position_audit
from config.calendar_config import CalendarConfig
from config.config_manager import ConfigManager
from config.feature_flags import FeatureFlagManager, FeatureFlags
from config.secret_redaction import SecretRedactingFilter
from execution.position_manager import TRAILING_ATR_MULTIPLIER
from news.calendar_provider import CalendarProviderChain, build_calendar_provider_chain
from optimizer.self_learning import (
    TunableParameter,
    WeeklyOptimizationCycleResult,
    create_weekend_optimizer_scheduler,
    get_effective_parameter_value,
    run_weekly_optimization_cycle,
)
from risk.drawdown_fsm import DrawdownState
from storage.state_manager import AuditActionType, StateManager
from strategy.trend_filter import ADX_TREND_THRESHOLD

logger = logging.getLogger(__name__)

# Bounds each self-learning-tunable parameter may move within, and the
# per-step size `decide_parameter_shift()` moves by — not independently
# derived from any spec, matches the bounds this module's own test suite
# already exercises (tests/unit/test_unit.py::TestSelfLearning).
ADX_TREND_THRESHOLD_BOUNDS = (20.0, 35.0, 1.0)
TRAILING_ATR_MULTIPLIER_BOUNDS = (1.0, 3.0, 0.25)


def _build_weekly_optimization_job(
    state_manager: StateManager,
) -> Callable[[], WeeklyOptimizationCycleResult]:
    """Construct the callable `create_weekend_optimizer_scheduler()` runs
    every Saturday: seeds each `TunableParameter`'s current `value` fresh
    from `get_effective_parameter_value()` (the latest applied shift, or
    the hardcoded default if never shifted) so this week's decision
    builds on last week's, not always the original constant.
    """

    def job() -> WeeklyOptimizationCycleResult:
        adx_min, adx_max, adx_step = ADX_TREND_THRESHOLD_BOUNDS
        trail_min, trail_max, trail_step = TRAILING_ATR_MULTIPLIER_BOUNDS
        tunable_parameters = {
            "ADX_TREND_THRESHOLD": TunableParameter(
                "ADX_TREND_THRESHOLD",
                get_effective_parameter_value(
                    state_manager, "ADX_TREND_THRESHOLD", ADX_TREND_THRESHOLD
                ),
                adx_min,
                adx_max,
                adx_step,
            ),
            "TRAILING_ATR_MULTIPLIER": TunableParameter(
                "TRAILING_ATR_MULTIPLIER",
                get_effective_parameter_value(
                    state_manager, "TRAILING_ATR_MULTIPLIER", TRAILING_ATR_MULTIPLIER
                ),
                trail_min,
                trail_max,
                trail_step,
            ),
        }
        return run_weekly_optimization_cycle(state_manager, tunable_parameters)

    return job


@dataclass
class ApplicationContainer:
    """The application's fully-wired object graph.

    Construct once via `ApplicationContainer.build()` at process startup;
    every long-lived service the rest of the system needs lives here, not
    in module-level globals or re-constructed ad hoc by each caller.
    """

    config: ConfigManager
    state_manager: StateManager
    gateway: MT5Gateway
    calendar_provider: CalendarProviderChain
    clock_provider: MT5ClockProvider
    feature_flags: FeatureFlagManager
    initial_drawdown_state: DrawdownState
    optimizer_scheduler: BackgroundScheduler

    @classmethod
    def build(
        cls, env_file: str | None = None, db_path: Path | str | None = None
    ) -> "ApplicationContainer":
        """Construct the full object graph per docs/RUNBOOK.md §1's boot
        sequence: load + validate config, attach secret redaction to the
        root logger, open storage, connect the broker (with exponential
        backoff), and reconcile broker-reported positions against the
        local ledger.

        `db_path` overrides `storage.db_engine.DEFAULT_DB_PATH` — mainly
        so tests can point this at a temp file rather than the real
        on-disk ledger.

        Fail-closed: any step's exception (`ConfigurationError`,
        `BrokerConnectionError`, etc.) propagates uncaught, halting
        startup rather than returning a partially-initialized container.
        """
        config = ConfigManager.load(env_file=env_file)

        redaction_filter = SecretRedactingFilter(
            [config.mt5_password, config.economic_calendar_api_key]
        )
        logging.getLogger().addFilter(redaction_filter)

        state_manager = StateManager(db_path) if db_path is not None else StateManager()

        gateway = MT5Gateway(
            login=config.mt5_login,
            password=config.mt5_password,
            server=config.mt5_server,
            magic_number=config.strategy_magic_number,
        )
        gateway.connect()
        logger.info(
            "Connected to MT5: server=%s symbol=%s magic=%s",
            config.mt5_server,
            gateway.symbol_spec.name,
            config.strategy_magic_number,
        )

        audit = gateway.audit_open_positions(state_manager.get_open_trades())
        if not audit.is_clean:
            logger.warning(
                "Position audit found divergence on startup: broker_only=%s ledger_only=%s",
                [p.ticket for p in audit.broker_only_positions],
                [e.client_order_id for e in audit.ledger_only_entries],
            )

        # Disaster Recovery reconciliation (docs/PRODUCTION_SPEC.md §7):
        # settle any broker/ledger divergence found above, and gate the
        # FSM's starting drawdown_state on whether reconciliation was
        # clean — docs/RUNBOOK.md already treats a position-audit mismatch
        # as HIGH-severity, blocking automated trading pending manual
        # review (RR-008).
        plan = resolve_position_audit(audit)
        for upsert in plan.ledger_upserts:
            state_manager.record_trade(upsert)
        if plan.requires_manual_review:
            logger.critical(
                "Disaster recovery reconciliation required manual review: %s", plan.summary
            )
            state_manager.record_audit_event(
                actor="system:disaster_recovery",
                action_type=AuditActionType.DISASTER_RECOVERY_RECONCILIATION.value,
                parameter_name="fsm_startup_drawdown_state",
                old_value=None,
                new_value=DrawdownState.MANUAL_RESET_REQUIRED.value,
                metadata={"summary": plan.summary},
            )
            initial_drawdown_state = DrawdownState.MANUAL_RESET_REQUIRED
        else:
            initial_drawdown_state = DrawdownState.ACTIVE

        calendar_config = CalendarConfig.from_env()
        calendar_provider = build_calendar_provider_chain(
            calendar_config, config.economic_calendar_api_key
        )
        clock_provider = MT5ClockProvider(gateway=gateway)

        feature_flags = FeatureFlagManager(FeatureFlags.from_env())

        # Self-learning optimizer (optimizer/self_learning.py, previously
        # built but never started anywhere — optimizer/README.md's
        # "Depended On By" section flagged this exact wiring as missing).
        # BackgroundScheduler runs the job in its own thread; no explicit
        # shutdown path, matching this process's existing no-graceful-
        # shutdown posture elsewhere.
        optimizer_scheduler = create_weekend_optimizer_scheduler(
            _build_weekly_optimization_job(state_manager)
        )
        optimizer_scheduler.start()

        logger.info(
            "ApplicationContainer built: environment_mode=%s drawdown_state=%s",
            config.environment_mode,
            initial_drawdown_state.value,
        )

        return cls(
            config=config,
            state_manager=state_manager,
            gateway=gateway,
            calendar_provider=calendar_provider,
            clock_provider=clock_provider,
            feature_flags=feature_flags,
            initial_drawdown_state=initial_drawdown_state,
            optimizer_scheduler=optimizer_scheduler,
        )
