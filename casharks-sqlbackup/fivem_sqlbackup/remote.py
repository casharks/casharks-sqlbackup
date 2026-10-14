"""Off-site copies. 'directory' covers other disks, NAS shares and cloud-synced folders; 'rclone'
covers S3, Backblaze B2, Google Drive, SFTP and many more, with credentials kept in rclone's own
(optionally encrypted) configuration rather than in this application."""

import hashlib
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from .catalog import MANIFEST, BackupSet
from .errors import StorageError
from .fsutil import popen_kwargs
from .logsetup import audit, get_logger
from .state import State

log = get_logger("remote")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Destination(ABC):
    @abstractmethod
    def describe(self) -> str: ...

    @abstractmethod
    def upload(self, bset: BackupSet) -> None: ...

    @abstractmethod
    def list_ids(self) -> list[str]: ...

    @abstractmethod
    def delete(self, set_id: str) -> None: ...

    def check(self) -> None:
        self.list_ids()


class DirectoryDestination(Destination):
    def __init__(self, root: Path):
        self.root = root

    def describe(self) -> str:
        return f"folder {self.root}"

    def upload(self, bset: BackupSet) -> None:
        final = self.root / bset.id
        if final.exists():
            log.info("Remote copy of %s already exists", bset.id)
            return
        staging = self.root / f".partial-{bset.id}"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if staging.exists():
                shutil.rmtree(staging)
            shutil.copytree(bset.path, staging)
            for entry in bset.manifest["databases"]:
                if _sha256(staging / entry["file"]) != entry["sha256"]:
                    raise StorageError(f"Remote copy of {entry['file']} does not match the original checksum.",
                                       transient=True)
            staging.rename(final)
        except OSError as exc:
            raise StorageError(f"Copy to {self.root} failed: {exc}", transient=True) from None
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def list_ids(self) -> list[str]:
        if not self.root.is_dir():
            raise StorageError(f"Remote folder {self.root} does not exist or is not reachable.")
        return [p.name for p in self.root.iterdir() if p.is_dir() and (p / MANIFEST).is_file()]

    def delete(self, set_id: str) -> None:
        target = self.root / set_id
        if (target / MANIFEST).is_file():
            shutil.rmtree(target)


class RcloneDestination(Destination):
    def __init__(self, binary: str, remote: str):
        self.binary = binary
        self.remote = remote.rstrip("/")

    def describe(self) -> str:
        return f"rclone {self.remote}"

    def _run(self, *args: str, timeout: float = 3600) -> str:
        try:
            r = subprocess.run([self.binary, *args], capture_output=True, text=True, timeout=timeout,
                               **popen_kwargs(low_priority=True))
        except FileNotFoundError:
            raise StorageError(f"rclone was not found ('{self.binary}').",
                               hint="Install rclone or set remote.rclone_binary to its full path.") from None
        except subprocess.TimeoutExpired:
            raise StorageError("rclone timed out.", transient=True) from None
        if r.returncode != 0:
            raise StorageError(f"rclone {args[0]} failed: {r.stderr.strip()[-800:]}", transient=True)
        return r.stdout

    def upload(self, bset: BackupSet) -> None:
        dest = f"{self.remote}/{bset.id}"
        self._run("copy", str(bset.path), dest, "--checksum", "--retries", "3")
        self._run("check", str(bset.path), dest, "--one-way")

    def list_ids(self) -> list[str]:
        out = self._run("lsf", "--dirs-only", self.remote + "/", timeout=300)
        return [line.strip().rstrip("/") for line in out.splitlines() if line.strip()]

    def delete(self, set_id: str) -> None:
        self._run("purge", f"{self.remote}/{set_id}")


def destination(cfg) -> Destination | None:
    r = cfg.remote
    if not r.enabled:
        return None
    if r.type == "rclone":
        return RcloneDestination(r.rclone_binary, r.path)
    return DirectoryDestination(cfg.resolve(r.path))


def upload_and_prune(cfg, bset: BackupSet) -> None:
    dest = destination(cfg)
    if dest is None:
        return
    dest.upload(bset)
    log.info("Copied %s to %s", bset.id, dest.describe())
    audit("remote_upload", backup=bset.id, destination=dest.describe())
    state = State(cfg.state_path)

    def record(s):
        entries = {e["id"]: e for e in s.get("remote_uploaded", [])}
        entries[bset.id] = {"id": bset.id, "ts": bset.manifest["created_ts"]}
        s["remote_uploaded"] = sorted(entries.values(), key=lambda e: e["ts"])
    uploaded = state.update(record)["remote_uploaded"]
    if cfg.remote.keep_last <= 0:
        return
    removed = []
    for old in uploaded[:-cfg.remote.keep_last]:
        try:
            dest.delete(old["id"])
            removed.append(old["id"])
            log.info("Remote retention removed %s", old["id"])
        except (OSError, StorageError) as exc:
            log.warning("Could not remove remote backup %s: %s", old["id"], exc)

    def forget(s):
        s["remote_uploaded"] = [e for e in s.get("remote_uploaded", []) if e["id"] not in removed]
    if removed:
        state.update(forget)
