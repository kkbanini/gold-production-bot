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
nor does it source the bar-close-wait timing from `clock_provider` — see
`docs/ARCHITECTURE_SUMMARY.md` §5.
Phase 11d added `feature_flags` (`FeatureFlagManager`,
docs/PRODUCTION_SPEC.md §6), consumed by `main.py`'s `run_bar_close_cycle()`
to decide `HARD_LOCK`'s liquidate-vs-freeze behavior. Phase 11e added
`initial_drawdown_state`: the Disaster Recovery reconciliation
(docs/PRODUCTION_SPEC.md §7) settles any broker/ledger divergence found at
boot and computes whether the FSM may start `ACTIVE` or must start
`MANUAL_RESET_REQUIRED`, consumed by `main()` when it seeds its first
`FSMContext`. Later sub-phases extend this same container rather than
introducing their own ad hoc wiring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from broker.clock_provider import MT5ClockProvider
from broker.mt5_gateway import MT5Gateway, resolve_position_audit
from config.calendar_config import CalendarConfig
from config.config_manager import ConfigManager
from config.feature_flags import FeatureFlagManager, FeatureFlags
from config.secret_redaction import SecretRedactingFilter
from news.calendar_provider import CalendarProviderChain, build_calendar_provider_chain
from risk.drawdown_fsm import DrawdownState
from storage.state_manager import AuditActionType, StateManager

logger = logging.getLogger(__name__)


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

        return cls(
            config=config,
            state_manager=state_manager,
            gateway=gateway,
            calendar_provider=calendar_provider,
            clock_provider=clock_provider,
            feature_flags=feature_flags,
            initial_drawdown_state=initial_drawdown_state,
        )
