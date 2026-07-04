"""Boot-time environment configuration loading and validation.

Sole owner of environment/secret access (ADR-0002 consumers, RQ-018,
docs/RISK_REGISTER.md RR-001, RR-012). No other module reads `os.environ`
directly for a trading-relevant value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REQUIRED_ENV_VARS: tuple[str, ...] = (
    "MT5_LOGIN",
    "MT5_PASSWORD",
    "MT5_SERVER",
    "ECONOMIC_CALENDAR_API_KEY",
    "STRATEGY_MAGIC_NUMBER",
    "ENVIRONMENT_MODE",
)

VALID_ENVIRONMENT_MODES: tuple[str, ...] = ("DEMO", "LIVE")


class ConfigurationError(Exception):
    """Raised when required environment variables are missing or invalid.

    Raising this at boot time is the enforcement mechanism for RQ-018 and
    RR-012: the system must never start with an incomplete or ambiguous
    configuration.
    """


@dataclass(frozen=True, slots=True)
class ConfigManager:
    """Validated, typed snapshot of the runtime environment configuration."""

    mt5_login: int
    mt5_password: str
    mt5_server: str
    economic_calendar_api_key: str
    strategy_magic_number: int
    environment_mode: str

    @classmethod
    def load(cls, env_file: str | Path | None = None) -> "ConfigManager":
        """Load and validate configuration from the environment.

        Populates `os.environ` from `env_file` (defaults to `.env` in the
        current working directory, via python-dotenv's own discovery) without
        overriding variables already set in the process environment, then
        validates that every required key is present and well-formed before
        returning. Raises ConfigurationError on any validation failure so
        that startup halts rather than proceeding with a partial config.
        """
        load_dotenv(dotenv_path=env_file, override=False)

        missing = [key for key in REQUIRED_ENV_VARS if not os.getenv(key)]
        if missing:
            raise ConfigurationError(
                "Missing required environment variable(s): "
                f"{', '.join(missing)}. Populate them in your local .env file "
                "(see .env.template) before starting the system."
            )

        environment_mode = os.environ["ENVIRONMENT_MODE"].strip().upper()
        if environment_mode not in VALID_ENVIRONMENT_MODES:
            raise ConfigurationError(
                f"ENVIRONMENT_MODE={environment_mode!r} is invalid; expected "
                f"one of {VALID_ENVIRONMENT_MODES}."
            )

        mt5_login_raw = os.environ["MT5_LOGIN"]
        try:
            mt5_login = int(mt5_login_raw)
        except ValueError as exc:
            raise ConfigurationError(
                f"MT5_LOGIN={mt5_login_raw!r} is not a valid integer account number."
            ) from exc

        magic_number_raw = os.environ["STRATEGY_MAGIC_NUMBER"]
        try:
            strategy_magic_number = int(magic_number_raw)
        except ValueError as exc:
            raise ConfigurationError(
                f"STRATEGY_MAGIC_NUMBER={magic_number_raw!r} is not a valid integer."
            ) from exc

        return cls(
            mt5_login=mt5_login,
            mt5_password=os.environ["MT5_PASSWORD"],
            mt5_server=os.environ["MT5_SERVER"],
            economic_calendar_api_key=os.environ["ECONOMIC_CALENDAR_API_KEY"],
            strategy_magic_number=strategy_magic_number,
            environment_mode=environment_mode,
        )
