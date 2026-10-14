"""'doctor' (full diagnostics) and 'status' (quick overview)."""

import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import config as config_mod
from . import service
from .backup import TRANSACTIONAL_ENGINES, resolve_databases
from .catalog import list_sets
from .engines import DumpOptions, load_tools, open_engine
from .engines.mysql import classify_error, stderr_reader
from .errors import AppError
from .fsutil import broad_access, free_bytes, human_size, path_fits
from .procwatch import server_processes
from .remote import destination
from .state import State, parse_iso


@dataclass
class Check:
    level: str
    title: str
    detail: str = ""
    hint: str | None = None


def _age(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    seconds = int((datetime.now().astimezone() - dt).total_seconds())
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h ago"
    return f"{seconds / 86400:.1f} days ago"


def _dry_run_dump(eng, info, database: str) -> str | None:
    proc = eng.start_dump(database, DumpOptions(no_data=True, binlog_position=info.log_bin, low_priority=True), info)
    errors = stderr_reader(proc.stderr)
    for _ in iter(lambda: proc.stdout.read(1 << 16), b""):
        pass
    if proc.wait() != 0:
        return str(classify_error(errors.text))
    return None


def _database_checks(cfg, checks: list[Check]) -> None:
    with open_engine(cfg, "backup") as eng:
        info = eng.server_info()
        where = cfg.database.socket or f"{cfg.database.host}:{cfg.database.port}"
        checks.append(Check("OK", "Database connection",
                            f"{info.flavor} {info.version} at {where} as '{cfg.database.user}'"))
        if info.flavor != eng.tools.flavor:
            checks.append(Check("WARN", "Tool mismatch",
                                f"Server is {info.flavor} but the dump tool is {eng.tools.flavor}.",
                                "Point database.client_bin_dir at tools from the same product as the server."))
        elif eng.tools.version_tuple[:2] < info.version_tuple[:2]:
            checks.append(Check("WARN", "Old client tools",
                                f"Dump tool {'.'.join(map(str, eng.tools.version_tuple))} is older than server "
                                f"{info.version}.", "Use the client tools shipped with the server version."))

        databases = resolve_databases(cfg, eng.list_databases())
        tables = eng.tables(databases)
        size = sum(t.size_bytes for t in tables)
        checks.append(Check("OK", "Databases selected",
                            f"{', '.join(databases)} ({len(tables)} tables, about {human_size(size)} on the server)"))
        unsafe = [f"{t.database}.{t.name} ({t.engine})" for t in tables
                  if t.engine and t.engine.lower() not in TRANSACTIONAL_ENGINES]
        if unsafe:
            checks.append(Check("WARN", "Non-transactional tables", ", ".join(unsafe[:8]),
                                "These tables can change during a backup. Convert them to InnoDB if possible."))

        if cfg.selected_tables.tables:
            existing = {f"{t.database}.{t.name}" for t in tables}
            missing = [r for r in cfg.selected_tables.tables if r not in existing]
            if missing:
                checks.append(Check("FAIL", "Selected tables", "Not found on the server: " + ", ".join(missing),
                                    "Check spelling and case; list real names with:  sqlbackup tables available"))
            else:
                checks.append(Check("OK", "Selected tables", f"{len(cfg.selected_tables.tables)} table(s) get "
                                    f"separate copies in {cfg.selected_dir}"))

        failures = [f"{d}: {err}" for d in databases if (err := _dry_run_dump(eng, info, d))]
        if failures:
            checks.append(Check("FAIL", "Backup privileges", "; ".join(failures),
                                "Grant the missing privileges; see:  sqlbackup grant-sql"))
        else:
            checks.append(Check("OK", "Backup privileges", "Schema dry-run dump succeeded for every database"))

        if info.log_bin:
            expire = info.variables.get("binlog_expire_logs_seconds") or "0"
            checks.append(Check("OK", "Point-in-time recovery",
                                f"Binary logging is ON (format {info.variables.get('binlog_format')}, "
                                f"expire after {int(float(expire)) // 86400 or 'unlimited'} days)."))
        else:
            checks.append(Check("INFO", "Point-in-time recovery",
                                "Binary logging is OFF: restores go back to the moment of a backup only.",
                                "Optional and advanced: needs log-bin added to the database my.ini and a database restart."))

        if cfg.database.user.lower() == "root":
            checks.append(Check("WARN", "Backup account",
                                "Backups use the 'root' account, which can change or delete anything.",
                                "Create a read-only backup account:  sqlbackup grant-sql"))
        try:
            rows = eng.query("SELECT User, Host FROM mysql.user WHERE "
                             "(authentication_string = '' OR authentication_string IS NULL) "
                             "AND plugin IN ('', 'mysql_native_password') AND User NOT IN ('', 'mariadb.sys', 'PUBLIC')")
            if rows:
                accounts = ", ".join(f"'{u}'@'{h}'" for u, h in rows)
                remote = [h for _, h in rows if h not in ("localhost", "127.0.0.1", "::1")]
                listening_all = info.variables.get("bind_address", "") in ("", "*", "0.0.0.0", "::")
                level = "WARN"
                detail = f"Accounts without a password: {accounts}."
                if listening_all:
                    detail += " The server accepts network connections on all interfaces."
                checks.append(Check(level, "Database security", detail,
                                    "Set passwords on these accounts" + (
                                        " and block port 3306 in the firewall" if listening_all or remote else "")
                                    + ". Update the game server's connection string at the same time."))
        except AppError:
            pass


def run_doctor(config_arg: str | None) -> list[Check]:
    checks: list[Check] = []
    try:
        cfg = config_mod.load(config_arg)
        checks.append(Check("OK", "Configuration", str(cfg.path)))
    except AppError as exc:
        return [Check("FAIL", "Configuration", str(exc), exc.hint)]

    if cfg.security.restrict_permissions:
        for label, path in (("config file", cfg.path), ("secret store", cfg.secrets_path),
                            ("backup folder", cfg.backup_dir), ("log folder", cfg.log_dir)):
            broad = broad_access(path)
            if broad:
                checks.append(Check("WARN", "File permissions", f"The {label} {path} is accessible to: "
                                    + ", ".join(broad), "Run:  sqlbackup fix-permissions"))

    try:
        tools = load_tools(cfg)
        checks.append(Check("OK", "Client tools", f"{tools.flavor} tools in {tools.bin_dir} ({tools.version})"))
    except AppError as exc:
        checks.append(Check("FAIL", "Client tools", str(exc), exc.hint))
        return checks

    try:
        _database_checks(cfg, checks)
    except AppError as exc:
        checks.append(Check("FAIL", "Database", str(exc), exc.hint))

    backup_dir = cfg.backup_dir
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=backup_dir, prefix=".write-test-"):
            pass
        free = free_bytes(backup_dir)
        level = "OK" if free > cfg.disk.warn_free_mb * 1024 * 1024 else (
            "WARN" if free > cfg.disk.min_free_mb * 1024 * 1024 else "FAIL")
        checks.append(Check(level, "Backup folder", f"{backup_dir} (writable, {human_size(free)} free)"))
        if not path_fits(backup_dir, 120):
            checks.append(Check("FAIL", "Backup folder path length",
                                f"{len(str(backup_dir))} characters; backup files would exceed the Windows path limit.",
                                "Use a shorter backup.directory or enable Windows long paths."))
    except OSError as exc:
        checks.append(Check("FAIL", "Backup folder", f"{backup_dir}: {exc}"))
    if not cfg.remote.enabled:
        checks.append(Check("INFO", "Off-site copy", "No remote destination configured. A disk failure, theft or "
                            "ransomware could destroy the database and every backup at once.",
                            "Set [remote] to another disk, NAS or cloud folder."))

    sets = list_sets(backup_dir)
    valid = [s for s in sets if s.valid]
    if valid:
        newest = valid[0]
        checks.append(Check("OK", "Backups", f"{len(valid)} backups, {human_size(sum(s.size for s in valid))}; "
                                             f"newest {newest.id} ({_age(newest.created)})"))
        sched = cfg.schedule
        expected = timedelta(minutes=sched.interval_minutes) if sched.interval_minutes else timedelta(days=1)
        if service.installed(cfg) and datetime.now().astimezone() - newest.created > expected * 2 + timedelta(hours=1):
            checks.append(Check("WARN", "Backup freshness", f"The newest backup is {_age(newest.created)}.",
                                "Check the log file and that the background service is running."))
    else:
        checks.append(Check("WARN", "Backups", "No backups yet.", "Create one now:  sqlbackup backup"))
    broken = [s.id for s in sets if s.status in ("INVALID", "FAILED")]
    if broken:
        checks.append(Check("WARN", "Damaged backups", ", ".join(broken[:5]), "Run:  sqlbackup verify --all"))

    st = State(cfg.state_path).load()
    last_ok, last_bad = parse_iso(st.get("last_success_at")), parse_iso(st.get("last_failure_at"))
    if last_bad and (not last_ok or last_bad > last_ok):
        checks.append(Check("FAIL", "Last backup attempt", f"Failed {_age(last_bad)}: {st.get('last_error', '')}",
                            f"See the log in {cfg.log_dir}"))

    if service.installed(cfg):
        running = service.is_running(cfg)
        beat = parse_iso(st.get("service", {}).get("heartbeat_at"))
        checks.append(Check("OK" if running else "WARN", "Background service",
                            f"installed, {'running' if running else 'NOT running'} (last heartbeat {_age(beat)})",
                            None if running else "Start it:  sqlbackup service start"))
    else:
        checks.append(Check("WARN", "Background service", "not installed (no automatic backups)",
                            "Install it:  sqlbackup service install"))

    if cfg.server_watch.enabled:
        try:
            procs = server_processes(cfg)
            checks.append(Check("INFO", "FiveM server", f"running (PID {', '.join(str(p.pid) for p in procs)})"
                                if procs else "not detected as running"))
        except OSError as exc:
            checks.append(Check("WARN", "FiveM server", f"process detection failed: {exc}"))

    dest = destination(cfg)
    if dest:
        try:
            dest.check()
            pending = st.get("pending_remote", [])
            checks.append(Check("WARN" if pending else "OK", "Remote copy", dest.describe() + (
                f" ({len(pending)} backup(s) waiting to be copied)" if pending else " reachable")))
        except (AppError, OSError) as exc:
            checks.append(Check("FAIL", "Remote copy", f"{dest.describe()}: {exc}"))
    if cfg.notify.enabled:
        from . import notify
        try:
            have = [k for k in notify.KINDS if notify.webhook_for(cfg, k)]
            missing = [k for k in notify.KINDS if k not in have]
            if have:
                checks.append(Check("WARN" if missing else "OK", "Discord alerts",
                                    f"webhooks set for: {', '.join(have)}" +
                                    (f"; none for: {', '.join(missing)}" if missing else ""),
                                    "Set notify.webhook_url (used for every type) or the per-type URLs, then run: "
                                    "sqlbackup notify-test"))
            else:
                checks.append(Check("WARN", "Discord alerts", "enabled but no webhook URL configured",
                                    "Paste your Discord webhook URL into notify.webhook_url in config.lua"))
        except AppError as exc:
            checks.append(Check("FAIL", "Discord alerts", str(exc), exc.hint))
    if cfg.inactive_tables.enabled:
        from . import activity
        n = len(activity._load(cfg).get("tables", {}))
        checks.append(Check("INFO", "Inactive table report",
                            f"every {cfg.inactive_tables.report_every_days} days, threshold "
                            f"{cfg.inactive_tables.inactive_after_days} days; history for {n} table(s)"))
    return checks


