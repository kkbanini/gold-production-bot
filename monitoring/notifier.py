"""Fire-and-forget Telegram push notifications for trading events.

The inverse direction of `monitoring/telegram_bot.py`: that module is a
separate *pull* process (the operator asks `/check` and gets an answer);
this one is a tiny *push* utility `main.py`'s own process calls at the
moment something noteworthy happens (an entry filled, a short-term close
reconciled, the process about to die on an unhandled error) — so the
operator hears about it immediately instead of only when they think to
poll.

Deliberately incapable of breaking trading:

- `build_notifier_from_env()` returns `None` when `TELEGRAM_BOT_TOKEN`/
  `TELEGRAM_ALLOWED_CHAT_ID` aren't configured — notifications are an
  optional add-on, never a boot requirement (`main.py`'s loop must run
  fine on a host with no Telegram set up at all).
- `TelegramNotifier.send()` swallows *every* exception (network fault,
  Telegram API error, bad chat id) after logging it — a notification
  failure must never take down or delay the bar-close loop it's
  reporting on. This is the one place in the codebase where a blanket
  `except Exception` is the correct contract, not a smell.
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

_SEND_TIMEOUT_SECONDS = 5.0


class TelegramNotifier:
    """Sends one-way Telegram messages to a fixed set of chat ids."""

    def __init__(self, bot_token: str, chat_ids: tuple[int, ...]) -> None:
        self._bot_token = bot_token
        self._chat_ids = chat_ids

    def send(self, text: str) -> None:
        """Best-effort delivery to every configured chat. Never raises."""
        for chat_id in self._chat_ids:
            try:
                response = requests.post(
                    f"https://api.telegram.org/bot{self._bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                    timeout=_SEND_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
            except Exception:
                logger.warning(
                    "Telegram notification to chat_id=%s failed (ignored).",
                    chat_id,
                    exc_info=True,
                )


def build_notifier_from_env() -> TelegramNotifier | None:
    """A `TelegramNotifier` from `TELEGRAM_BOT_TOKEN`/`TELEGRAM_ALLOWED_CHAT_ID`,
    or `None` when either is unset/unparseable — never raises, unlike
    `config.telegram_config.TelegramConfig.from_env()` (which fails fast
    because for `monitoring/telegram_bot.py` those variables are the whole
    point of the process; here they're optional).

    Reads `os.environ` directly rather than re-running `load_dotenv()`:
    every caller (`container.ApplicationContainer.build()`) has already
    loaded `.env` via `ConfigManager.load()` by the time this runs.
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    raw_chat_ids = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
    if not bot_token or not raw_chat_ids:
        return None
    try:
        chat_ids = tuple(
            int(chat_id.strip()) for chat_id in raw_chat_ids.split(",") if chat_id.strip()
        )
    except ValueError:
        logger.warning(
            "TELEGRAM_ALLOWED_CHAT_ID is not a comma-separated list of integers; "
            "push notifications disabled."
        )
        return None
    if not chat_ids:
        return None
    return TelegramNotifier(bot_token, chat_ids)
