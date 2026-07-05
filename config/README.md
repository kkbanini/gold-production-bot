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
- `calendar_config.py` (Phase 11b, `docs/PRODUCTION_SPEC.md` §2) —
  `CalendarConfig.from_env()` loads the optional `CALENDAR_*` env vars
  (`CALENDAR_PROVIDER_PRIORITY`, `CALENDAR_TIMEOUT_MS`,
  `CALENDAR_RATE_LIMIT_PER_MIN`, `CALENDAR_<PROVIDER>_BASE_URL`,
  `CALENDAR_OFFLINE_SNAPSHOT_PATH`). Defaults to a safe `offline_snapshot`-only
  chain requiring no additional configuration; raises `ConfigurationError`
  (the same fail-closed exception `ConfigManager` raises) if an unknown
  provider name is listed, a network provider is listed without its base
  URL, or a numeric field isn't a valid integer. Sole owner of the
  `CALENDAR_*` variables, same rule as `config_manager.py`'s required keys.
- `feature_flags.py` (Phase 11d, `docs/PRODUCTION_SPEC.md` §6) —
  `FeatureFlags.from_env()` loads the optional `FLAG_*` env vars (today:
  `FLAG_LIQUIDATE_ON_HARD_LOCK`, accepting `true`/`false`/`1`/`0`/`yes`/`no`/
  `on`/`off` case-insensitively); an unrecognized value raises
  `ConfigurationError`. Defaults `liquidate_on_hard_lock` to `False` — the
  safer, capital-preserving choice when a deployer hasn't made an explicit
  choice. `FeatureFlagManager` wraps a `FeatureFlags` snapshot as the
  single place `risk.drawdown_fsm.decide_hard_lock_response()`'s caller
  reads the flag from. Sole owner of the `FLAG_*` variables.

## Depends On

Nothing internal. External: `python-dotenv`, environment variables, `.env`
file (never committed).

## Depended On By

`container.py`'s `ApplicationContainer` (config loading + secret redaction
setup, Phase 11a; `CalendarConfig.from_env()` feeding
`news.calendar_provider.build_calendar_provider_chain()`, Phase 11b;
`FeatureFlagManager` construction, Phase 11d), `broker/` (credentials,
`ENVIRONMENT_MODE` for the RR-012 startup check), `news/` (calendar
provider API key, and `calendar_config.py`'s `CalendarConfig` for
`calendar_provider.py`'s provider chain), `main.py` (Phase 11d:
`run_bar_close_cycle()` takes `container.feature_flags` and passes
`.liquidate_on_hard_lock` to `risk.drawdown_fsm.decide_hard_lock_response()`).

## Governing Docs

`docs/PRODUCTION_SPEC.md` §1 (secrets management & boot validation), §2
(`calendar_config.py`'s configuration matrix, Phase 11b), and §6
(`feature_flags.py`'s `config.flags.liquidate_on_hard_lock`, Phase 11d).
ADR references: none yet dedicated otherwise (cross-cutting). See
`docs/RISK_REGISTER.md` RR-001 (secret hygiene) and RR-012 (wrong-account
guard), `docs/DEPLOYMENT.md` §2–§3.

## Non-Goals (This Phase)

The broker-side cross-check of `ENVIRONMENT_MODE` against the actual
connected MT5 account's live/demo status, and an explicit account-ID
allowlist, remain open gaps (`docs/ARCHITECTURE_SUMMARY.md` §5). The
placeholder-leak check (`check_no_placeholder_leak`) is a curated list of
common template markers, not an exhaustive secret-strength/entropy
analysis — a real credential that happens to contain one of those
substrings would false-positive (unlikely in practice, but a known
limitation of a substring-based heuristic).
