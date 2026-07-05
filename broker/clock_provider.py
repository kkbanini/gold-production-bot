"""`ClockProvider`: a broker-agnostic abstraction over server time, so
execution-session-boundary logic never reads the host machine's local
clock or bakes in a hardcoded DST table (`docs/PRODUCTION_SPEC.md` §3).

`MT5ClockProvider` derives server time strictly from the connected
`MT5Gateway`'s own `broker_utc_offset` (ADR-0002) — the same fixed
UTC-offset value `is_within_execution_window()` implicitly assumes has
already been applied to whatever timestamp it's given.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from broker.mt5_gateway import MT5Gateway


@runtime_checkable
class ClockProvider(Protocol):
    """Resolves the current broker server time for a given symbol,
    normalized to a timezone-aware UTC datetime (the spec's
    `AwareDatetime`)."""

    def get_server_time(self, symbol: str) -> datetime: ...


@dataclass(frozen=True, slots=True)
class MT5ClockProvider:
    """The production `ClockProvider`: `gateway.broker_utc_offset` was
    sampled once at connect time as `broker_time - host_utc_now`
    (`MT5Gateway._resolve_broker_utc_offset`); adding it back to the
    current host UTC time reconstructs the current broker server time
    without ever reading a hardcoded DST rule."""

    gateway: MT5Gateway

    def get_server_time(self, symbol: str) -> datetime:
        if symbol != self.gateway.symbol_spec.name:
            raise ValueError(
                f"MT5ClockProvider is bound to {self.gateway.symbol_spec.name!r}; "
                f"got symbol={symbol!r} (multi-symbol clocks are not supported — "
                "this system trades a single Gold symbol)."
            )
        return datetime.now(timezone.utc) + self.gateway.broker_utc_offset
