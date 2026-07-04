# config/

## Responsibility

Sole owner of environment/secret loading and validation. Resolves
`ENVIRONMENT_MODE` (`DEMO`/`LIVE`), MT5 broker credentials, the economic
calendar API key, and the strategy magic number from environment variables / a
local `.env` file via `ConfigManager` (`config_manager.py`). No other module
reads `os.environ` directly for a trading-relevant value.

## Implementation

- `config_manager.py` — `ConfigManager`, a frozen dataclass produced by
  `ConfigManager.load()`. Loads `.env` via `python-dotenv` (without overriding
  variables already present in the process environment), asserts every
  required key in `REQUIRED_ENV_VARS` is present, validates `ENVIRONMENT_MODE`
  against `VALID_ENVIRONMENT_MODES`, and type-coerces `MT5_LOGIN` /
  `STRATEGY_MAGIC_NUMBER` to `int`. Raises `ConfigurationError` — never
  returns a partially-populated config — on any validation failure, so the
  system cannot boot with an incomplete or ambiguous configuration (RQ-018,
  RR-012).
- `.env.template` (repo root) — documents every required key with empty
  placeholder values; never populated with real credentials.

## Depends On

Nothing internal. External: `python-dotenv`, environment variables, `.env`
file (never committed).

## Depended On By

`broker/` (credentials, `ENVIRONMENT_MODE` for the RR-012 startup check),
`execution/` (risk-limit constants, future phase), `news/` (calendar provider
API key), `.github/workflows/` (dependency scan indirectly touches this
module's declared dependencies).

## Governing Docs

ADR references: none yet dedicated (cross-cutting). See `docs/RISK_REGISTER.md`
RR-001 (secret hygiene) and RR-012 (wrong-account guard), `docs/DEPLOYMENT.md`
§2–§3.

## Non-Goals (This Phase)

Phase 1's scope, per the approved phase directive, is `ConfigManager`'s
environment validation logic plus Ruff/Mypy/Pytest gating — it did not include
authoring `tests/config/` unit test files, so `tests/` remains structurally
empty this phase (Pytest run confirms clean collection of zero tests). The
three validation paths (missing keys, invalid `ENVIRONMENT_MODE`, valid load)
were verified ad hoc against the running interpreter instead; see Phase 1's
`CHANGELOG.md` entry. Formal `tests/config/test_config_manager.py` covering
RQ-018 is expected in the phase that first adds real `tests/` content. The
broker-side cross-check of `ENVIRONMENT_MODE` against the actual connected MT5
account's live/demo status, and an explicit account-ID allowlist, remain
`broker/` responsibilities landing in Phase 2 (ADR-0002).
