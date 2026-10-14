"""Locate the native client tools (mariadb-dump/mysqldump, mariadb/mysql, mariadb-binlog/mysqlbinlog)."""

import glob
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..errors import ConfigError
from ..fsutil import IS_WINDOWS, popen_kwargs

TOOL_NAMES = {
    "mariadb": ("mariadb-dump", "mariadb", "mariadb-binlog"),
    "mysql": ("mysqldump", "mysql", "mysqlbinlog"),
}

_COMMON_WINDOWS = [
    r"{pf}\MariaDB*\bin", r"{pf}\MySQL\MySQL Server*\bin", r"{pf86}\MariaDB*\bin", r"{pf86}\MySQL\MySQL Server*\bin",
    r"C:\xampp\mysql\bin", r"C:\laragon\bin\mysql\*\bin", r"C:\laragon\bin\mariadb\*\bin",
    r"C:\wamp64\bin\mysql\*\bin", r"C:\wamp64\bin\mariadb\*\bin", r"{drive}\MariaDB*\bin", r"{drive}\MySQL*\bin",
]
_COMMON_POSIX = ["/usr/bin", "/usr/local/bin", "/usr/local/mysql/bin", "/opt/homebrew/bin", "/usr/local/mariadb/bin"]


@dataclass
class ToolSet:
    bin_dir: Path
    dump: Path
    client: Path
    binlog: Path | None
    flavor: str
    version: str

    @property
    def version_tuple(self) -> tuple[int, ...]:
        return parse_version(self.version)


def parse_version(text: str) -> tuple[int, ...]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def _exe(name: str) -> str:
    return name + ".exe" if IS_WINDOWS else name


def _service_bin_dirs() -> list[Path]:
    """Folders of MySQL/MariaDB server binaries registered as Windows services."""
    if not IS_WINDOWS:
        return []
    import winreg
    found: list[Path] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services")
    except OSError:
        return found
    with root:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            try:
                with winreg.OpenKey(root, name) as key:
                    image = str(winreg.QueryValueEx(key, "ImagePath")[0])
            except OSError:
                continue
            m = re.match(r'\s*"([^"]+)"|\s*(\S+)', image)
            exe = (m.group(1) or m.group(2)) if m else ""
            if re.search(r"(mysqld|mariadbd)(\.exe)?$", exe, re.I):
                found.append(Path(exe).parent)
    return found


def candidate_dirs(hint: str = "") -> list[Path]:
    dirs: list[Path] = []
    if hint:
        return [Path(os.path.expandvars(hint)).expanduser()]
    for names in TOOL_NAMES.values():
        for name in names[:2]:
            located = shutil.which(name)
            if located:
                dirs.append(Path(located).parent)
    dirs += _service_bin_dirs()
    if IS_WINDOWS:
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        drives = [f"{letter}:" for letter in "CDEFGH" if os.path.exists(f"{letter}:\\")]
        for pattern in _COMMON_WINDOWS:
            for drive in drives if "{drive}" in pattern else [""]:
                for match in sorted(glob.glob(pattern.format(pf=pf, pf86=pf86, drive=drive)), reverse=True):
                    dirs.append(Path(match))
    else:
        dirs += [Path(d) for d in _COMMON_POSIX]
    unique: list[Path] = []
    for d in dirs:
        if d not in unique and d.is_dir():
            unique.append(d)
    return unique


def tool_version(exe: Path) -> str:
    try:
        r = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=15, **popen_kwargs())
        return (r.stdout or r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _toolset_in(directory: Path, prefer: str | None) -> ToolSet | None:
    order = ["mysql", "mariadb"] if prefer == "mysql" else ["mariadb", "mysql"]
    for family in order:
        dump_name, client_name, binlog_name = TOOL_NAMES[family]
        dump, client, binlog = (directory / _exe(n) for n in (dump_name, client_name, binlog_name))
        if dump.is_file() and client.is_file():
            version = tool_version(dump)
            flavor = "mariadb" if "mariadb" in version.lower() else "mysql"
            return ToolSet(directory, dump, client, binlog if binlog.is_file() else None, flavor, version)
    return None


def find_tools(hint: str = "", prefer: str | None = None) -> ToolSet:
    for directory in candidate_dirs(hint):
        tools = _toolset_in(directory, prefer)
        if tools:
            return tools
    if hint:
        raise ConfigError(f"No mariadb-dump/mysqldump and mariadb/mysql client found in {hint}",
                          hint="Set database.client_bin_dir to the 'bin' folder of your MariaDB or MySQL install.")
    raise ConfigError("Could not find the MariaDB/MySQL client tools (mariadb-dump or mysqldump).",
                      hint="Install the MariaDB or MySQL client tools, or set database.client_bin_dir to their "
                           "'bin' folder.")
