"""Small persistent state shared by the background service and the command-line tools."""

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .fsutil import atomic_write_text
from .locks import FileLock


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = FileLock(self.path.with_name(self.path.name + ".lock"))

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def update(self, change: Callable[[dict], None]) -> dict:
        with self._lock:
            data = self.load()
            change(data)
            atomic_write_text(self.path, json.dumps(data, indent=2, sort_keys=True) + "\n")
            return data
