"""Boot-time environment configuration loading and validation.

Sole owner of environment/secret access (ADR-0002 consumers, RQ-018,
docs/RISK_REGISTER.md RR-001, RR-012). No other module reads `os.environ`
directly for a trading-relevant value.

`ConfigValidator` (docs/PRODUCTION_SPEC.md §1) inspects every required key
at initialization for presence, placeholder/default-value leakage, and
syntactic validity. Raising `ConfigurationError` from any check is the
"fatal application panic" §1 requires: it propagates uncaught through
`ConfigManager.load()`, halting process startup rather than proceeding
with a partial or leaked configuration.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
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
    "TRADING_MODE",
    "SHORT_TERM_MAGIC_NUMBER",
)

VALID_ENVIRONMENT_MODES: tuple[str, ...] = ("DEMO", "LIVE")

# WAIT_FOR_CONDITIONS: the original single strategy (D1+H4+H1 alignment).
# SHORT_TERM: the relaxed, higher-frequency scalp mode only.
# BOTH: both run concurrently under separate magic numbers.
VALID_TRADING_MODES: tuple[str, ...] = ("WAIT_FOR_CONDITIONS", "SHORT_TERM", "BOTH")

# Substrings (case-insensitive) that indicate a value was left as a
# template/example placeholder rather than replaced with a real credential
# (docs/PRODUCTION_SPEC.md §1's "default-leaked" check). Deliberately a
# curated list, not a generic weak-password check — false positives here
# would block legitimate startup.
_PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "changeme",
    "change_me",
    "your_",
    "xxxx",
    "placeholder",
    "insert_",
    "<your",
    "replace_me",
    "example.com",
    "todo",
)


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


class ConfigurationError(Exception):
    """Raised when required environment variables are missing, leaked
    placeholders, or syntactically invalid.

    Raising this at boot time is the enforcement mechanism for RQ-018,
    RR-012, and docs/PRODUCTION_SPEC.md §1: the system must never start
    with an incomplete, leaked, or ambiguous configuration.
    """


class ConfigValidator:
    """Inspects raw environment values at boot time (docs/PRODUCTION_SPEC.md §1).

    Each `check_*` method raises `ConfigurationError` on its own failure
    mode so the specific problem (missing / leaked / malformed) is always
    named in the exception rather than folded into one generic message.
    """

    def __init__(self, required_vars: tuple[str, ...] = REQUIRED_ENV_VARS) -> None:
        self.required_vars = required_vars

    def check_presence(self, env: Mapping[str, str]) -> None:
        missing = [key for key in self.required_vars if not env.get(key)]
        if missing:
            raise ConfigurationError(
                "Missing required environment variable(s): "
                f"{', '.join(missing)}. Populate them in your local .env file "
                "(see .env.template) before starting the system."
            )

    def check_no_placeholder_leak(self, env: Mapping[str, str]) -> None:
        leaked = [key for key in self.required_vars if _looks_like_placeholder(env.get(key, ""))]
        if leaked:
            raise ConfigurationError(
                f"Placeholder/default value(s) still set for: {', '.join(leaked)}. "
                "Replace these with real values before starting the system "
                "(docs/PRODUCTION_SPEC.md §1)."
            )

    def check_environment_mode(self, env: Mapping[str, str]) -> str:
        environment_mode = env["ENVIRONMENT_MODE"].strip().upper()
        if environment_mode not in VALID_ENVIRONMENT_MODES:
            raise ConfigurationError(
                f"ENVIRONMENT_MODE={environment_mode!r} is invalid; expected "
                f"one of {VALID_ENVIRONMENT_MODES}."
            )
        return environment_mode

    def check_trading_mode(self, env: Mapping[str, str]) -> str:
        trading_mode = env["TRADING_MODE"].strip().upper()
        if trading_mode not in VALID_TRADING_MODES:
            raise ConfigurationError(
                f"TRADING_MODE={trading_mode!r} is invalid; expected "
                f"one of {VALID_TRADING_MODES}."
            )
        return trading_mode

    def check_integer(self, env: Mapping[str, str], key: str) -> int:
        raw = env[key]
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigurationError(f"{key}={raw!r} is not a valid integer.") from exc

    def check_magic_numbers_distinct(
        self, strategy_magic_number: int, short_term_magic_number: int
    ) -> None:
        if strategy_magic_number == short_term_magic_number:
            raise ConfigurationError(
                f"SHORT_TERM_MAGIC_NUMBER must differ from STRATEGY_MAGIC_NUMBER "
                f"(both are {strategy_magic_number!r}); the short-term mode's "
                "positions would be indistinguishable from the regular "
                "strategy's, corrupting both position tracking and disaster "
                "recovery reconciliation."
            )

    def validate(self, env: Mapping[str, str]) -> tuple[int, int, str, str, int]:
        """Run every check in order (fail fast on the first violation);
        returns `(mt5_login, strategy_magic_number, environment_mode,
        trading_mode, short_term_magic_number)` once all checks pass.
        """
        self.check_presence(env)
        self.check_no_placeholder_leak(env)
        environment_mode = self.check_environment_mode(env)
        trading_mode = self.check_trading_mode(env)
        mt5_login = self.check_integer(env, "MT5_LOGIN")
        strategy_magic_number = self.check_integer(env, "STRATEGY_MAGIC_NUMBER")
        short_term_magic_number = self.check_integer(env, "SHORT_TERM_MAGIC_NUMBER")
        self.check_magic_numbers_distinct(strategy_magic_number, short_term_magic_number)
        return (
            mt5_login,
            strategy_magic_number,
            environment_mode,
            trading_mode,
            short_term_magic_number,
        )


@dataclass(frozen=True, slots=True)
class ConfigManager:
    """Validated, typed snapshot of the runtime environment configuration."""

    mt5_login: int
    mt5_password: str
    mt5_server: str
    economic_calendar_api_key: str
    strategy_magic_number: int
    environment_mode: str
    trading_mode: str
    short_term_magic_number: int

    @classmethod
    def load(cls, env_file: str | Path | None = None) -> "ConfigManager":
        """Load and validate configuration from the environment.

        Populates `os.environ` from `env_file` (defaults to `.env` in the
        current working directory, via python-dotenv's own discovery) without
        overriding variables already set in the process environment, then
        runs `ConfigValidator` against it before returning. Raises
        `ConfigurationError` on any validation failure so that startup halts
        rather than proceeding with a partial or leaked configuration.
        """
        load_dotenv(dotenv_path=env_file, override=False)

        (
            mt5_login,
            strategy_magic_number,
            environment_mode,
            trading_mode,
            short_term_magic_number,
        ) = ConfigValidator().validate(os.environ)

        return cls(
            mt5_login=mt5_login,
            mt5_password=os.environ["MT5_PASSWORD"],
            mt5_server=os.environ["MT5_SERVER"],
            economic_calendar_api_key=os.environ["ECONOMIC_CALENDAR_API_KEY"],
            strategy_magic_number=strategy_magic_number,
            environment_mode=environment_mode,
            trading_mode=trading_mode,
            short_term_magic_number=short_term_magic_number,
        )
