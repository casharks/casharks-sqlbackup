"""Backup names built from user-configurable patterns such as "{date}-FullServer" or "{date}-{table}"."""

import re
from collections.abc import Callable
from datetime import datetime

FIELDS_FULL = ("date", "time", "trigger", "instance")
FIELDS_TABLE = FIELDS_FULL + ("database", "table")
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def render(template: str, when: datetime, date_format: str, time_format: str = "%H-%M-%S", **fields: str) -> str:
    values = {"date": when.strftime(date_format), "time": when.strftime(time_format), **fields}
    name = _UNSAFE.sub("_", template.format_map(values)).strip(" .")
    return name[:120] or "backup"


def unique(name: str, taken: Callable[[str], bool], when: datetime | None = None,
           time_format: str = "%H-%M-%S") -> str:
    """name if free; otherwise name_<time> (pass when=None if the name already contains the time); then _2, _3."""
    if not taken(name):
        return name
    base = f"{name}_{_UNSAFE.sub('_', when.strftime(time_format))}" if when else name
    candidate, n = base, 1
    while taken(candidate):
        n += 1
        candidate = f"{base}_{n}"
    return candidate


def problem(template: str, date_format: str, allowed: tuple[str, ...], time_format: str = "%H-%M-%S") -> str | None:
    """Return an error message if the pattern cannot be used."""
    try:
        render(template, datetime(2026, 1, 2, 3, 4, 5), date_format, time_format, **{f: "x" for f in allowed})
    except KeyError as exc:
        return f"unknown placeholder {{{exc.args[0]}}}; use {', '.join('{' + f + '}' for f in allowed)}"
    except (ValueError, IndexError) as exc:
        return f"invalid pattern ({exc})"
    if "{date}" not in template and "{time}" not in template:
        return "must contain {date} or {time} so backups are named after when they were made"
    return None
