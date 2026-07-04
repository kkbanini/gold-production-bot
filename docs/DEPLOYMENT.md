# Deployment Specification

## 1. Target Topology

The system is designed for a **single-tenant, single-account, single-host**
deployment, consistent with the single-writer architecture (ADR-0001) and the
embedded-storage decision (ADR-0003):

```
┌─────────────────────────────────────────────────────────────┐
│ Windows VPS (target: Windows Server 2022 or Windows 11 Pro)  │
│                                                               │
│  ┌──────────────────────┐      ┌─────────────────────────┐  │
│  │ MetaTrader 5 Terminal │◄────►│ gold_production_bot      │  │
│  │ (broker connection)   │ ctypes│ (Python 3.12+ process)  │  │
│  └──────────────────────┘  IPC  └─────────────────────────┘  │
│                                          │                    │
│                                          ▼                    │
│                                 ┌──────────────────┐          │
│                                 │ SQLite DB file     │          │
│                                 │ (local NTFS disk)  │          │
│                                 └──────────────────┘          │
└─────────────────────────────────────────────────────────────┘
```

- **Why Windows**: the `MetaTrader5` Python package requires the MT5 terminal
  process, which is Windows-native (ADR-0002). Linux/macOS deployment via Wine is
  explicitly unsupported due to unverified ctypes/DLL compatibility risk.
- **Why single-host, local disk**: SQLite/WAL requires reliable local `fsync` and
  file-locking semantics (ADR-0003); network-mounted (SMB/NFS) database paths are
  unsupported.
- **Process model**: one bot process per MT5 terminal instance per account. No
  process supervises multiple accounts; scaling to multiple accounts is a future
  multi-instance architecture, out of scope for the current ADRs.

## 2. Environment Promotion Strategy

Three environments, strictly ordered, no environment may be skipped:

| Environment | Purpose | Broker Connection | Promotion Gate In |
|---|---|---|---|
| `dev` | Local development, unit/integration tests, backtesting. | None (`backtester/` test double `BrokerGateway` only, ADR-0002) or MT5 demo account for manual smoke tests. | CI green (Ruff + Mypy + Pytest + coverage threshold). |
| `paper` | Continuous run against a live MT5 **demo** account, real-time market data, simulated fills. | MT5 demo account, `ENVIRONMENT_MODE=DEMO` (RR-012 guard). | Minimum observation window (see §4) with no `HIGH`+ severity incidents unresolved, plus a passing anchored-WFO report for any active parameter set (ADR-0004). |
| `live` | Real capital, MT5 **live** account. | MT5 live account, `ENVIRONMENT_MODE=LIVE`, explicit human-confirmed account allowlist (RR-012). | Explicit human sign-off referencing the paper-stage `PerformanceReport`, plus phase-approval gate per `docs/RUNBOOK.md` §7. |

`config/`'s `ENVIRONMENT_MODE` check (implemented Phase 1, `config/config_manager.py`)
and the broker-side account-allowlist cross-check (RR-012, pending Phase 2) are the
hard technical enforcement that prevents an environment-promotion mistake (e.g.
accidentally starting the bot against the live account while intending paper);
this is a startup-time FATAL-on-mismatch check, not a soft warning.

## 3. Configuration & Secrets

- All broker credentials, account IDs, and any third-party API keys (news
  calendar provider) are supplied via environment variables loaded from a
  local `.env` file, read exclusively by `config/` (RR-001, RQ-018).
- `.env` is never committed; `.env.template` (introduced in Phase 1) documents
  required keys with placeholder values only.
- Environment-specific values (`ENVIRONMENT_MODE`, account allowlist, risk
  limits) are environment-scoped — `dev`, `paper`, and `live` each have their
  own `.env`, never a shared file toggled by a flag. `.env.template` (Phase 1)
  documents the full set of required keys with empty placeholder values.

## 4. Rollback Procedure

1. Stop the bot process via the ordinary shutdown sequence
   (`docs/RUNBOOK.md` §2) — never a hard `kill`, so that broker-side protective
   orders and the storage layer are left in a consistent state.
2. Revert the deployed code to the last known-good tagged version (`VERSION` +
   git tag, once version control is initialized in a later phase).
3. Restart via the ordinary startup sequence (`docs/RUNBOOK.md` §1), whose
   broker-truth reconciliation step will surface any state gap introduced by the
   rollback for manual review before automated trading resumes.
4. A rollback that crosses a schema migration boundary (ADR-0003) requires the
   corresponding down-migration to be applied to the database before the older
   code version can start against it; rolling back code without rolling back
   schema is unsupported and will be blocked by the `schema_version` startup
   check.

## 5. Minimum Host Requirements (Reference)

| Resource | Minimum | Rationale |
|---|---|---|
| OS | Windows 10/11 or Windows Server 2019+ | MT5 terminal + Python 3.12 support |
| Python | 3.12+ | Language mandate; modern `typing`, performance improvements used by `indicators/`/`optimizer/` |
| Disk | Local SSD, low-latency `fsync` | SQLite WAL durability (ADR-0003) |
| Network | Low-latency, stable connection to broker's trade servers | Minimizes tick/order round-trip variance feeding into the slippage guard (RR-006) |
| Uptime monitoring | External heartbeat/watchdog | RR-017; a host outage must be observable outside the host itself |

## 6. Paper-to-Live Observation Window (Promotion Gate Detail)

The minimum `paper` observation window is defined quantitatively in
`docs/RESEARCH.md` (tied to achieving a statistically meaningful trade sample size
for the strategy's expected trade frequency) rather than a fixed calendar duration
in this document, since the correct duration is a function of trade frequency, not
wall-clock time alone.
