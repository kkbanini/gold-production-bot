# config/

## Responsibility

Sole owner of environment/secret loading and validation. Resolves `TRADING_MODE`
(`dev`/`paper`/`live`), broker credentials, account allowlist, and risk-limit
constants from environment variables / a local `.env` file. No other module reads
`os.environ` directly for a trading-relevant value.

## Depends On

Nothing internal. External: environment variables, `.env` file (never committed).

## Depended On By

`broker/` (credentials, account allowlist for RR-012 startup check), `execution/`
(risk-limit constants), `news/` (calendar provider API key), `.github/workflows/`
(dependency scan indirectly touches this module's declared dependencies).

## Governing Docs

ADR references: none yet dedicated (cross-cutting). See `docs/RISK_REGISTER.md`
RR-001 (secret hygiene) and RR-012 (wrong-account guard), `docs/DEPLOYMENT.md` §3.

## Non-Goals (This Phase)

No code exists yet. Phase 0 defines only this contract stub; `.env.example`,
the config-loading implementation, and `tests/config/test_no_secrets_in_repo.py`
(RQ-018) land in Phase 1.
