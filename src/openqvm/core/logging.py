"""Logging setup with secret redaction."""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key)\b(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
)
_REDACTED = r"\1\2[redacted]"

_CONFIGURED = False


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        record.msg = _SECRET_RE.sub(_REDACTED, message)
        record.args = ()
        return True


def setup_logging(log_file: Path | None = None, *, level: int = logging.INFO, console: bool = False) -> None:
    """Configure the ``openqvm`` logger exactly once."""

    global _CONFIGURED
    logger = logging.getLogger("openqvm")
    if _CONFIGURED:
        return
    logger.setLevel(level)
    logger.propagate = False
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s [%(process)d] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    redaction = RedactionFilter()
    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redaction)
        logger.addHandler(file_handler)
    if console or log_file is None:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.addFilter(redaction)
        logger.addHandler(console_handler)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if name == "openqvm" or name.startswith("openqvm."):
        return logging.getLogger(name)
    return logging.getLogger(f"openqvm.{name}")
