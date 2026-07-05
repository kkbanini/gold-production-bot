"""Centralized composition root: constructs and wires every long-lived
service object (config, storage, broker) via constructor-based dependency
injection (docs/PRODUCTION_SPEC.md "Core Orchestration Directive" #1).

Replaces ad hoc construction previously scattered through `main.py`'s
bootstrap logic with a single object graph assembled in one place: every
dependency a component needs is passed into its constructor explicitly,
rather than each component importing/constructing its own collaborators.
Later Phase 11 sub-phases (11b: `CalendarProvider`/`ClockProvider`, 11c:
event-sourced ledger) extend this same container rather than introducing
their own ad hoc wiring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from broker.mt5_gateway import MT5Gateway
from config.config_manager import ConfigManager
from config.secret_redaction import SecretRedactingFilter
from storage.state_manager import StateManager

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

        return cls(config=config, state_manager=state_manager, gateway=gateway)
