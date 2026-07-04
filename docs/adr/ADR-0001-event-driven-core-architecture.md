# ADR-0001: Event-Driven, Single-Writer Core Architecture

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-07-04 |
| Deciders | Principal Quantitative Engineering |
| Supersedes | — |
| Superseded by | — |
| Related | ADR-0002, ADR-0003, ADR-0004 |

## Context

The system must ingest a continuous stream of MT5 tick/bar data, evaluate strategy
signal triggers, apply risk gates, route orders, and persist state — all under a hard
latency budget appropriate for a swing/intraday XAUUSD system (sub-second reaction to
tick events, not high-frequency/sub-millisecond). The system also has a fully
asynchronous side-channel: the weekend offline optimizer (`optimizer/`) which mutates
strategy parameters that the live core must pick up safely without corrupting an
in-flight order lifecycle.

Two categories of failure must be architecturally impossible, not just tested against:

1. **Torn state**: a partially-applied strategy parameter set or a partially-written
   order record observed by a concurrent reader.
2. **Re-entrant order corruption**: two threads independently deciding to open/close
   the same logical position because of a shared-mutable-state race.

A naive multi-threaded design (one thread per concern: data feed, signal evaluation,
execution, persistence) exposes both failure classes through the Python GIL's
cooperative-but-not-atomic scheduling around compound operations (read-modify-write on
shared dicts/objects).

## Decision

Adopt a **single-writer, event-driven architecture** built around one in-process
`EventBus` (see `docs/API_SPEC.md` for the `EventBus` contract) and a single core
event loop that owns all mutable trading state (open positions, pending orders,
strategy parameter set, risk budget counters).

Concretely:

1. **One core loop, one writer.** All mutations to trading state occur on a single
   logical thread of control (an `asyncio` event loop in the reference
   implementation). Producers (MT5 tick feed, news calendar poller, optimizer
   parameter-update watcher) run on separate threads/processes and communicate
   **only** by publishing immutable event objects onto the `EventBus`; they never
   mutate core state directly.
2. **Events are immutable, timestamped, and typed.** Every event (`TickEvent`,
   `BarClosedEvent`, `SignalEvent`, `OrderRequestEvent`, `FillEvent`,
   `ParameterUpdateEvent`, `NewsWindowEvent`, `RiskBreachEvent`) is a frozen
   `dataclass` carrying a monotonic `sequence_id`, a `source_timestamp_utc`, and a
   `received_timestamp_utc` (see ADR-0002 for the server-time vs. wall-clock
   distinction this enables).
3. **Handlers are pure with respect to core state.** Each event handler reads the
   current core state snapshot, computes a decision, and returns a list of
   *follow-on* events (e.g. a `SignalEvent` handler returns zero or one
   `OrderRequestEvent`). The core loop applies the resulting state transition and
   persists it (via `storage/`) **before** the next event is dequeued. This makes
   the system replayable: the ordered event log is the source of truth, and core
   state is a left-fold over it.
4. **Backpressure over concurrency for correctness-critical paths.** The
   `OrderRequestEvent → FillEvent` path is processed strictly sequentially per
   symbol. Concurrency is only permitted across *independent* symbols in a future
   multi-instrument phase (out of scope for this phase; XAUUSD only for now).
5. **The optimizer is a producer, never a mutator.** The weekend optimizer
   (`optimizer/`, ADR-0004) computes candidate parameter sets out-of-process and
   publishes a single `ParameterUpdateEvent`. The core loop applies it atomically
   between bar closes, never mid-signal-evaluation.

## Consequences

### Positive

- Eliminates an entire class of race conditions by construction rather than by
  discipline (no shared-mutable-state access outside the single writer).
- The ordered, persisted event log (via `storage/`, ADR-0003) gives the system a
  free audit trail and a deterministic replay/backtest harness: the event-driven
  backtester (`backtester/`) can consume the exact same event types the live system
  does, satisfying "vectorized and event-driven validation" from the top-level spec.
- Crash recovery reduces to: reload last persisted core-state snapshot + replay
  unacknowledged events since that snapshot's `sequence_id`.

### Negative / Accepted Trade-offs

- Single-writer serialization caps theoretical throughput; explicitly accepted
  because XAUUSD intraday/swing signal evaluation does not require sub-millisecond
  concurrency, and correctness dominates raw throughput for this risk profile.
- Requires strict discipline that no module reaches into another module's state
  directly (enforced at review time and, from Phase 2 onward, via `mypy` module
  boundary typing and import-linter rules to be introduced when code lands).
- Introduces an explicit event-schema versioning burden: any change to an event
  dataclass is an API-Spec-governed change (MAJOR/MINOR per `CHANGELOG.md` policy).

## Alternatives Considered

| Alternative | Rejected Because |
|---|---|
| Multi-threaded shared-state model (locks around a global `PositionManager`) | Lock discipline errors are the single largest source of live-trading-system defects industry-wide; correctness cannot be statically verified. |
| Multi-process microservices (gRPC/message-queue per module) | Operational complexity (deployment, IPC failure modes, distributed transaction semantics) is unjustified for a single-instrument, single-account system; revisit only if multi-instrument/multi-account scaling is required. |
| Fully synchronous single-threaded polling loop (no event bus) | Couples the MT5 blocking I/O calls to signal evaluation timing; cannot cleanly integrate the weekend optimizer or news-calendar side-channel without ad hoc polling flags. |

## Compliance / Verification

- Enforced structurally: `EventBus` and event dataclasses are the only cross-module
  communication path (see `docs/API_SPEC.md`).
- Verified in `tests/` via FSM simulation tests once execution logic lands (Phase 5+),
  asserting no core-state field is written from any stack frame outside the core
  loop's `apply_event` dispatcher.
- Diagrammed in `docs/diagrams/component_diagram.puml` and
  `docs/diagrams/sequence_diagram.puml`.
