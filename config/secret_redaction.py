"""Logging filter that redacts known secret values from log records before
they reach any handler, preventing credential leakage into structured
production logs (docs/PRODUCTION_SPEC.md §1).
"""

from __future__ import annotations

import logging


class SecretRedactingFilter(logging.Filter):
    """A `logging.Filter` that replaces every occurrence of each configured
    secret value with a fixed redaction marker in the formatted log
    message. Attach via `logger.addFilter(...)` (or
    `logging.Handler.addFilter(...)` to scope it to one handler).

    Filters run before formatting in the standard library's logging
    pipeline, so this rewrites `record.msg`/`record.args` directly rather
    than the final formatted string — every handler downstream of this
    filter sees only the redacted text.
    """

    REDACTED = "***REDACTED***"

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        # Longest-first so a secret that is a substring of another secret
        # is not partially redacted by the shorter match first.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = self._redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, self.REDACTED)
        return text