def status_lines(cfg) -> list[str]:
    st = State(cfg.state_path).load()
    sets = [s for s in list_sets(cfg.backup_dir) if s.valid]
    last_ok, last_bad = parse_iso(st.get("last_success_at")), parse_iso(st.get("last_failure_at"))
    beat = parse_iso(st.get("service", {}).get("heartbeat_at"))
    try:
        procs = server_processes(cfg)
        server = f"running (PID {', '.join(str(p.pid) for p in procs)})" if procs else "not running"
    except OSError:
        server = "unknown"
    sched = cfg.schedule
    plan = []
    if sched.interval_minutes:
        plan.append(f"every {sched.interval_minutes} min" + (" while the server runs" if
                                                             sched.only_when_server_running else ""))
    if sched.daily_times:
        plan.append("daily at " + ", ".join(sched.daily_times))
    if sched.on_server_start:
        plan.append("after server start")
    if sched.on_server_stop:
        plan.append("after server stop")
    lines = [
        f"Instance:        {cfg.general.instance_name}   (config: {cfg.path})",
        f"Service:         {'installed' if service.installed(cfg) else 'NOT installed'}, "
        f"{'running' if service.is_running(cfg) else 'not running'} (heartbeat {_age(beat)})",
        f"FiveM server:    {server}",
        f"Schedule:        {'; '.join(plan) or 'manual only'}",
        f"Last success:    {_age(last_ok)}" + (f"  ({st.get('last_success_id')})" if last_ok else ""),
    ]
    if last_bad and (not last_ok or last_bad > last_ok):
        lines.append(f"Last FAILURE:    {_age(last_bad)}: {st.get('last_error', '')[:300]}")
    lines.append(f"Backups:         {len(sets)} in {cfg.backup_dir} ({human_size(sum(s.size for s in sets))})")
    if sets:
        lines.append(f"Oldest/newest:   {sets[-1].created:%Y-%m-%d %H:%M} / {sets[0].created:%Y-%m-%d %H:%M}")
    if st.get("pending_remote"):
        lines.append(f"Remote pending:  {len(st['pending_remote'])} backup(s) not yet copied off-site")
    return lines
