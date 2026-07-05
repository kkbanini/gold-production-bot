"""Rich pre-trade/pre-retry validation gates (`docs/PRODUCTION_SPEC.md` §5's
`PreTradeValidator` pipeline).

This phase (11c) implements the pipeline's concrete duplicate-order gate:
`check_duplicate_order_before_retry()`, the realization of RR-007's
"`client_order_id` idempotency key check ... before resubmitting on retry"
against the concrete `storage.state_manager` Event Store rather than the
generic `OrderRepository` Protocol `docs/API_SPEC.md` §4 originally
specified (the same simplification `storage/README.md` already documents
for the rest of the event-sourcing model). A general-purpose pre-trade
risk/slippage gate (RQ-009/RQ-010) is a separate, still-open gap — see
`docs/ARCHITECTURE_SUMMARY.md` §5 — not built here.

Pure decision logic only: takes the two already-fetched facts the gate
needs (the local Event Store's latest recorded state, and whether a
previously-recorded broker ticket is still confirmed open) rather than a
`StateManager`/`MT5Gateway` reference itself, so it has no I/O and no
dependency on either module — consistent with this codebase's pure
decision / impure glue split (`main.py`'s `run_bar_close_cycle()` is the
same shape).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from storage.state_manager import OrderEvent, OrderLifecycleState

_PRE_SEND_STATES = frozenset({OrderLifecycleState.REQUESTED, OrderLifecycleState.VALIDATED})
_TERMINAL_NEGATIVE_STATES = frozenset(
    {OrderLifecycleState.REJECTED, OrderLifecycleState.EXPIRED, OrderLifecycleState.CANCELLED}
)


class SeverityLevel(str, Enum):
    """`docs/PRODUCTION_SPEC.md` §5's `ValidationResult.severity` levels."""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """`docs/PRODUCTION_SPEC.md` §5's rich validator payload.

    `metadata` is typed `dict[str, Any]` rather than the spec's literal
    bare `dict` — this project's `mypy --strict` config enables
    `disallow-any-generics`, under which an untyped `dict` is itself a
    strict-mode violation.
    """

    is_valid: bool
    reason_code: str
    severity: SeverityLevel
    is_retryable: bool
    metadata: dict[str, Any]


def check_duplicate_order_before_retry(
    latest_event: OrderEvent | None,
    *,
    broker_ticket_still_open: bool | None,
) -> ValidationResult:
    """The `PreTradeValidator` pipeline's duplicate-order gate
    (`docs/PRODUCTION_SPEC.md` §4/§5, RR-007): decides whether an automated
    retry for the order `latest_event` belongs to is safe, by reducing two
    audited facts:

    - `latest_event` — the local transaction engine's (`order_events`)
      most recently recorded lifecycle state for this `client_order_id`
      ("actively audit the local transaction engine").
    - `broker_ticket_still_open` — whether the broker's live position
      cache still confirms open a ticket a prior `SENT`-or-later event
      recorded ("query the server cache"). `None` when no such ticket
      exists yet to check (nothing was ever sent).

    Decision table:
    - No prior event at all -> new request, retry is trivially safe.
    - Latest state is `REQUESTED`/`VALIDATED` (never reached the broker)
      -> safe to retry.
    - Latest state is `CLOSED` -> the order's lifecycle is already
      finished; retrying is nonsensical, not merely unsafe.
    - Latest state is a terminal negative (`REJECTED`/`EXPIRED`/`CANCELLED`)
      -> the broker already confirmed no fill occurred; safe to retry.
    - Latest state is in-flight/confirmed (`SENT`/`PENDING`/
      `PARTIALLY_FILLED`/`FILLED`/`MODIFIED`) and the broker confirms the
      ticket is still open -> a real duplicate-order risk; refuse.
    - Same in-flight/confirmed states but the broker does *not* confirm an
      open ticket -> ambiguous (the ticket may simply never have been
      recorded, or may have been filled *and already closed*); absence of
      an open ticket does not prove absence of a fill, so this is refused
      for manual review rather than assumed safe — the conservative
      reading of "strictly mitigating duplicate order anomalies".
    """
    if latest_event is None:
        return ValidationResult(
            is_valid=True,
            reason_code="NEW_ORDER_NO_PRIOR_EVENT",
            severity=SeverityLevel.INFO,
            is_retryable=True,
            metadata={},
        )

    if latest_event.event_type in _PRE_SEND_STATES:
        return ValidationResult(
            is_valid=True,
            reason_code="NOT_YET_SENT_SAFE_TO_RETRY",
            severity=SeverityLevel.INFO,
            is_retryable=True,
            metadata={"client_order_id": latest_event.client_order_id},
        )

    if latest_event.event_type is OrderLifecycleState.CLOSED:
        return ValidationResult(
            is_valid=False,
            reason_code="ORDER_ALREADY_CLOSED",
            severity=SeverityLevel.ERROR,
            is_retryable=False,
            metadata={"client_order_id": latest_event.client_order_id},
        )

    if latest_event.event_type in _TERMINAL_NEGATIVE_STATES:
        return ValidationResult(
            is_valid=True,
            reason_code="PRIOR_ATTEMPT_TERMINALLY_REJECTED",
            severity=SeverityLevel.WARNING,
            is_retryable=True,
            metadata={
                "client_order_id": latest_event.client_order_id,
                "prior_state": latest_event.event_type.value,
            },
        )

    # latest_event.event_type is SENT/PENDING/PARTIALLY_FILLED/FILLED/MODIFIED.
    if broker_ticket_still_open is True:
        return ValidationResult(
            is_valid=False,
            reason_code="DUPLICATE_ORDER_DETECTED",
            severity=SeverityLevel.CRITICAL,
            is_retryable=False,
            metadata={
                "client_order_id": latest_event.client_order_id,
                "prior_state": latest_event.event_type.value,
            },
        )

    return ValidationResult(
        is_valid=False,
        reason_code="AMBIGUOUS_SENT_STATE_MANUAL_REVIEW_REQUIRED",
        severity=SeverityLevel.CRITICAL,
        is_retryable=False,
        metadata={
            "client_order_id": latest_event.client_order_id,
            "prior_state": latest_event.event_type.value,
            "broker_ticket_still_open": broker_ticket_still_open,
        },
    )
