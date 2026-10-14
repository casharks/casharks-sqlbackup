"""Read database settings from a FiveM server.cfg (oxmysql / mysql-async / ghmattimysql style)."""

import re
import urllib.parse
from pathlib import Path

_CONVAR = re.compile(r'^\s*set[rs]?\s+mysql_connection_string\s+(?:"([^"]*)"|(\S+))', re.I | re.M)
_EXEC = re.compile(r'^\s*exec\s+(?:"([^"]+)"|(\S+))', re.I | re.M)
_ALIASES = {
    "host": ("host", "hostname", "ip", "server", "datasource", "data source", "addr", "address"),
    "port": ("port",),
    "user": ("user", "userid", "user id", "username", "user name", "uid"),
    "password": ("password", "pwd", "pass"),
    "database": ("database", "db"),
}


def locate_server_cfg(path: Path) -> Path | None:
    path = Path(path)
    if path.is_file():
        return path
    if path.is_dir():
        for candidate in ("server.cfg", "server.cfg.txt"):
            if (path / candidate).is_file():
                return path / candidate
    return None


def find_connection_string(cfg_file: Path, _depth: int = 0) -> str | None:
    try:
        text = Path(cfg_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = None
    for m in _CONVAR.finditer(text):
        found = m.group(1) if m.group(1) is not None else m.group(2)
    if found is None and _depth < 2:
        for m in _EXEC.finditer(text):
            included = Path(cfg_file).parent / (m.group(1) or m.group(2))
            found = find_connection_string(included, _depth + 1) or found
    return found


def parse_connection_string(value: str) -> dict:
    value = value.strip()
    if "://" in value:
        parts = urllib.parse.urlsplit(value)
        try:
            port = parts.port
        except ValueError:
            port = None
        result = {
            "host": parts.hostname or "",
            "port": port,
            "user": urllib.parse.unquote(parts.username or ""),
            "password": urllib.parse.unquote(parts.password) if parts.password is not None else None,
            "database": urllib.parse.unquote(parts.path.lstrip("/")),
        }
    else:
        pairs = {}
        for item in value.split(";"):
            key, sep, val = item.partition("=")
            if sep:
                pairs[key.strip().lower()] = val.strip()
        result = {}
        for field, names in _ALIASES.items():
            result[field] = next((pairs[n] for n in names if n in pairs), None)
        if result["port"] is None and result["host"] and ":" in result["host"]:
            result["host"], _, result["port"] = result["host"].partition(":")
        try:
            result["port"] = int(result["port"]) if result["port"] else None
        except ValueError:
            result["port"] = None
    return {k: v for k, v in result.items() if v not in (None, "")}
