"""Creating backups.

Each database is streamed from mariadb-dump/mysqldump (consistent InnoDB snapshot, no table locks)
through gzip into a new file that is never allowed to overwrite an existing one. The stream is
analysed on the fly, the file is re-read to confirm it is intact, and the finished set is moved
into place atomically.
"""

import errno
import gzip
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import __version__, naming
from .catalog import LOCK_FILE, MANIFEST, PARTIAL_DIR, PIN_FILE, FORMAT, BackupSet, list_sets
from .config import SYSTEM_DATABASES
from .dumpstream import DumpAnalyzer, verify_file
from .engines import DumpOptions, open_engine
from .engines.mysql import classify_error, stderr_reader
from .errors import AppError, BackupBusy, DbError, StorageError
from .fsutil import atomic_write_text, free_bytes, fsync_dir, human_size, path_fits, restrict, safe_component
from .locks import FileLock
from .logsetup import audit, get_logger
from .state import State, now_iso

log = get_logger("backup")
TRANSACTIONAL_ENGINES = {"innodb", "xtradb", "rocksdb", "ndbcluster", "ndb"}
_GZIP_ESTIMATE = 0.35


@dataclass
class BackupResult:
    status: str
    set_id: str | None = None
    path: Path | None = None
    size: int = 0
    duration: float = 0.0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    hint: str | None = None
    selected: list[dict] = field(default_factory=list)


class _HashingWriter:
    def __init__(self, raw, digest):
        self.raw = raw
        self.digest = digest

    def write(self, data) -> int:
        self.digest.update(data)
        return self.raw.write(data)

    def flush(self) -> None:
        self.raw.flush()


def resolve_databases(cfg, available: list[str], override: list[str] | None = None) -> list[str]:
    wanted = override or cfg.backup.databases
    if wanted == ["*"]:
        excluded = set(cfg.backup.exclude_databases) | SYSTEM_DATABASES
        result = [d for d in available if d not in excluded]
        if not result:
            raise AppError("No user databases found on the server to back up.")
        return result
    lookup = {d.lower(): d for d in available}
    missing = [d for d in wanted if d not in available and d.lower() not in lookup]
    if missing:
        raise DbError(f"Database(s) not found on the server: {', '.join(missing)}", code=1049,
                      hint="Check backup.databases in the configuration (names are case-sensitive on Linux).")
    return [d if d in available else lookup[d.lower()] for d in wanted]


def _is_disk_full(exc: OSError) -> bool:
    return exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in (39, 112)


