"""Backup sets on disk: listing, lookup, verification and pinning.

A backup set is a folder named <instance>_<YYYYmmdd-HHMMSS>_<trigger> containing one dump file per
database and a manifest.json. Sets are assembled in .partial/ and renamed into place only when
complete, so a visible set is never half-written.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .dumpstream import FileCheck, verify_file
from .errors import AppError
from .fsutil import atomic_write_text

MANIFEST = "manifest.json"
PIN_FILE = ".pinned"
VERIFY_FILE = "verification.json"
PARTIAL_DIR = ".partial"
LOCK_FILE = ".backup.lock"
FORMAT = 1


@dataclass
class BackupSet:
    id: str
    path: Path
    manifest: dict | None
    error: str | None = None

    @property
    def valid(self) -> bool:
        return self.manifest is not None and self.error is None

    @property
    def created(self) -> datetime:
        if self.manifest:
            return datetime.fromtimestamp(self.manifest["created_ts"]).astimezone()
        return datetime.fromtimestamp(self.path.stat().st_mtime).astimezone()

    @property
    def pinned(self) -> bool:
        return (self.path / PIN_FILE).exists()

    @property
    def trigger(self) -> str:
        return (self.manifest or {}).get("trigger", "?")

    @property
    def size(self) -> int:
        if self.manifest:
            return sum(d["size"] for d in self.manifest["databases"])
        return sum(f.stat().st_size for f in self.path.iterdir() if f.is_file())

    @property
    def database_names(self) -> list[str]:
        return [d["name"] for d in (self.manifest or {}).get("databases", [])]

    def database(self, name: str) -> dict:
        for entry in (self.manifest or {}).get("databases", []):
            if entry["name"] == name:
                return entry
        raise AppError(f"Backup {self.id} does not contain database '{name}'.",
                       hint=f"It contains: {', '.join(self.database_names) or 'nothing'}")

    def file_for(self, name: str) -> Path:
        return self.path / self.database(name)["file"]

    @property
    def verification(self) -> dict | None:
        try:
            return json.loads((self.path / VERIFY_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @property
    def status(self) -> str:
        if not self.valid:
            return "INVALID"
        v = self.verification
        if v is not None:
            return "verified" if v.get("ok") else "FAILED"
        return "ok" if self.manifest.get("verified") else "unverified"


def _read_manifest(path: Path) -> tuple[dict | None, str | None]:
    try:
        data = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "manifest.json is missing"
    except (OSError, ValueError) as exc:
        return None, f"manifest.json is unreadable: {exc}"
    if data.get("format") != FORMAT or "created_ts" not in data or not isinstance(data.get("databases"), list):
        return None, "manifest.json has an unknown format"
    return data, None


def list_sets(backup_dir: Path) -> list[BackupSet]:
    """All backup sets, newest first. Folders that are not backups of this tool are ignored."""
    if not backup_dir.is_dir():
        return []
    sets = []
    for child in backup_dir.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        manifest, error = _read_manifest(child)
        if manifest is None and not (child / MANIFEST).exists() and not any(child.glob("*.sql*")):
            continue
        sets.append(BackupSet(child.name, child, manifest, error))
    sets.sort(key=lambda s: (s.created, s.id), reverse=True)
    return sets


def resolve_set(backup_dir: Path, ref: str) -> BackupSet:
    sets = list_sets(backup_dir)
    if not sets:
        raise AppError(f"No backups found in {backup_dir}")
    if ref in ("latest", "newest"):
        valid = [s for s in sets if s.valid]
        if not valid:
            raise AppError("No valid backups found.")
        return valid[0]
    if ref.isdigit() and 1 <= int(ref) <= len(sets) and len(ref) < 6:
        return sets[int(ref) - 1]
    exact = [s for s in sets if s.id == ref]
    if exact:
        return exact[0]
    matches = [s for s in sets if ref in s.id]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise AppError(f"'{ref}' matches {len(matches)} backups; be more specific.",
                       hint="Use the number shown by 'list' or the full backup name.")
    raise AppError(f"No backup matches '{ref}'.", hint="Run 'list' to see available backups.")


def verify_set(bset: BackupSet, record: bool = True) -> tuple[bool, list[FileCheck]]:
    if not bset.valid:
        return False, [FileCheck(str(bset.path), False, [bset.error or "invalid backup"])]
    checks = [verify_file(bset.path / d["file"], d.get("sha256"), d.get("size")) for d in bset.manifest["databases"]]
    ok = all(c.ok for c in checks)
    if record:
        atomic_write_text(bset.path / VERIFY_FILE, json.dumps({
            "ok": ok, "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "problems": {Path(c.path).name: c.problems for c in checks if c.problems},
        }, indent=2) + "\n")
    return ok, checks


def set_pinned(bset: BackupSet, pinned: bool) -> None:
    pin = bset.path / PIN_FILE
    if pinned:
        pin.write_text("Pinned backups are never removed by retention.\n", encoding="utf-8")
    elif pin.exists():
        pin.unlink()
