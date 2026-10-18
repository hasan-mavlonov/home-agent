"""Logging to a rotating file in the data directory, with secrets scrubbed."""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys

from .config import Settings

_TOKEN_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"tvly-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"ya29\.[A-Za-z0-9_\-\.]{10,}"),
    re.compile(r"1//[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(?i)(authorization|x-subscription-token|api[_-]?key|password|secret)(\"?\s*[:=]\s*\"?)[^\s\",]+"),
]


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def _clean(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[redacted]")
        for pattern in _TOKEN_PATTERNS:
            text = pattern.sub(lambda m: (m.group(1) + m.group(2) + "[redacted]") if m.lastindex else "[redacted]", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = self._clean(message)
        if record.exc_info and record.exc_info[1] is not None:
            # format the traceback now so it can be scrubbed too
            formatter = logging.Formatter()
            cleaned += "\n" + self._clean(formatter.formatException(record.exc_info))
            record.exc_info = None
            record.exc_text = None
        record.msg = cleaned
        record.args = ()
        return True


def configure_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
    redactor = RedactingFilter(settings.secret_values())
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        settings.log_dir / "home-agent.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO if sys.stderr.isatty() else logging.WARNING)
    console.setFormatter(formatter)
    console.addFilter(redactor)
    root.addHandler(console)
    for noisy in ("httpx2", "httpcore2", "httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        (settings.log_dir / "home-agent.log").chmod(0o600)
    except OSError:
        pass
