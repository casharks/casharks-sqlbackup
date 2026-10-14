"""Restoring backups.

Two modes:
  * restore into a NEW, separate database (safe; nothing existing is touched), and
  * replace a live database (or selected tables of it). Live replacement requires explicit
    confirmation from the caller, refuses to run while the FiveM server is up, takes a pinned
    safety backup first, proves the backup loads cleanly in a scratch database, and rolls back
    automatically from the safety backup if the final load fails.
Every restore is verified afterwards against the exact row counts recorded in the manifest.
"""

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .backup import BackupRunner
from .catalog import BackupSet, resolve_set
from .config import SYSTEM_DATABASES, validate_db_name
from .dumpstream import filter_tables, iter_dump_lines, verify_file
from .engines import open_engine, quote_ident, quote_literal
from .engines.mysql import classify_error, stderr_reader
from .errors import AppError, DbError
from .logsetup import audit, get_logger
from .procwatch import server_processes

log = get_logger("restore")


@dataclass
class RestoreReport:
    target: str
    ok: bool
    problems: list[str] = field(default_factory=list)
    checked_tables: int = 0
    safety_backup: str | None = None
    rolled_back: bool = False
    duration: float = 0.0


def check_target_name(name: str) -> None:
    problem = validate_db_name(name)
    if problem:
        raise AppError(f"Target database name '{name}' {problem}.")
    if name.lower() in SYSTEM_DATABASES:
        raise AppError(f"'{name}' is a system database and cannot be a restore target.")


def database_exists(eng, name: str) -> bool:
    return bool(eng.query("SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = " + quote_literal(name)))


def _create_database(eng, name: str, entry: dict) -> None:
    sql = f"CREATE DATABASE {quote_ident(name)}"
    if re.fullmatch(r"\w+", entry.get("charset") or ""):
        sql += f" CHARACTER SET {entry['charset']}"
        if re.fullmatch(r"\w+", entry.get("collation") or ""):
            sql += f" COLLATE {entry['collation']}"
    eng.query(sql)


def load_dump(eng, path: Path, target: str, tables: set[str] | None, max_packet_mb: int) -> None:
    proc = eng.start_load(target, max_packet_mb)
    errors = stderr_reader(proc.stderr)
    lines = iter_dump_lines(path)
    if tables:
        lines = filter_tables(lines, tables)
    try:
        for line in lines:
            proc.stdin.write(line)
        proc.stdin.close()
    except OSError:
        pass 
    finally:
        rc = proc.wait()
    if rc != 0:
        raise classify_error(errors.text, f"Loading the backup into '{target}' failed (exit code {rc})")


def _binlog_files(directory: Path, first: str) -> list[str]:
    stem, _, number = first.rpartition(".")
    if not number.isdigit():
        raise AppError(f"Unexpected binary log file name '{first}'.")
    files = sorted((p for p in directory.glob(f"{stem}.*") if p.suffix[1:].isdigit() and
                    int(p.suffix[1:]) >= int(number)), key=lambda p: int(p.suffix[1:]))
    if not files or files[0].name != first:
        raise AppError(f"Binary log {first} was not found in {directory}.",
                       hint="Point pitr.binlog_dir at the folder holding the server's binary logs (or an archive "
                            "of them). Logs removed by binlog expiry cannot be replayed.")
    return [str(p) for p in files]


def replay_binlogs(cfg, eng, entry: dict, target: str, until: str) -> None:
    import subprocess
    from .fsutil import popen_kwargs

    position = entry.get("binlog")
    if not position:
        raise AppError("This backup has no binary log position, so it cannot be rolled forward.",
                       hint="Point-in-time recovery needs binary logging enabled on the server before the backup "
                            "is taken (log-bin in the database my.ini).")
    directory = cfg.resolve(cfg.pitr.binlog_dir) if cfg.pitr.binlog_dir else None
    if directory is None:
        base = eng.server_info().variables.get("log_bin_basename", "")
        if not base:
            raise AppError("The server did not report where its binary logs are.", hint="Set pitr.binlog_dir.")
        directory = Path(base).parent
    files = _binlog_files(directory, position["file"])
    cmd = eng.binlog_replay_command(files, position["position"], until, entry["name"], target)
    log.info("Replaying %d binary log file(s) from %s:%s until %s", len(files), position["file"],
             position["position"], until)
    reader = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_kwargs())
    reader_errors = stderr_reader(reader.stderr)
    loader = eng.start_load(target, cfg.backup.max_allowed_packet_mb)
    loader_errors = stderr_reader(loader.stderr)
    try:
        for chunk in iter(lambda: reader.stdout.read(1 << 20), b""):
            loader.stdin.write(chunk)
        loader.stdin.close()
    except OSError:
        pass
    finally:
        rc_reader = reader.wait()
        rc_loader = loader.wait()
    if rc_reader != 0:
        raise classify_error(reader_errors.text, "Reading the binary logs failed")
    if rc_loader != 0:
        raise classify_error(loader_errors.text, "Applying the binary logs failed")