class BackupRunner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.backup_dir = cfg.backup_dir
        self.complete = False
        self.single_database = True

    def run(self, trigger: str = "manual", note: str = "", pinned: bool = False,
            databases: list[str] | None = None, complete: bool = False) -> BackupResult:
        """complete=True ignores exclude_tables/schema_only_tables (used for safety backups before a restore)."""
        self.complete = complete
        if not path_fits(self.backup_dir, 100):
            raise StorageError(
                f"The backup folder path is too long for Windows ({len(str(self.backup_dir))} characters).",
                hint="Use a shorter backup.directory (e.g. D:\\FiveMBackups) or enable Windows long paths.")
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"Cannot create the backup folder {self.backup_dir}: {exc}", transient=True) from None
        if self.cfg.security.restrict_permissions:
            restrict(self.cfg.backup_root)
        lock = FileLock(self.backup_dir / LOCK_FILE)
        if not lock.acquire():
            raise BackupBusy("Another backup is already running for this backup folder.")
        try:
            self._cleanup_partials()
            delay = self.cfg.retry.delay_seconds
            attempts = self.cfg.retry.attempts
            for attempt in range(1, attempts + 1):
                try:
                    result = self._attempt(trigger, note, pinned, databases)
                    if self.cfg.selected_tables.tables and trigger != "pre-restore":
                        self._export_selected(result)
                    return result
                except (DbError, StorageError) as exc:
                    if not exc.transient or attempt == attempts:
                        raise
                    log.warning("Backup attempt %d/%d failed (%s); retrying in %ds", attempt, attempts, exc, delay)
                    time.sleep(delay)
                    delay = int(delay * self.cfg.retry.backoff_factor)
        finally:
            lock.release()
        raise AssertionError("unreachable")

    def _export_selected(self, result: BackupResult) -> None:
        """Separate per-table copies. Never allowed to affect the finished full backup."""
        from . import selected
        try:
            manifest = json.loads((result.path / MANIFEST).read_text(encoding="utf-8"))
            items, warnings = selected.export_from_set(self.cfg, result.path, manifest)
            atomic_write_text(result.path / "selected_tables.json", json.dumps(items, indent=2) + "\n")
            result.selected = items
            result.warnings += warnings
        except Exception as exc:
            log.exception("Separate table copies failed")
            result.warnings.append(f"Separate table copies failed: {exc}")

    def _cleanup_partials(self) -> None:
        partial_root = self.backup_dir / PARTIAL_DIR
        if not partial_root.is_dir():
            return
        for leftover in partial_root.iterdir():
            log.warning("Removing incomplete backup left by an interrupted run: %s", leftover.name)
            shutil.rmtree(leftover, ignore_errors=True)

    def _new_set_id(self, trigger: str) -> str:
        b = self.cfg.backup
        now = datetime.now()
        base = naming.render(b.name_format, now, b.date_format, b.time_format, trigger=trigger,
                             instance=self.cfg.general.instance_name)
        return naming.unique(base, lambda c: (self.backup_dir / c).exists()
                             or (self.backup_dir / PARTIAL_DIR / c).exists(),
                             None if "{time}" in b.name_format else now, b.time_format)

    def _check_disk(self, estimate: int, warnings: list[str]) -> None:
        free = free_bytes(self.backup_dir)
        need = estimate + self.cfg.disk.min_free_mb * 1024 * 1024
        if free < need:
            raise StorageError(
                f"Not enough free space in {self.backup_dir}: {human_size(free)} free, about "
                f"{human_size(estimate)} needed plus the {self.cfg.disk.min_free_mb} MB safety margin.",
                hint="Free up space, lower retention limits, or move backup.directory to a larger disk.")
        if free - estimate < self.cfg.disk.warn_free_mb * 1024 * 1024:
            warnings.append(f"Low disk space: {human_size(free)} free in {self.backup_dir}.")

    def _attempt(self, trigger: str, note: str, pinned: bool, override: list[str] | None) -> BackupResult:
        cfg = self.cfg
        started = time.monotonic()
        created = datetime.now().astimezone()
        warnings: list[str] = []
        with open_engine(cfg, "backup") as eng:
            info = eng.server_info()
            if cfg.database.engine != "auto" and info.flavor != cfg.database.engine:
                warnings.append(f"Configured engine is {cfg.database.engine} but the server reports {info.flavor}.")
            databases = resolve_databases(cfg, eng.list_databases(), override)
            excluded = set() if self.complete else set(cfg.backup.exclude_tables)
            tables = eng.tables(databases)
            unsafe = sorted(f"{t.database}.{t.name} ({t.engine})" for t in tables
                            if t.engine and t.engine.lower() not in TRANSACTIONAL_ENGINES
                            and f"{t.database}.{t.name}" not in excluded)
            if unsafe:
                warnings.append("Non-transactional tables cannot be captured in a consistent snapshot: "
                                + ", ".join(unsafe[:10]) + (" ..." if len(unsafe) > 10 else ""))
            raw_size = sum(t.size_bytes for t in tables)
            ratio = _GZIP_ESTIMATE if cfg.backup.compression == "gzip" else 1.1
            self._check_disk(int(raw_size * ratio), warnings)

            set_id = self._new_set_id(trigger)
            self.single_database = len(databases) == 1
            partial = self.backup_dir / PARTIAL_DIR / set_id
            longest = max([len(set_id) + len(safe_component(d)) + 10 for d in databases] + [len(MANIFEST) + 14])
            if not path_fits(partial, longest + 1):
                raise StorageError(
                    f"The backup folder path is too long for Windows ({len(str(self.backup_dir))} characters).",
                    hint="Use a shorter backup.directory (e.g. D:\\FiveMBackups) or enable Windows long paths.")
            partial.mkdir(parents=True)
            log.info("Backup %s started (trigger=%s, databases=%s)", set_id, trigger, ", ".join(databases))
            try:
                entries = []
                used_names: set[str] = set()
                for database in databases:
                    entries.append(self._dump_database(eng, info, database, partial, used_names, warnings))
                manifest = {
                    "format": FORMAT,
                    "id": set_id,
                    "app_version": __version__,
                    "instance": cfg.general.instance_name,
                    "trigger": trigger,
                    "note": note,
                    "created_at": created.isoformat(timespec="seconds"),
                    "created_ts": created.timestamp(),
                    "duration_seconds": round(time.monotonic() - started, 2),
                    "server": {"flavor": info.flavor, "version": info.version, "log_bin": info.log_bin,
                               "binlog_format": info.variables.get("binlog_format")},
                    "tools": {"flavor": eng.tools.flavor, "dump_version": eng.tools.version},
                    "compression": cfg.backup.compression,
                    "verified": cfg.verify.after_backup,
                    "databases": entries,
                    "warnings": warnings,
                }
                if pinned:
                    (partial / PIN_FILE).write_text("Pinned backups are never removed by retention.\n",
                                                    encoding="utf-8")
                atomic_write_text(partial / MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
                final = self.backup_dir / set_id
                if final.exists():
                    raise StorageError(f"Refusing to overwrite existing backup {final}")
                os.rename(partial, final)
                fsync_dir(self.backup_dir)
            except BaseException:
                shutil.rmtree(partial, ignore_errors=True)
                raise

        size = sum(e["size"] for e in entries)
        duration = time.monotonic() - started
        log.info("Backup %s completed: %s in %.1fs", set_id, human_size(size), duration)
        for w in warnings:
            log.warning("%s: %s", set_id, w)
        return BackupResult("success", set_id, final, size, duration, warnings)

    def _dump_database(self, eng, info, database: str, folder: Path, used: set[str], warnings: list[str]) -> dict:
        cfg = self.cfg
        stem = folder.name if self.single_database else f"{folder.name}-{safe_component(database, 'database')}"
        while stem.lower() in used:
            stem += "_"
        used.add(stem.lower())
        ext = ".sql.gz" if cfg.backup.compression == "gzip" else ".sql"
        path = folder / (stem + ext)
        prefix = database + "."
        complete = self.complete
        options = DumpOptions(
            ignore_tables=[] if complete else [t for t in cfg.backup.exclude_tables if t.startswith(prefix)],
            schema_only_tables=[] if complete else [t for t in cfg.backup.schema_only_tables if t.startswith(prefix)],
            routines=cfg.backup.routines, events=cfg.backup.events, triggers=cfg.backup.triggers,
            binlog_position=info.log_bin, max_packet_mb=cfg.backup.max_allowed_packet_mb,
            low_priority=cfg.backup.low_priority)
        if options.schema_only_tables and eng.flavor != "mariadb":
            raise AppError("backup.schema_only_tables requires the MariaDB dump tool.")
        charset, collation = eng.database_charset(database)

        started = time.monotonic()
        analyzer = DumpAnalyzer()
        digest = hashlib.sha256()
        proc = eng.start_dump(database, options, info)
        errors = stderr_reader(proc.stderr)
        try:
            with open(path, "xb") as raw:
                writer = _HashingWriter(raw, digest)
                out = gzip.GzipFile(filename="", mode="wb", fileobj=writer, compresslevel=cfg.backup.compression_level) \
                    if ext == ".sql.gz" else writer
                for chunk in iter(lambda: proc.stdout.read(1 << 20), b""):
                    analyzer.feed(chunk)
                    out.write(chunk)
                if out is not writer:
                    out.close()
                raw.flush()
                os.fsync(raw.fileno())
            rc = proc.wait()
        except OSError as exc:
            if _is_disk_full(exc):
                raise StorageError(f"Disk full while writing {path.name}.",
                                   hint="Free up space or move backup.directory.") from None
            raise StorageError(f"Could not write {path}: {exc}", transient=True) from None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        analyzer.finish()
        stderr = errors.text
        if rc != 0:
            raise classify_error(stderr, f"Dump of '{database}' failed with exit code {rc}")
        if not analyzer.completed:
            raise DbError(f"Dump of '{database}' ended without its completion marker (output was cut off).",
                          transient=True)
        if stderr.strip():
            warnings.append(f"{database}: {classify_error(stderr)}")

        size = path.stat().st_size
        sha = digest.hexdigest()
        if cfg.verify.after_backup:
            check = verify_file(path, sha, size)
            if not check.ok:
                raise StorageError(f"Verification of {path.name} failed: {'; '.join(check.problems)}",
                                   transient=True)
        summary = analyzer.summary()
        return {
            "name": database,
            "file": path.name,
            "size": size,
            "sha256": sha,
            "uncompressed_bytes": analyzer.bytes_seen,
            "charset": charset,
            "collation": collation,
            "duration_seconds": round(time.monotonic() - started, 2),
            "excluded_tables": options.ignore_tables,
            "schema_only_tables": options.schema_only_tables,
            **summary,
        }


def perform_backup(cfg, trigger: str = "manual", note: str = "", pinned: bool = False,
                   databases: list[str] | None = None, housekeeping: bool = True) -> BackupResult:
    """Run a backup and handle everything around it: state, alerts, retention and remote copies."""
    from . import notify, retention

    state = State(cfg.state_path)
    state.update(lambda s: s.update(last_attempt_at=now_iso()))
    try:
        result = BackupRunner(cfg).run(trigger, note, pinned, databases)
    except BackupBusy as exc:
        log.info("Backup skipped: %s", exc)
        return BackupResult("skipped", error=str(exc))
    except Exception as exc:
        if isinstance(exc, AppError):
            err = exc
            log.error("Backup failed (trigger=%s): %s", trigger, err)
            if err.hint:
                log.error("Hint: %s", err.hint)
        else:
            log.exception("Backup failed unexpectedly (trigger=%s)", trigger)
            err = AppError(f"Unexpected error: {exc}", hint="See the log file for details.")

        def failed(s):
            s.update(last_failure_at=now_iso(), last_error=str(err)[:2000])
            s["consecutive_failures"] = s.get("consecutive_failures", 0) + 1
        state.update(failed)
        audit("backup_failed", trigger=trigger, error=str(err))
        notify.backup_failure(cfg, trigger, err, databases or list(cfg.backup.databases))
        return BackupResult("failed", error=str(err), hint=err.hint)

    def succeeded(s):
        s.update(last_success_at=now_iso(), last_success_id=result.set_id, consecutive_failures=0)
        if cfg.remote.enabled:
            s["pending_remote"] = sorted(set(s.get("pending_remote", [])) | {result.set_id})
    state.update(succeeded)
    audit("backup", backup=result.set_id, trigger=trigger, size=result.size, seconds=round(result.duration, 1),
          warnings=result.warnings)

    if housekeeping:
        removed = retention.prune(cfg)
        if removed:
            log.info("Retention removed %d old backup(s)", len(removed))
        if cfg.selected_tables.tables:
            from . import selected
            selected.prune(cfg)
        sync_pending_remote(cfg, result.warnings)

    manifest = json.loads((result.path / MANIFEST).read_text(encoding="utf-8"))
    try:
        from . import activity
        activity.record(cfg, manifest)
    except Exception:
        log.exception("Could not update the table activity history")
    notify.backup_success(cfg, result, manifest)
    for item in result.selected:
        if not item.get("ok"):
            notify.table_copy_problem(cfg, result.set_id, item)
    notify.warnings(cfg, result.set_id, [w for w in result.warnings if not w.startswith("Separate copy of ")])
    return result


def sync_pending_remote(cfg, warnings: list[str] | None = None) -> None:
    """Upload backups that have not reached the remote destination yet (retried on later runs)."""
    from . import notify, remote

    if not cfg.remote.enabled:
        return
    state = State(cfg.state_path)
    pending = state.load().get("pending_remote", [])
    if not pending:
        return
    existing = {s.id: s for s in list_sets(cfg.backup_dir) if s.valid}
    done, failed = [], []
    for set_id in pending:
        bset: BackupSet | None = existing.get(set_id)
        if bset is None:
            done.append(set_id)
            continue
        try:
            remote.upload_and_prune(cfg, bset)
            done.append(set_id)
        except (AppError, OSError) as exc:
            failed.append(f"{set_id}: {exc}")
            log.warning("Remote copy of %s failed (will retry later): %s", set_id, exc)
    state.update(lambda s: s.update(pending_remote=[p for p in s.get("pending_remote", []) if p not in done]))
    if failed:
        message = "Remote copy failed (will retry): " + "; ".join(failed)
        if warnings is not None:
            warnings.append(message)
        else:
            notify.warnings(cfg, None, [message])
