"""Calendar-provider-chain configuration (`docs/PRODUCTION_SPEC.md` §2's
`calendar.provider_priority`/`timeout_ms`/`rate_limit_per_min` matrix),
loaded from environment variables.

Sole owner of the `CALENDAR_*` environment variables — no other module
reads them directly (same rule `config/config_manager.py` states for the
required credentials it owns).

Base URLs for network-backed providers are deliberately never hardcoded
here or in `news/calendar_provider.py`: no verified real API contract
exists for either `tradingeconomics` or `finnhub` in this codebase (see
`news/README.md`'s provenance note), so guessing a plausible-looking
vendor URL would be actively misleading. A network provider listed in
`CALENDAR_PROVIDER_PRIORITY` without its `CALENDAR_<PROVIDER>_BASE_URL`
set is a fatal `ConfigurationError`, not a silent skip or a guessed
default — consistent with §1's fail-closed boot posture.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from config.config_manager import ConfigurationError

DEFAULT_PROVIDER_PRIORITY: tuple[str, ...] = ("offline_snapshot",)
DEFAULT_TIMEOUT_MS = 3000
DEFAULT_RATE_LIMIT_PER_MIN = 60

KNOWN_NETWORK_PROVIDERS: tuple[str, ...] = ("tradingeconomics", "finnhub")
OFFLINE_PROVIDER_NAME = "offline_snapshot"
KNOWN_PROVIDERS: tuple[str, ...] = (*KNOWN_NETWORK_PROVIDERS, OFFLINE_PROVIDER_NAME)


@dataclass(frozen=True, slots=True)
class CalendarConfig:
    """Validated, typed snapshot of the calendar-provider-chain
    configuration."""

    provider_priority: tuple[str, ...]
    timeout_ms: int
    rate_limit_per_min: int
    provider_base_urls: dict[str, str]
    offline_snapshot_path: Path

    @classmethod
    def from_env(cls) -> "CalendarConfig":
        """Load and validate `CALENDAR_*` environment variables.

        Every field has a safe default that requires no additional
        configuration (`provider_priority` defaults to
        `("offline_snapshot",)` alone, matching what `.env.template` ships
        out of the box) — the spec's illustrative 3-provider example is
        opt-in via `CALENDAR_PROVIDER_PRIORITY`, not the unconditional
        default, so that boot never fails for a deployer who hasn't
        supplied real `tradingeconomics`/`finnhub` endpoints.
        """
        raw_priority = os.environ.get(
            "CALENDAR_PROVIDER_PRIORITY", ",".join(DEFAULT_PROVIDER_PRIORITY)
        )
        provider_priority = tuple(p.strip() for p in raw_priority.split(",") if p.strip())

        unknown = [p for p in provider_priority if p not in KNOWN_PROVIDERS]
        if unknown:
            raise ConfigurationError(
                f"CALENDAR_PROVIDER_PRIORITY contains unknown provider(s): "
                f"{', '.join(unknown)}. Known providers: {', '.join(KNOWN_PROVIDERS)}."
            )

        timeout_ms = _parse_int_env("CALENDAR_TIMEOUT_MS", DEFAULT_TIMEOUT_MS)
        rate_limit_per_min = _parse_int_env(
            "CALENDAR_RATE_LIMIT_PER_MIN", DEFAULT_RATE_LIMIT_PER_MIN
        )

        provider_base_urls: dict[str, str] = {}
        for provider in provider_priority:
            if provider not in KNOWN_NETWORK_PROVIDERS:
                continue
            env_key = f"CALENDAR_{provider.upper()}_BASE_URL"
            base_url = os.environ.get(env_key)
            if not base_url:
                raise ConfigurationError(
                    f"{env_key} must be set since {provider!r} is listed in "
                    "CALENDAR_PROVIDER_PRIORITY (docs/PRODUCTION_SPEC.md §2)."
                )
            provider_base_urls[provider] = base_url

        snapshot_path = Path(
            os.environ.get("CALENDAR_OFFLINE_SNAPSHOT_PATH", str(_default_snapshot_path()))
        )

        return cls(
            provider_priority=provider_priority,
            timeout_ms=timeout_ms,
            rate_limit_per_min=rate_limit_per_min,
            provider_base_urls=provider_base_urls,
            offline_snapshot_path=snapshot_path,
        )


def _parse_int_env(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{key}={raw!r} is not a valid integer.") from exc


def _default_snapshot_path() -> Path:
    return Path(__file__).resolve().parent.parent / "news" / "offline_calendar_snapshot.json"
