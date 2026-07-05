# tests/stress/

## Purpose

Reserved for load/volume tests (sustained high-frequency bar-close
cycles, large trade-ledger/order-events tables, concurrent-writer
contention against `storage/`'s single-writer SQLite model) — the
complementary half of `tests/chaos/`'s fault/disruption framing
(Phase 11e's test-suite reorganization, `docs/PRODUCTION_SPEC.md` §7).

## Non-Goal (this phase)

No stress tests exist yet. This directory is a deliberate, honest
placeholder — not filled with fabricated tests just to have content —
consistent with this project's established pattern of flagging genuinely
absent capabilities (see `docs/ARCHITECTURE_SUMMARY.md` §5) rather than
implying coverage that doesn't exist. Building real stress tests requires
first deciding what load profile matters for a single-instrument,
one-position-at-a-time system like this one, which hasn't been specified.

Excluded from `pyproject.toml`'s default `testpaths` (like
`tests/chaos/`) — run explicitly via `pytest tests/stress` once tests
exist here.
