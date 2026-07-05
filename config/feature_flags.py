"""Feature-flag configuration (`docs/PRODUCTION_SPEC.md` §6's
`config.flags.liquidate_on_hard_lock`): boot-time-loaded, immutable
booleans gating alternate system behavior.

Sole owner of the `FLAG_*` environment variables — no other module reads
them directly (the same rule `config/config_manager.py` states for the
required credentials it owns, and `config/calendar_config.py` for the
`CALENDAR_*` variables).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from config.config_manager import ConfigurationError

_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "off"})


def _parse_bool_env(key: str, *, default: bool) -> bool:
    raw = os.environ.get(key)
    if not raw:
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise ConfigurationError(
        f"{key}={raw!r} is not a valid boolean "
        f"(expected one of {sorted(_TRUE_VALUES | _FALSE_VALUES)})."
    )


@dataclass(frozen=True, slots=True)
class FeatureFlags:
    """Validated, typed snapshot of every feature flag."""

    liquidate_on_hard_lock: bool

    @classmethod
    def from_env(cls) -> "FeatureFlags":
        """Defaults `liquidate_on_hard_lock` to `False` (freeze rather than
        auto-liquidate real capital) — the safer posture when a deployer
        hasn't made an explicit choice, consistent with this project's
        fail-closed defaults elsewhere (`config/calendar_config.py`'s
        `offline_snapshot`-only default, etc.).
        """
        return cls(
            liquidate_on_hard_lock=_parse_bool_env("FLAG_LIQUIDATE_ON_HARD_LOCK", default=False)
        )


class FeatureFlagManager:
    """The single place `HARD_LOCK`'s liquidate-vs-freeze behavior is read
    from (`docs/PRODUCTION_SPEC.md` §6), rather than each caller reaching
    into `container.config` or `os.environ` ad hoc.
    """

    def __init__(self, flags: FeatureFlags) -> None:
        self._flags = flags

    @property
    def liquidate_on_hard_lock(self) -> bool:
        return self._flags.liquidate_on_hard_lock
