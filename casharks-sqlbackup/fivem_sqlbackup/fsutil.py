"""Filesystem helpers: atomic writes, permissions, free space and subprocess flags."""

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

IS_WINDOWS = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_BROAD_SIDS = {"S-1-1-0": "Everyone", "S-1-5-11": "Authenticated Users", "S-1-5-32-545": "Users", "S-1-5-7": "Anonymous"}


def popen_kwargs(low_priority: bool = False) -> dict:
    """Keyword arguments that keep child processes windowless and optionally low priority."""
    if IS_WINDOWS:
        return {"creationflags": NO_WINDOW | (BELOW_NORMAL if low_priority else 0)}
    if low_priority:
        return {"preexec_fn": lambda: os.nice(10)}
    return {}


def safe_component(text: str, fallback: str = "x") -> str:
    cleaned = _UNSAFE.sub("_", text).strip("._")
    return cleaned[:80] or fallback


def is_within(child: Path, parent: Path) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def fsync_dir(path: Path) -> None:
    if IS_WINDOWS:
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes, private: bool = False) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if private:
            restrict(Path(tmp))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    fsync_dir(path.parent)


def atomic_write_text(path: Path, text: str, private: bool = False) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), private)


def free_bytes(path: Path) -> int:
    p = Path(path).resolve()
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(p).free


def tree_size(path: Path) -> int:
    return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


WINDOWS_MAX_PATH = 259


def long_paths_enabled() -> bool:
    if not IS_WINDOWS:
        return True
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            return winreg.QueryValueEx(key, "LongPathsEnabled")[0] == 1
    except OSError:
        return False


def path_fits(path: Path, extra: int = 0) -> bool:
    """False when path (plus `extra` more characters) exceeds the Windows 260-character limit."""
    return len(str(path)) + extra <= WINDOWS_MAX_PATH or long_paths_enabled()


_user_sid: str | None = None


def current_user_sid() -> str | None:
    global _user_sid
    if _user_sid is None and IS_WINDOWS:
        r = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True,
                           creationflags=NO_WINDOW)
        m = re.search(r'"(S-1-[\d-]+)"', r.stdout)
        _user_sid = m.group(1) if m else None
    return _user_sid


def restrict(path: Path) -> bool:
    """Limit access to the current user (plus SYSTEM and Administrators on Windows)."""
    p = Path(path)
    if not p.exists():
        return False
    if IS_WINDOWS:
        sid = current_user_sid()
        if not sid:
            return False
        inherit = "(OI)(CI)" if p.is_dir() else ""
        cmd = ["icacls", str(p), "/inheritance:r", "/grant:r",
               f"*{sid}:{inherit}F", f"*S-1-5-18:{inherit}F", f"*S-1-5-32-544:{inherit}F"]
        return subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW).returncode == 0
    os.chmod(p, 0o700 if p.is_dir() else 0o600)
    return True


def broad_access(path: Path) -> list[str]:
    """Names of broad groups (Everyone, Users, ...) that can access the path. Empty means private."""
    p = Path(path)
    if not p.exists():
        return []
    if not IS_WINDOWS:
        mode = p.stat().st_mode
        found = []
        if mode & 0o070:
            found.append("group")
        if mode & 0o007:
            found.append("others")
        return found
    script = ("(Get-Acl -LiteralPath $env:SQLBACKUP_ACL_PATH).Access | ForEach-Object { "
              "try { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } catch {} }")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, creationflags=NO_WINDOW,
                       env={**os.environ, "SQLBACKUP_ACL_PATH": str(p)})
    sids = set(r.stdout.split())
    return sorted(name for sid, name in _BROAD_SIDS.items() if sid in sids)
