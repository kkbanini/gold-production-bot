# Operational Runbook

Operational procedures for the Gold (XAUUSD) production trading system. This
document is the authoritative reference for startup/shutdown sequencing, incident
response, backup/recovery, and routine maintenance. Every procedure references the
`docs/RISK_REGISTER.md` row(s) it responds to where applicable.

> Phase 0 status: this runbook specifies procedures against the architecture defined
> in ADR-0001–ADR-0004 and `docs/API_SPEC.md`. Procedures that reference concrete
> commands/scripts will be filled in with real invocations as each module lands
> (tracked in `docs/TRACEABILITY_MATRIX.md`); the *procedure* and its ordering is
> fully specified now and is not expected to change shape later.

## 1. Startup Sequence

Startup is strictly ordered; each step must succeed before the next begins. Any
failure halts startup (fail-closed, never fail-open into a partially-initialized
trading state).

1. **Load configuration** (`config/`). Validate presence of all required
   environment variables (broker credentials, `ENVIRONMENT_MODE`, account
   allowlist) via `ConfigManager.load()`. Missing/malformed config halts here
   — see RR-012.
2. **Open storage layer** (`storage/`). Run `PRAGMA integrity_check`. Verify
   `schema_version` matches the running code's expected migration head. Load the
   last persisted core-state snapshot and its `sequence_id`.
3. **Connect BrokerGateway** (`broker/`). Resolve `broker_utc_offset`. Assert the
   connected account ID/type matches the `config/`-declared allowlist and
   `ENVIRONMENT_MODE` (RR-012) — mismatch is FATAL, halt immediately.
4. **Replay unacknowledged events** since the last snapshot's `sequence_id`
   (`storage/`) to reconstruct in-memory core state exactly (ADR-0001, ADR-0003).
5. **Reconcile replayed state against broker truth**: compare replayed open
   positions/orders against `BrokerGateway.get_open_positions()` /
   broker-reported open orders. Any mismatch is a `HIGH`-severity `RiskBreach`
   (RR-008) and blocks automated trading pending manual reconciliation.
6. **Start EventBus subscribers**: `strategy/`, `execution/`, `news/`, `analytics/`
   handlers register with the `EventBus`.
7. **Start producers**: MT5 tick/bar feed, news calendar poller, optimizer
   parameter-update watcher.
8. **Emit `INFO`-severity startup-complete event** with the resolved config
   summary (mode, account, active parameter set versions) to the operational log.

## 2. Shutdown Sequence

1. Stop producers first (tick feed, news poller, optimizer watcher) so no new
   events enter the queue.
2. Drain the `EventBus`: allow in-flight events to finish processing; do not
   accept new `OrderRequestEvent`s once drain begins.
3. Persist a core-state snapshot (`storage/`) tagged with the last processed
   `sequence_id`.
4. Disconnect `BrokerGateway` cleanly.
5. Checkpoint the WAL (`PRAGMA wal_checkpoint(TRUNCATE)`) and close the storage
   connection.
6. Emit `INFO`-severity shutdown-complete event.

**Open positions are never force-closed on ordinary shutdown.** Ordinary shutdown
assumes broker-side stop-loss/take-profit protection remains active
independent of the bot process (RR-017); only a `CRITICAL`/`FATAL` de-risk
procedure (§3) force-closes positions.

## 3. Incident Response by Severity

| Severity | Automated Response | Human Action Required |
|---|---|---|
| `INFO` | Log only. | None. |
| `LOW` | Log + append to daily digest. | Review in next-business-day standup-equivalent (self-review for a solo operator). |
| `MEDIUM` | Log + immediate notification (see §5 Alerting). | Review within 4h same trading day; confirm no escalation needed. |
| `HIGH` | Log + immediate notification + pause new position entries for the affected symbol (existing positions retain broker-side protection). | Review within 1h; manually resume entries once root cause is understood. |
| `CRITICAL` | Log + immediate notification + autonomous de-risk: cancel all pending orders, do not open new positions; existing positions retain stop-loss but no new risk is added. | Immediate review required before any manual or automated resumption of trading. |
| `FATAL` | Log + immediate notification + full trading halt: disconnect new order submission capability entirely; existing broker-side protective orders remain (never cancelled automatically). | Manual root-cause review and explicit restart required; system does not self-restart. |

