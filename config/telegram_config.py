"""Telegram status-bot configuration (`monitoring/telegram_bot.py`'s
`/check` command), loaded from environment variables.

Sole owner of the `TELEGRAM_*` environment variables — same rule
`config/config_manager.py` states for the credentials it owns.

Unlike `MT5_LOGIN` et al, `main.py`'s bar-close loop never depends on
this: it is read only by `monitoring/telegram_bot.py`'s own standalone
process, so a missing/invalid value never blocks the trading loop from
booting — it only blocks that separate, optional monitoring script.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from config.config_manager import ConfigurationError


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    """Validated, typed snapshot of the Telegram status bot's
    configuration."""

    bot_token: str
    allowed_chat_ids: tuple[int, ...]

    @classmethod
    def from_env(cls, env_file: str | None = None) -> "TelegramConfig":
        """Load and validate `TELEGRAM_*` environment variables.

        Populates `os.environ` from `env_file` (defaults to `.env` in the
        current working directory) the same way `ConfigManager.load()`
        does — `monitoring/telegram_bot.py` runs as its own standalone
        process, so it never goes through `container.py`'s boot sequence
        and must load `.env` itself.
        """
        load_dotenv(dotenv_path=env_file, override=False)

        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not bot_token:
            raise ConfigurationError(
                "TELEGRAM_BOT_TOKEN must be set to run monitoring/telegram_bot.py "
                "(see .env.template)."
            )

        raw_chat_ids = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
        if not raw_chat_ids:
            raise ConfigurationError(
                "TELEGRAM_ALLOWED_CHAT_ID must be set to run monitoring/telegram_bot.py "
                "(comma-separated chat IDs permitted to use /check; see .env.template)."
            )
        try:
            allowed_chat_ids = tuple(
                int(chat_id.strip()) for chat_id in raw_chat_ids.split(",") if chat_id.strip()
            )
        except ValueError as exc:
            raise ConfigurationError(
                f"TELEGRAM_ALLOWED_CHAT_ID={raw_chat_ids!r} must be a comma-separated "
                "list of integers."
            ) from exc
        if not allowed_chat_ids:
            raise ConfigurationError("TELEGRAM_ALLOWED_CHAT_ID must contain at least one chat ID.")

        return cls(bot_token=bot_token, allowed_chat_ids=allowed_chat_ids)