def verify_restored(eng, target: str, entry: dict, tables: set[str] | None = None,
                    exact_counts: bool = True) -> tuple[list[str], int]:
    expected: dict[str, int] = {t: n for t, n in entry["tables"].items() if tables is None or t in tables}
    views = {v for v in entry.get("views", []) if tables is None or v in tables}
    schema_only = {t.split(".", 1)[1] for t in entry.get("schema_only_tables", [])}
    present = {r[0]: r[1] for r in eng.query(
        "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA = " + quote_literal(target))}
    problems = [f"table `{t}` is missing" for t in expected if t not in present]
    problems += [f"view `{v}` is missing" for v in views if v not in present]
    names = [t for t in expected if t in present]
    for i in range(0, len(names), 50):
        batch = names[i:i + 50]
        sql = " UNION ALL ".join(f"SELECT {quote_literal(t)}, COUNT(*) FROM {quote_ident(target)}.{quote_ident(t)}"
                                 for t in batch)
        for name, count in eng.query(sql, timeout=3600):
            want = 0 if name in schema_only else expected[name]
            if exact_counts and int(count) != want:
                problems.append(f"table `{name}` has {count} rows, the backup has {want}")
    if tables is None:
        for kind, sql in (("routines", "SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = "),
                          ("triggers", "SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = "),
                          ("events", "SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA = ")):
            want = entry.get(kind, 0)
            got = int(eng.query(sql + quote_literal(target))[0][0])
            if exact_counts and got != want:
                problems.append(f"{got} {kind} restored, the backup has {want}")
    return problems, len(names)


def preflight(bset: BackupSet, source_db: str) -> dict:
    entry = bset.database(source_db)
    check = verify_file(bset.path / entry["file"], entry.get("sha256"), entry.get("size"))
    if not check.ok:
        raise AppError(f"Backup file {entry['file']} failed validation: {'; '.join(check.problems)}",
                       hint="Choose another backup. This one is damaged or incomplete.")
    return entry


def _wanted(entry: dict, tables: list[str] | None) -> set[str] | None:
    if not tables:
        return None
    known = set(entry["tables"]) | set(entry.get("views", []))
    unknown = [t for t in tables if t not in known]
    if unknown:
        raise AppError(f"Table(s) not in this backup: {', '.join(unknown)}",
                       hint="Use 'inspect' to see the tables a backup contains.")
    return set(tables)


def _alert_on_failure(operation: str):
    """Send a failure alert (never raises) for a restore that fails, then re-raise the error."""
    import functools

    def wrap(fn):
        @functools.wraps(fn)
        def inner(cfg, bset, source_db, target, tables=None, *args, **kwargs):
            try:
                return fn(cfg, bset, source_db, target, tables, *args, **kwargs)
            except Exception as exc:
                from . import notify
                notify.operation_failure(cfg, operation, exc, {
                    "Backup": getattr(bset, "id", "?"), "Database": f"{source_db} -> {target}",
                    "Tables": ", ".join(tables) if tables else "all"})
                raise
        return inner
    return wrap


@_alert_on_failure("Restore into a new database")
def restore_to_new(cfg, bset: BackupSet, source_db: str, target: str, tables: list[str] | None = None,
                   until: str | None = None) -> RestoreReport:
    """Restore into a database that does not exist yet. Never touches existing data."""
    check_target_name(target)
    started = time.monotonic()
    entry = preflight(bset, source_db)
    wanted = _wanted(entry, tables)
    with open_engine(cfg, "restore") as eng:
        if database_exists(eng, target):
            raise AppError(f"Database '{target}' already exists.",
                           hint="Pick a new name for a separate restore, or use --replace-live to overwrite a live "
                                "database (with a safety backup).")
        log.info("Restoring %s/%s into new database %s", bset.id, source_db, target)
        _create_database(eng, target, entry)
        try:
            load_dump(eng, bset.path / entry["file"], target, wanted, cfg.backup.max_allowed_packet_mb)
            if until:
                replay_binlogs(cfg, eng, entry, target, until)
        except BaseException:
            log.warning("Restore failed; removing the incomplete database %s", target)
            try:
                eng.query(f"DROP DATABASE IF EXISTS {quote_ident(target)}")
            except DbError as exc:
                log.error("Could not remove %s: %s", target, exc)
            raise
        problems, checked = verify_restored(eng, target, entry, wanted, exact_counts=not until)
    report = RestoreReport(target, not problems, problems, checked, duration=time.monotonic() - started)
    audit("restore", mode="new-database", backup=bset.id, source=source_db, target=target, tables=tables,
          until=until, ok=report.ok, problems=problems)
    return report