### 3.1 Specific Incident Procedures

- **Broker disconnect (RR-002)**: `broker/` retries with exponential backoff
  automatically. If backoff budget exhausts, treat as `CRITICAL`: verify via the
  MT5 terminal UI directly whether positions remain protected by broker-side
  stops before taking any other action.
- **Event log / derived-state divergence (RR-008)**: treat as `FATAL`. Do not
  attempt automated repair. Preserve the database file (copy, do not modify) for
  forensic replay, then rebuild derived tables via full event-log replay from
  genesis in a separate recovery instance before resuming.
- **Wrong-account connection (RR-012)**: treat as `FATAL` at startup — the system
  will already have refused to proceed automatically (§1 step 3); no live/paper
  cross-contamination is possible if startup sequencing was followed.
- **Optimizer promotion-gate failure (RR-004, RR-010)**: not an incident — this is
  the optimizer working as designed (ADR-0004). Logged at `INFO`/`LOW`; the
  currently-active parameter set simply remains active another week.
- **Margin level breach (RR-011)**: treat as `CRITICAL`. Autonomous de-risk
  triggers per the table above; manually verify margin recovery before permitting
  new entries.

## 4. Backups & Disaster Recovery

- **Cadence**: `VACUUM INTO` a timestamped snapshot of the SQLite database daily
  after market close, retained on a 30-day rolling window plus one permanent
  monthly snapshot.
- **Verification**: each backup snapshot is opened read-only and
  `PRAGMA integrity_check`'d immediately after creation; a failed check pages the
  operator (this is itself a `HIGH`-severity event).
- **Restore procedure**: stop the bot process (§2, or force-stop if already down),
  replace the live database file with the chosen snapshot, restart (§1) — startup's
  broker-truth reconciliation step (§1.5) will surface any gap between the
  restored snapshot and actual broker state for manual review.
- **Off-host copy**: backup snapshots are additionally copied off the VPS host on
  the same cadence (destination specified in `docs/DEPLOYMENT.md`) so host-level
  failure (RR-017) does not also destroy the audit trail.

## 5. Alerting

Notification channel and routing are finalized in a later phase (tracked as a
Phase 2+ config item); this runbook specifies *what* must trigger a notification,
not yet *which* transport carries it. At minimum, `MEDIUM` severity and above must
reach the operator outside of only the application log (i.e., a push
notification/message, not solely a file on disk that requires manual tailing).

## 6. Routine Maintenance

| Task | Cadence | Procedure Reference |
|---|---|---|
| WAL checkpoint | Daily, off-hours | §4-adjacent; `PRAGMA wal_checkpoint(TRUNCATE)` |
| Backup verification | Daily (automatic, part of backup job) | §4 |
| Risk Register review | End of each delivery phase; quarterly thereafter | `docs/RISK_REGISTER.md` §Review Cadence |
| Dependency vulnerability scan | Weekly (CI scheduled job, once `.github/workflows/` hardened in Phase 1) | RR-016 |
| Anchored WFO re-optimization | Weekly, weekend (market-closed window) | ADR-0004, `docs/RESEARCH.md` |
| Traceability Matrix audit | End of each delivery phase | `docs/TRACEABILITY_MATRIX.md` |

## 7. Phase Approval Gate

Per the System Meta-Directive, no phase's code is considered mergeable until:
Ruff → Mypy → Pytest all pass, documentation (ADR/Traceability/Changelog) is
updated, SemVer is bumped correctly, and explicit human approval is recorded. This
runbook's procedures are the checklist a phase approver should walk before
sign-off, in addition to the automated CI gate.
