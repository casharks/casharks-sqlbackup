"""Logging with secret redaction, size-based rotation and an append-only audit trail."""

import json
import logging
import logging.handlers
import sys
import threading
from datetime import datetime
from pathlib import Path

LOGGER_NAME = "sqlbackup"
_audit_lock = threading.Lock()
_audit_path: Path | None = None


class Redactor:
    def __init__(self):
        self._secrets: set[str] = set()

    def add(self, value: str | None) -> None:
        if value and len(value) >= 3:
            self._secrets.add(value)

    def scrub(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "***")
        return text


REDACTOR = Redactor()


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return REDACTOR.scrub(super().format(record))


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def setup_logging(log_dir: Path | None, level: str = "INFO", max_mb: int = 5, keep: int = 10,
                  console: bool = True, verbose: bool = False) -> logging.Logger:
    global _audit_path
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if console and sys.stderr is not None:
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(logging.DEBUG if verbose else logging.WARNING)
        handler.setFormatter(RedactingFormatter("%(levelname)s: %(message)s"))
        logger.addHandler(handler)

    if log_dir is not None:
        from .fsutil import restrict
        log_dir.mkdir(parents=True, exist_ok=True)
        restrict(log_dir)
        handler = logging.handlers.RotatingFileHandler(
            log_dir / "sqlbackup.log", maxBytes=max(1, max_mb) * 1024 * 1024, backupCount=max(1, keep),
            encoding="utf-8", delay=True)
        handler.setLevel(getattr(logging, level.upper(), logging.INFO))
        handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-7s [%(process)d] %(name)s: %(message)s"))
        logger.addHandler(handler)
        _audit_path = log_dir / "audit.jsonl"
    return logger


def audit(event: str, **fields) -> None:
    """Append one structured record of a significant operation (backup, restore, prune...)."""
    if _audit_path is None:
        return
    record = {"time": datetime.now().astimezone().isoformat(timespec="seconds"), "event": event, **fields}
    line = REDACTOR.scrub(json.dumps(record, default=str, ensure_ascii=False))
    with _audit_lock:
        try:
            with open(_audit_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            get_logger().warning("Could not write audit log: %s", exc)