def foreign_key_links(cfg, database: str, tables: list[str]) -> list[str]:
    """Foreign keys between the given tables and other tables, as readable strings (best effort)."""
    if not tables:
        return []
    names = ", ".join(quote_literal(t) for t in tables)
    try:
        with open_engine(cfg, "backup") as eng:
            rows = eng.query(
                "SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                "FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = " + quote_literal(database) +
                f" AND REFERENCED_TABLE_NAME IS NOT NULL AND (TABLE_NAME IN ({names}) "
                f"OR REFERENCED_TABLE_NAME IN ({names}))")
    except AppError:
        return []
    return [f"{t}.{c} -> {rt}.{rc}" for t, c, rt, rc in rows if not (t in tables and rt in tables)]


def assert_server_stopped(cfg) -> None:
    if not cfg.restore.refuse_while_server_running:
        return
    procs = server_processes(cfg)
    if procs:
        raise AppError(
            f"The FiveM server is running (PID {', '.join(str(p.pid) for p in procs)}). Replacing live data now "
            "would be undone by the server, which keeps players, stashes and inventories in memory and writes them "
            "back later.",
            hint="Stop the server first (e.g. in txAdmin), restore, then start it again.")


@_alert_on_failure("Live database restore")
def replace_live(cfg, bset: BackupSet, source_db: str, target: str, tables: list[str] | None = None,
                 until: str | None = None, skip_staging: bool = False) -> RestoreReport:
    """Overwrite a live database (or only the given tables) with a backup. The caller must have obtained
    explicit confirmation from the user before calling this."""
    check_target_name(target)
    started = time.monotonic()
    assert_server_stopped(cfg)
    entry = preflight(bset, source_db)
    wanted = _wanted(entry, tables)
    report = RestoreReport(target, False)

    with open_engine(cfg, "restore") as eng:
        exists = database_exists(eng, target)
        if exists:
            log.info("Taking safety backup of %s before restoring", target)
            safety = BackupRunner(cfg).run("pre-restore", note=f"Automatic safety backup before restoring {bset.id}",
                                           pinned=True, databases=[target], complete=True)
            report.safety_backup = safety.set_id
            log.info("Safety backup %s created", safety.set_id)

        if not skip_staging:
            staging = f"{target[:40]}__chk_{datetime.now():%Y%m%d%H%M%S}"
            log.info("Test-loading the backup into scratch database %s", staging)
            _create_database(eng, staging, entry)
            try:
                try:
                    load_dump(eng, bset.path / entry["file"], staging, wanted, cfg.backup.max_allowed_packet_mb)
                    problems, _ = verify_restored(eng, staging, entry, wanted)
                except DbError as exc:
                    problems = [str(exc)]
                if problems:
                    raise AppError("The backup did not load cleanly into a scratch database; the live database was "
                                   "not changed: " + "; ".join(problems[:5]),
                                   hint="Choose a different backup, or inspect this one with --target-db.")
            finally:
                eng.query(f"DROP DATABASE IF EXISTS {quote_ident(staging)}")

        try:
            if wanted is None:
                eng.query(f"DROP DATABASE IF EXISTS {quote_ident(target)}")
                _create_database(eng, target, entry)
            elif not exists:
                _create_database(eng, target, entry)
            load_dump(eng, bset.path / entry["file"], target, wanted, cfg.backup.max_allowed_packet_mb)
            if until:
                replay_binlogs(cfg, eng, entry, target, until)
            problems, checked = verify_restored(eng, target, entry, wanted, exact_counts=not until)
            if problems and not until:
                raise AppError("Verification after restore failed: " + "; ".join(problems[:5]))
            report.ok, report.problems, report.checked_tables = not problems, problems, checked
        except BaseException as exc:
            log.error("Restore into %s failed: %s", target, exc)
            if report.safety_backup:
                _rollback(cfg, eng, report.safety_backup, target)
                report.rolled_back = True
                log.warning("Rolled %s back to its state before the restore", target)
            audit("restore", mode="replace-live", backup=bset.id, source=source_db, target=target, tables=tables,
                  until=until, ok=False, error=str(exc), rolled_back=report.rolled_back,
                  safety_backup=report.safety_backup)
            if isinstance(exc, AppError) and report.rolled_back:
                exc.hint = (exc.hint + " " if exc.hint else "") + \
                    f"The database was rolled back from safety backup {report.safety_backup}."
            raise
    report.duration = time.monotonic() - started
    audit("restore", mode="replace-live", backup=bset.id, source=source_db, target=target, tables=tables,
          until=until, ok=report.ok, problems=report.problems, safety_backup=report.safety_backup)
    return report


def _rollback(cfg, eng, safety_id: str, target: str) -> None:
    bset = resolve_set(cfg.backup_dir, safety_id)
    entry = bset.database(target)
    eng.query(f"DROP DATABASE IF EXISTS {quote_ident(target)}")
    _create_database(eng, target, entry)
    load_dump(eng, bset.path / entry["file"], target, None, cfg.backup.max_allowed_packet_mb)
    problems, _ = verify_restored(eng, target, entry)
    if problems:
        raise AppError(f"ROLLBACK INCOMPLETE for '{target}': " + "; ".join(problems[:5]),
                       hint=f"Restore safety backup {safety_id} manually with the restore command.")
