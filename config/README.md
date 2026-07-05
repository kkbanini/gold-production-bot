# config/

## Responsibility

Sole owner of environment/secret loading, validation, and log redaction.
Resolves `ENVIRONMENT_MODE` (`DEMO`/`LIVE`), MT5 broker credentials, the
economic calendar API key, and the strategy magic number from environment
variables / a local `.env` file via `ConfigManager` (`config_manager.py`).
No other module reads `os.environ` directly for a trading-relevant value.

## Implementation

- `config_manager.py`:
  - `ConfigValidator` (added Phase 11a, `docs/PRODUCTION_SPEC.md` §1) —
    inspects raw environment values for presence (`check_presence`),
    placeholder/default-value leakage (`check_no_placeholder_leak` — a
    curated substring list like `changeme`/`your_`/`replace_me`, not a
    generic weak-value heuristic, to avoid false positives blocking
    legitimate startup), and syntactic validity
    (`check_environment_mode`/`check_integer`). `validate()` runs all
    three in order and returns the parsed numeric/enum fields.
  - `ConfigManager.load()` calls `ConfigValidator().validate()` and raises
    `ConfigurationError` — never returns a partially-populated config — on
    any failure. Per `docs/PRODUCTION_SPEC.md` §1, this uncaught exception
    *is* the required "fatal application panic": it propagates through
    `container.ApplicationContainer.build()`, halting process startup
    (RQ-018, RR-012).
- `secret_redaction.py` (added Phase 11a, `docs/PRODUCTION_SPEC.md` §1) —
  `SecretRedactingFilter`, a `logging.Filter` that replaces every
  occurrence of a configured secret value with a fixed marker in the log
  message before any handler sees it. Attached to the root logger by
  `container.ApplicationContainer.build()` using the loaded config's
  password/API-key fields, so no code elsewhere needs to remember to
  redact anything.
- `.env.template` (repo root) — documents every required key with empty
  placeholder values; never populated with real credentials.

## Depends On

Nothing internal. External: `python-dotenv`, environment variables, `.env`
file (never committed).

## Depended On By

`container.py`'s `ApplicationContainer` (config loading + secret redaction
setup, Phase 11a), `broker/` (credentials, `ENVIRONMENT_MODE` for the
RR-012 startup check), `news/` (calendar provider API key).

## Governing Docs

`docs/PRODUCTION_SPEC.md` §1 (secrets management & boot validation — the
authority for this phase's additions). ADR references: none yet dedicated
otherwise (cross-cutting). See `docs/RISK_REGISTER.md` RR-001 (secret
hygiene) and RR-012 (wrong-account guard), `docs/DEPLOYMENT.md` §2–§3.

## Non-Goals (This Phase)

The broker-side cross-check of `ENVIRONMENT_MODE` against the actual
connected MT5 account's live/demo status, and an explicit account-ID
allowlist, remain open gaps (`docs/ARCHITECTURE_SUMMARY.md` §5). The
placeholder-leak check (`check_no_placeholder_leak`) is a curated list of
common template markers, not an exhaustive secret-strength/entropy
analysis — a real credential that happens to contain one of those
substrings would false-positive (unlikely in practice, but a known
limitation of a substring-based heuristic).
