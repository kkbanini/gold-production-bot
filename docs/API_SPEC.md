# API Specification & Interface Type Hinting Contracts

| Field | Value |
|---|---|
| Status | Normative — Phase 0 baseline |
| Version | 0.1.0 |
| Governs | ADR-0001, ADR-0002, ADR-0003 |

This document is the **canonical, versioned interface contract** for every
cross-module boundary in the system. Per `CHANGELOG.md`'s versioning policy, any
change to a shape defined here is a MAJOR (breaking) or MINOR (additive) version
event.

This is a **type-hinting contract specification**, not executable trading logic — no
function bodies contain business logic in this phase. `...` denotes "implemented in
its owning module's later phase," never "left unspecified."

---

## 1. Core Value Types (`shared/types.py` — introduced when first consumed)

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum, auto
from typing import Protocol, Sequence, runtime_checkable


class Symbol(str, Enum):
    """Instrument universe. Single-instrument in Phase 0-era scope."""
    XAUUSD = "XAUUSD"


class TimeFrame(str, Enum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class Tick:
    """A single quote update. All timestamps are UTC-normalized at the
    BrokerGateway boundary per ADR-0002; no consumer performs timezone math."""
    symbol: Symbol
    bid: Decimal
    ask: Decimal
    source_timestamp_utc: datetime
    received_timestamp_utc: datetime

    @property
    def mid(self) -> Decimal:
        ...

    @property
    def spread(self) -> Decimal:
        ...


@dataclass(frozen=True, slots=True)
class Bar:
    """A single closed OHLCV bar. `close_time_utc` marks the bar's close,
    not open — signal evaluation is anchored to bar close per ADR-0001."""
    symbol: Symbol
    timeframe: TimeFrame
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    open_time_utc: datetime
    close_time_utc: datetime


@dataclass(frozen=True, slots=True)
class Signal:
    """Output of strategy/ — a directional trigger, not yet an order.
    Carries provenance (which filter/trigger combination fired) for
    docs/TRACEABILITY_MATRIX.md audit and analytics/ attribution."""
    symbol: Symbol
    side: OrderSide
    strength: Decimal  # normalized [0, 1] confidence/edge score
    strategy_id: str
    trigger_name: str
    generated_at_utc: datetime
    parameter_set_version: str  # ties signal to the optimizer/ parameter set active at generation


@dataclass(frozen=True, slots=True)
class RiskGateDecision:
    """Output of execution/'s pre-trade risk gate. A Signal never becomes
    an Order without passing through this and being APPROVED."""
    approved: bool
    reason_code: str
    max_position_size_lots: Decimal
    stop_loss_price: Decimal | None
    take_profit_price: Decimal | None


@dataclass(frozen=True, slots=True)
class Order:
    symbol: Symbol
    side: OrderSide
    order_type: OrderType
    volume_lots: Decimal
    requested_price: Decimal | None  # None for MARKET
    stop_loss_price: Decimal | None
    take_profit_price: Decimal | None
    client_order_id: str  # generated locally, idempotency key for retries
    status: OrderStatus
    broker_order_id: str | None
    created_at_utc: datetime


@dataclass(frozen=True, slots=True)
class Fill:
    client_order_id: str
    broker_order_id: str
    broker_deal_id: str
    symbol: Symbol
    side: OrderSide
    filled_volume_lots: Decimal
    fill_price: Decimal
    slippage_points: Decimal  # signed: requested vs. filled, see execution/ slippage guard
    commission: Decimal
    swap: Decimal
    filled_at_utc: datetime


@dataclass(frozen=True, slots=True)
class Position:
    symbol: Symbol
    side: OrderSide
    volume_lots: Decimal
    average_open_price: Decimal
    stop_loss_price: Decimal | None
    take_profit_price: Decimal | None
    unrealized_pnl: Decimal
    status: PositionStatus
    opened_at_utc: datetime
    strategy_id: str
    parameter_set_version: str


@dataclass(frozen=True, slots=True)
class AccountState:
    balance: Decimal
    equity: Decimal
    margin_used: Decimal
    margin_free: Decimal
    margin_level_pct: Decimal | None
    as_of_utc: datetime
```

## 2. Event Envelope (ADR-0001)

```python
@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """Every EventBus message is wrapped in this envelope. sequence_id is
    monotonic and assigned by storage/'s event log (ADR-0003), never by
    the producer, so ordering is defined by the single writer, not by
    producer clock skew."""
    sequence_id: int
    event_type: str
    source_timestamp_utc: datetime
    received_timestamp_utc: datetime
    payload: (
        Tick
        | Bar
        | Signal
        | RiskGateDecision
        | Order
        | Fill
        | "ParameterUpdate"
        | "NewsWindow"
        | "RiskBreach"
    )


@dataclass(frozen=True, slots=True)
class ParameterUpdate:
    """Emitted exclusively by optimizer/ per ADR-0004. strategy_id ties
    this to the strategy/ component whose parameters are being replaced."""
    strategy_id: str
    parameter_set_version: str
    parameters: dict[str, Decimal | int | str]
    anchored_wfo_report_id: str  # FK to optimizer/'s persisted WFO report; mandatory, never None


@dataclass(frozen=True, slots=True)
class NewsWindow:
    """Emitted by news/. Consumed by execution/'s pre-trade risk gate to
    block/derisk around high-impact releases."""
    symbol_impact: Symbol
    event_name: str
    impact_level: str  # "LOW" | "MEDIUM" | "HIGH" — economic calendar taxonomy, not RiskRegister severity
    scheduled_at_utc: datetime
    blackout_before: timedelta
    blackout_after: timedelta


@dataclass(frozen=True, slots=True)
class RiskBreach:
    """Emitted by execution/ or the core loop when a docs/RISK_REGISTER.md
    guard trips. severity is the RiskRegister taxonomy, INFO..FATAL."""
    severity: str
    risk_id: str  # FK into docs/RISK_REGISTER.md row id, e.g. "RR-014"
    message: str
    occurred_at_utc: datetime


@runtime_checkable
class EventBus(Protocol):
    """Single in-process pub/sub bus. Publish is append-only; the core
    loop is the only subscriber permitted to cause a state mutation
    (ADR-0001)."""

    def publish(self, payload: EventEnvelope) -> None: ...

    def subscribe(
        self, event_type: str, handler: "EventHandler"
    ) -> "Subscription": ...


@runtime_checkable
class EventHandler(Protocol):
    def __call__(self, envelope: EventEnvelope) -> Sequence[EventEnvelope]:
        """Pure with respect to core state: reads a state snapshot,
        returns follow-on events. Never mutates shared state directly."""
        ...


@runtime_checkable
class Subscription(Protocol):
    def unsubscribe(self) -> None: ...
```

## 3. BrokerGateway Port (ADR-0002)

```python
class BrokerConnectionError(Exception): ...
class BrokerOrderRejectedError(Exception): ...
class BrokerTimeoutError(Exception): ...
class BrokerSymbolUnavailableError(Exception): ...


@runtime_checkable
class BrokerGateway(Protocol):
    """Sole abstraction over MT5 (production adapter, broker/mt5_gateway.py)
    or historical replay (backtester/ test double). No caller-side branching
    on broker vs. backtest — both satisfy this exact Protocol."""

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...

    @property
    def broker_utc_offset(self) -> timedelta:
        """Resolved once at connect() and re-validated on a fixed interval
        to detect broker-side DST transitions, per ADR-0002."""
        ...

    def get_latest_tick(self, symbol: Symbol) -> Tick: ...

    def get_bars(
        self, symbol: Symbol, timeframe: TimeFrame, count: int
    ) -> Sequence[Bar]: ...

    def submit_order(self, order: Order) -> Order:
        """Returns the Order with status/broker_order_id populated.
        Raises BrokerOrderRejectedError (not a bool/None) on rejection."""
        ...

    def cancel_order(self, client_order_id: str) -> None: ...

    def get_open_positions(self, symbol: Symbol | None = None) -> Sequence[Position]: ...

    def close_position(
        self, symbol: Symbol, volume_lots: Decimal | None = None
    ) -> Fill:
        """volume_lots=None closes the full position; a Decimal value
        performs a partial closure (execution/'s partial-closure contract)."""
        ...

    def get_account_state(self) -> AccountState: ...
```

## 4. Storage Repositories (ADR-0003)

```python
@runtime_checkable
class EventLogRepository(Protocol):
    def append(self, envelope: EventEnvelope) -> int:
        """Returns the assigned monotonic sequence_id."""
        ...

    def replay_since(self, sequence_id: int) -> Sequence[EventEnvelope]: ...


@runtime_checkable
class OrderRepository(Protocol):
    def upsert(self, order: Order) -> None: ...
    def get_by_client_order_id(self, client_order_id: str) -> Order | None: ...
    def get_open_orders(self) -> Sequence[Order]: ...


@runtime_checkable
class PositionRepository(Protocol):
    def upsert(self, position: Position) -> None: ...
    def get_open_positions(self) -> Sequence[Position]: ...


@runtime_checkable
class EquityCurveRepository(Protocol):
    def record_mark(self, account_state: AccountState) -> None: ...
    def get_curve(
        self, start_utc: datetime, end_utc: datetime
    ) -> Sequence[AccountState]: ...


@runtime_checkable
class ParameterHistoryRepository(Protocol):
    def record(self, update: ParameterUpdate) -> None: ...
    def get_active(self, strategy_id: str) -> ParameterUpdate | None: ...
```

## 5. Analytics Contracts (consumed by `analytics/`)

```python
@dataclass(frozen=True, slots=True)
class PerformanceReport:
    period_start_utc: datetime
    period_end_utc: datetime
    sharpe_ratio: Decimal
    sortino_ratio: Decimal
    mar_ratio: Decimal
    max_drawdown_pct: Decimal
    max_drawdown_duration: timedelta
    win_rate_pct: Decimal
    profit_factor: Decimal
    total_trades: int
    deflated_sharpe_ratio: Decimal | None  # populated for optimizer/ WFO reports only, per ADR-0004
```

## 6. Module Ownership Matrix

| Contract | Owning Module | Consumers |
|---|---|---|
| `Tick`, `Bar` | `broker/` | `strategy/`, `indicators/`, `backtester/` |
| `Signal` | `strategy/` | `execution/`, `analytics/` |
| `RiskGateDecision`, `Order`, `Fill` | `execution/` | `storage/`, `analytics/` |
| `Position`, `AccountState` | `broker/` | `execution/`, `analytics/`, `storage/` |
| `EventEnvelope`, `EventBus`, `EventHandler` | core (`main`, introduced Phase 2) | all modules |
| `BrokerGateway` | `broker/` (prod), `backtester/` (test double) | `strategy/`, `execution/` |
| `*Repository` | `storage/` | `execution/`, `analytics/`, `optimizer/` |
| `ParameterUpdate` | `optimizer/` | `strategy/`, `storage/` |
| `NewsWindow` | `news/` | `execution/` |
| `RiskBreach` | `execution/`, core | `storage/`, `docs/RUNBOOK.md`-driven alerting (later phase) |
| `PerformanceReport` | `analytics/` | `optimizer/` (ADR-0004 gate), reporting |

## 7. Contract Change Policy

1. Adding an optional field with a default → MINOR.
2. Adding a new Protocol method, new dataclass, new enum member → MINOR.
3. Removing/renaming a field, changing a field's type, changing a Protocol
   method's signature, changing an enum member's value → MAJOR, requires a new
   ADR entry referencing this document.
4. Every change updates this document, `CHANGELOG.md`, and
   `docs/TRACEABILITY_MATRIX.md` in the same phase commit.
