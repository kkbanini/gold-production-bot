# tests/

## Responsibility

Unit, integration, and FSM simulation tests for every module, mirroring the
top-level module layout (`tests/broker/`, `tests/storage/`, `tests/strategy/`,
etc., created as each owning module lands). Every requirement row in
`docs/TRACEABILITY_MATRIX.md` names its concrete test file here; a module is not
considered `IMPLEMENTED` in that matrix until its corresponding test exists and
passes in CI (`.github/workflows/ci.yml`).

Test categories:

- **Unit tests** — pure-function correctness (`indicators/`, `strategy/`
  determinism, `docs/RESEARCH.md` formula reference values).
- **Integration tests** — cross-module contracts honored (`broker/` import
  boundary, `storage/` transactional consistency, `execution/` idempotency).
- **FSM simulation tests** — drive the Order/Position/Parameter lifecycles
  (`docs/diagrams/fsm_diagram.puml`) through their full transition graph,
  including crash-recovery replay (RQ-006) and single-writer invariant
  assertions (RQ-003, RQ-004).

## Depends On

Every module under test. No module depends on `tests/`.

## Governing Docs

`docs/TRACEABILITY_MATRIX.md` (requirement → test mapping, authoritative).
`.github/workflows/ci.yml` (execution + coverage gate, `--cov-fail-under=90`).

## Non-Goals (This Phase)

No test files exist yet — there is no executable code to test in Phase 0. Test
files are added in the same phase as the module they verify, never retroactively
batched at the end.
