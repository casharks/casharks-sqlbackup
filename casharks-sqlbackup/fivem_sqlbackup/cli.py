"""Command-line interface."""

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import APP_NAME, __author__, __version__, config as config_mod, credentials
from .catalog import list_sets, resolve_set, set_pinned, verify_set
from .errors import Aborted, AppError
from .fsutil import human_size, restrict
from .logsetup import audit, get_logger, setup_logging

log = get_logger("cli")


def _load(args, require_complete: bool = True):
    cfg = config_mod.load(args.config, require_complete)
    setup_logging(cfg.log_dir, cfg.logging.level, cfg.logging.max_file_mb, cfg.logging.keep_files,
                  console=True, verbose=args.verbose)
    return cfg


def _interactive() -> bool:
    return bool(sys.stdin and sys.stdin.isatty())


def _confirm(prompt: str) -> bool:
    return _interactive() and input(f"{prompt} (y/n) [n]: ").strip().lower() in ("y", "yes")



def cmd_setup(args) -> int:
    setup_logging(None, console=True, verbose=args.verbose)
    from .wizard import run_setup
    return run_setup(args.config)


def cmd_doctor(args) -> int:
    from .doctor import run_doctor
    try:
        cfg = config_mod.load(args.config)
        setup_logging(cfg.log_dir, cfg.logging.level, console=True, verbose=args.verbose)
    except AppError:
        setup_logging(None, console=True, verbose=args.verbose)
    checks = run_doctor(args.config)
    marks = {"OK": "[ OK ]", "INFO": "[INFO]", "WARN": "[WARN]", "FAIL": "[FAIL]"}
    for c in checks:
        print(f"{marks[c.level]} {c.title}: {c.detail}")
        if c.hint and c.level != "OK":
            print(f"       -> {c.hint}")
    failed = sum(c.level == "FAIL" for c in checks)
    warned = sum(c.level == "WARN" for c in checks)
    print(f"\n{failed} problem(s), {warned} warning(s).")
    return 1 if failed else 0


def cmd_status(args) -> int:
    from .doctor import status_lines
    cfg = _load(args)
    print("\n".join(status_lines(cfg)))
    return 0


def cmd_backup(args) -> int:
    from .backup import perform_backup
    cfg = _load(args)
    print(f"Backing up {', '.join(args.database or cfg.backup.databases)} ...")
    result = perform_backup(cfg, "manual", note=args.note or "", pinned=args.pin, databases=args.database or None,
                            housekeeping=not args.no_housekeeping)
    if result.status == "success":
        print(f"OK  {result.set_id}  ({human_size(result.size)}, {result.duration:.1f}s)\n    {result.path}")
        for item in result.selected:
            if item["ok"]:
                print(f"    + table copy {item['table']} ({item['rows']} rows)")
        for w in result.warnings:
            print(f"WARNING: {w}")
        return 0
    if result.status == "skipped":
        print(f"Skipped: {result.error}")
        return 3
    print(f"FAILED: {result.error}", file=sys.stderr)
    if result.hint:
        print(f"Hint: {result.hint}", file=sys.stderr)
    return 1


def cmd_run(args) -> int:
    from .daemon import Daemon
    cfg = _load(args)
    return Daemon(cfg).run()


def _print_sets(sets) -> None:
    if not sets:
        print("No backups found.")
        return
    print(f"{'#':>3}  {'Backup':<46} {'Created':<16} {'Trigger':<12} {'Size':>9}  {'Status':<10} Databases")
    for i, s in enumerate(sets, 1):
        pin = "*" if s.pinned else " "
        print(f"{i:>3}{pin} {s.id:<46} {s.created:%Y-%m-%d %H:%M} {s.trigger:<12} {human_size(s.size):>9}  "
              f"{s.status:<10} {', '.join(s.database_names) or '-'}")
    if any(s.pinned for s in sets):
        print("  * pinned (kept regardless of retention)")


def cmd_list(args) -> int:
    cfg = _load(args)
    sets = list_sets(cfg.backup_dir)
    if args.json:
        print(json.dumps([{"number": i, "id": s.id, "created": s.created.isoformat(timespec="seconds"),
                           "trigger": s.trigger, "size": s.size, "status": s.status, "pinned": s.pinned,
                           "databases": s.database_names, "path": str(s.path)}
                          for i, s in enumerate(sets, 1)], indent=2))
    else:
        _print_sets(sets)
    return 0


def cmd_inspect(args) -> int:
    cfg = _load(args)
    bset = resolve_set(cfg.backup_dir, args.backup)
    print(f"Backup:   {bset.id}\nFolder:   {bset.path}\nStatus:   {bset.status}{'  (pinned)' if bset.pinned else ''}")
    if not bset.valid:
        print(f"Problem:  {bset.error}")
        return 1
    m = bset.manifest
    print(f"Created:  {bset.created:%Y-%m-%d %H:%M:%S %Z}   trigger: {m['trigger']}   took {m['duration_seconds']}s")
    if m.get("note"):
        print(f"Note:     {m['note']}")
    print(f"Server:   {m['server']['flavor']} {m['server']['version']}   dump tool: {m['tools']['dump_version']}")
    v = bset.verification
    if v:
        print(f"Verified: {v['checked_at']}  {'OK' if v['ok'] else 'FAILED: ' + json.dumps(v.get('problems'))}")
    for w in m.get("warnings", []):
        print(f"Warning:  {w}")
    for d in m["databases"]:
        total = sum(d["tables"].values())
        print(f"\nDatabase {d['name']}: {d['file']} ({human_size(d['size'])}, {human_size(d['uncompressed_bytes'])} "
              f"uncompressed), {len(d['tables'])} tables, {total} rows, {len(d['views'])} views, "
              f"{d['routines']} routines, {d['triggers']} triggers, {d['events']} events")
        if d.get("binlog"):
            print(f"  Binary log position: {d['binlog']['file']}:{d['binlog']['position']} (point-in-time ready)")
        if args.tables or len(d["tables"]) <= 60:
            for t, n in d["tables"].items():
                print(f"  {t:<40} {n:>10} rows")
        else:
            print("  (use --tables to list all tables)")
    return 0


def cmd_verify(args) -> int:
    cfg = _load(args)
    if args.all:
        targets = list_sets(cfg.backup_dir)
    elif args.backup:
        targets = [resolve_set(cfg.backup_dir, args.backup)]
    else:
        targets = [resolve_set(cfg.backup_dir, "latest")]
    bad = 0
    for bset in targets:
        ok, checks = verify_set(bset)
        print(f"{'OK    ' if ok else 'FAILED'} {bset.id}")
        for c in checks:
            for p in c.problems:
                print(f"         {Path(c.path).name}: {p}")
        bad += not ok
        audit("verify", backup=bset.id, ok=ok)
    print(f"\n{len(targets) - bad} of {len(targets)} backup(s) passed verification.")
    return 1 if bad else 0


def _pick(prompt: str, options: list[str]) -> int:
    while True:
        answer = input(f"{prompt} [1-{len(options)}, q to cancel]: ").strip().lower()
        if answer in ("q", "quit", ""):
            raise Aborted("Cancelled.")
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1


def _consequences(bset, target: str, tables) -> str:
    what = f"the tables {', '.join(tables)} in '{target}'" if tables else f"the whole database '{target}'"
    return (f"\nThis REPLACES {what} with the backup from {bset.created:%Y-%m-%d %H:%M}.\n"
            f"Everything saved after that moment in {'those tables' if tables else 'it'} will be lost: player "
            "progress, money, items, vehicles, stash contents and so on.\n"
            "A safety backup of the current data is taken first and kept (pinned), so this can be undone.\n"
            "The FiveM server must be stopped while the restore runs.")


def _guided_restore(cfg, args) -> tuple:
    sets = [s for s in list_sets(cfg.backup_dir) if s.valid][:20]
    if not sets:
        raise AppError("No backups available.")
    _print_sets(sets)
    bset = sets[_pick("\nWhich backup?", [s.id for s in sets])]
    names = bset.database_names
    source = names[0] if len(names) == 1 else names[_pick("Which database? " + ", ".join(
        f"{i}) {n}" for i, n in enumerate(names, 1)), names)]
    print("\nWhat do you want to do?\n"
          "  1) Restore into a NEW separate database to look at or copy from (safe, recommended)\n"
          "  2) Restore only some tables into a NEW separate database\n"
          "  3) REPLACE the live database with this backup\n"
          "  4) REPLACE only some tables of the live database")
    mode = _pick("Choose", ["new", "new-tables", "replace", "replace-tables"])
    tables = None
    if mode in (1, 3):
        print("Tables in this backup: " + ", ".join(bset.database(source)["tables"]))
        tables = [t.strip() for t in input("Tables (comma separated): ").split(",") if t.strip()]
        if not tables:
            raise Aborted("No tables given.")
    if mode in (0, 1):
        default = f"{source[:40]}_restored_{datetime.now():%Y%m%d_%H%M}"
        target = input(f"Name for the new database [{default}]: ").strip() or default
        return bset, source, target, tables, False
    target = input(f"Live database to replace [{source}]: ").strip() or source
    return bset, source, target, tables, True


def cmd_restore(args) -> int:
    from .restore import replace_live, restore_to_new
    cfg = _load(args)
    tables = [t.strip() for t in args.tables.split(",") if t.strip()] if args.tables else None
    if args.backup is None:
        if not _interactive():
            raise AppError("Specify a backup (see 'list') or run restore in an interactive terminal.")
        bset, source, target, tables, live = _guided_restore(cfg, args)
    else:
        bset = resolve_set(cfg.backup_dir, args.backup)
        names = bset.database_names
        source = args.database or (names[0] if len(names) == 1 else None)
        if source is None:
            raise AppError("This backup contains several databases; choose one with --database.",
                           hint=", ".join(names))
        live = args.replace_live
        target = args.target_db or (source if live else None)
        if target is None:
            raise AppError("Choose where to restore: --target-db NEW_NAME (safe) or --replace-live.")

    if args.until and not (len(args.until) == 19 and args.until[4] == "-" and args.until[13] == ":"):
        raise AppError("--until must look like 'YYYY-MM-DD HH:MM:SS' (local time).")

    _warn_related(cfg, source, tables)
    if live:
        print(_consequences(bset, target, tables))
        _confirm_replace(target, args.yes_replace)
        report = replace_live(cfg, bset, source, target, tables, args.until, args.skip_staging)
    else:
        print(f"Restoring {bset.id} ({source}) into new database '{target}' ...")
        report = restore_to_new(cfg, bset, source, target, tables, args.until)

    if report.safety_backup:
        print(f"Safety backup of the previous data: {report.safety_backup}")
    if report.ok:
        print(f"OK  Restored into '{report.target}' and verified {report.checked_tables} table(s) against the backup "
              f"({report.duration:.1f}s).")
        if not live:
            print(f"Open '{report.target}' in HeidiSQL (or any SQL tool) to inspect or copy data. Drop it when done.")
        return 0
    print("Restore finished but verification found problems:", file=sys.stderr)
    for p in report.problems:
        print(f"  - {p}", file=sys.stderr)
    return 1


SINGLE_TABLE_NOTE = ("Note: restoring single tables does not restore related data in other tables. FiveM data is "
                     "often linked by citizenid, plate or stash name (players <-> inventories, vehicles, houses...). "
                     "Check that the related rows still match after restoring.")


def _warn_related(cfg, database: str, tables: list[str] | None) -> None:
    from .restore import foreign_key_links
    if not tables:
        return
    print(SINGLE_TABLE_NOTE)
    links = foreign_key_links(cfg, database, tables)
    if links:
        print("Foreign keys link these tables to others (restore those too if their rows changed):")
        for link in links:
            print(f"  {link}")


def _confirm_replace(target: str, yes_replace: str | None) -> None:
    if yes_replace:
        if yes_replace != target:
            raise Aborted(f"--yes-replace must be exactly the database name '{target}'.")
        return
    if not _interactive():
        raise Aborted("Refusing to replace live data without confirmation.",
                      hint=f"Add --yes-replace {target} to confirm non-interactively.")
    if input(f"\nType the database name '{target}' to confirm, anything else cancels: ").strip() != target:
        raise Aborted("Cancelled. Nothing was changed.")


def cmd_tables(args) -> int:
    from . import selected
    from .restore import replace_live, restore_to_new
    cfg = _load(args)
    if args.action == "available":
        from .backup import resolve_databases
        from .engines import open_engine
        with open_engine(cfg) as eng:
            dbs = resolve_databases(cfg, eng.list_databases())
            infos = eng.tables(dbs)
        chosen = set(cfg.selected_tables.tables)
        print("Tables you can add to [selected_tables] tables = [...] (copy the database.table name exactly):")
        for t in infos:
            ref = f"{t.database}.{t.name}"
            print(f"  {'*' if ref in chosen else ' '} {ref:<50} {t.kind.lower():<10} {human_size(t.size_bytes):>9}")
        print("  * = already selected")
        return 0
    if not cfg.selected_tables.tables:
        raise AppError("No tables are configured in [selected_tables] tables.",
                       hint="Add them to the config file; list candidates with:  sqlbackup tables available")
    if args.action == "list":
        refs = [args.table] if args.table else cfg.selected_tables.tables
        for ref in refs:
            db, _, tb = ref.partition(".")
            copies = sorted(selected.list_copies(cfg, db, tb), key=lambda c: c.created, reverse=True)
            print(f"\n{ref}   ({selected.table_dir(cfg, db, tb)})")
            if not copies:
                print("   no copies yet")
            for i, c in enumerate(copies, 1):
                rows = "?" if c.rows is None else c.rows
                print(f"  {i:>3}  {c.created:%Y-%m-%d %H:%M:%S}  {rows:>9} rows  {human_size(c.size):>9}  {c.path.name}")
        return 0

    if not args.table:
        raise AppError("Give the table to restore, e.g.:  sqlbackup tables restore my_database.player_lockers")
    copy = selected.resolve_copy(cfg, args.table, args.version or "latest")
    source = selected.TableCopySet(copy)
    print(f"Table copy: {copy.database}.{copy.table} from {copy.created:%Y-%m-%d %H:%M:%S} ({copy.rows} rows)\n"
          f"File:       {copy.path}")
    _warn_related(cfg, copy.database, [copy.table])
    if args.replace_live:
        target = copy.database
        print(f"\nThis REPLACES table '{copy.table}' in the live database '{target}' with the copy above. Changes to "
              "that table after that moment are lost. A safety backup of the whole database is taken first.\n"
              "The FiveM server must be stopped while the restore runs.")
        _confirm_replace(target, args.yes_replace)
        report = replace_live(cfg, source, copy.database, target, [copy.table], skip_staging=args.skip_staging)
    else:
        target = args.target_db or f"{copy.database[:30]}_{copy.table[:20]}_{datetime.now():%m%d%H%M}"
        print(f"Restoring into new database '{target}' ...")
        report = restore_to_new(cfg, source, copy.database, target, [copy.table])
    if report.safety_backup:
        print(f"Safety backup of the previous data: {report.safety_backup}")
    if report.ok:
        print(f"OK  '{copy.table}' restored into '{report.target}' and verified ({report.duration:.1f}s).")
        if not args.replace_live:
            print(f"Inspect it in HeidiSQL, copy what you need into the live table, then drop '{report.target}'.")
        return 0
    for p in report.problems:
        print(f"  - {p}", file=sys.stderr)
    return 1


def cmd_notify_test(args) -> int:
    from . import notify
    cfg = _load(args, require_complete=False)
    kinds = notify.KINDS if args.kind == "all" else [args.kind]
    failed = 0
    for kind in kinds:
        if not notify.webhook_for(cfg, kind):
            print(f"  {kind:<9} no webhook configured (set notify.{kind}_webhook_url or notify.webhook_url)")
            failed += 1
            continue
        ok = notify.test(cfg, kind)
        print(f"  {kind:<9} {'sent - check Discord' if ok else 'FAILED (see logs/sqlbackup.log)'}")
        failed += not ok
    return 1 if failed else 0


def cmd_inactive(args) -> int:
    from . import activity
    cfg = _load(args)
    report = activity.send_report(cfg, force=True) if args.send else activity.build(cfg)
    labels = {"confirmed": "CONFIRMED (identical data in every backup)",
              "estimate": "ESTIMATE (database statistics only)", "unknown": "UNKNOWN (not enough evidence yet)"}
    print(f"Tables unchanged for at least {cfg.inactive_tables.inactive_after_days} days. Report only - nothing is "
          "changed or deleted.\n")
    for status, label in labels.items():
        items = report.by(status)
        print(f"{label}: {len(items)}")
        for f in items:
            days = f"{f.unchanged_days:.0f}+ days" if f.unchanged_days is not None else "?"
            print(f"  {f.table:<45} {days:>10}  rows={f.rows if f.rows is not None else '?'}  last activity: "
                  f"{f.last_activity}")
            print(f"      {f.evidence}")
    print(f"\nActive: {report.active}   Excluded: {report.excluded}")
    for note in report.notes:
        print(f"Note: {note}")
    if args.send:
        print("Report sent to the inactive-table webhook (if configured).")
    return 0


def cmd_prune(args) -> int:
    from .retention import prune
    cfg = _load(args)
    removed = prune(cfg, dry_run=args.dry_run)
    verb = "Would remove" if args.dry_run else "Removed"
    for s in removed:
        print(f"{verb} {s.id}")
    print(f"{verb} {len(removed)} full backup(s).")
    if cfg.selected_tables.tables:
        from . import selected
        copies = selected.prune(cfg, dry_run=args.dry_run)
        for p in copies:
            print(f"{verb} table copy {p}")
        print(f"{verb} {len(copies)} separate table copy/copies.")
    return 0


def cmd_pin(args) -> int:
    cfg = _load(args)
    bset = resolve_set(cfg.backup_dir, args.backup)
    set_pinned(bset, args.command == "pin")
    audit(args.command, backup=bset.id)
    print(f"{'Pinned' if args.command == 'pin' else 'Unpinned'} {bset.id}")
    return 0


def cmd_credentials(args) -> int:
    from .engines import secret_store
    cfg = _load(args, require_complete=False)
    store = secret_store(cfg)
    if args.action == "list":
        names = store.names()
        for name, desc in credentials.KNOWN_SECRETS.items():
            print(f"  {'stored ' if name in names else 'not set'}  {name:<22} {desc}")
        for name in names:
            if name not in credentials.KNOWN_SECRETS:
                print(f"  stored   {name}")
        print(f"Secret store: {cfg.secrets_path} ({store.protection})")
        return 0
    if args.action == "test-webhook":
        args.kind = "all"
        return cmd_notify_test(args)
    if not args.name:
        raise AppError("Give the secret name, e.g. database.password", hint=", ".join(credentials.KNOWN_SECRETS))
    if args.action == "delete":
        print("Deleted." if store.delete(args.name) else "Nothing stored under that name.")
        return 0
    if args.name not in credentials.KNOWN_SECRETS:
        raise AppError(f"Unknown secret name '{args.name}'.", hint=", ".join(credentials.KNOWN_SECRETS))
    import getpass
    if args.stdin:
        value = sys.stdin.readline().rstrip("\r\n")
    else:
        if not _interactive():
            raise AppError("No terminal to type the secret into.", hint="Pipe it in with --stdin.")
        value = getpass.getpass(f"Value for {args.name} (input hidden): ")
        if not args.name.startswith("notify.") and getpass.getpass("Repeat to confirm: ") != value:
            raise Aborted("The two entries did not match. Nothing was stored.")
    if args.name.startswith("notify."):
        from .notify import validate_url
        validate_url(value)
    store.set(args.name, value)
    audit("credential_set", name=args.name)
    print(f"Stored {args.name} ({store.protection}).")
    return 0


def cmd_service(args) -> int:
    from . import service
    cfg = _load(args)
    if args.action == "install":
        python = Path(args.python) if args.python else None
        print(service.install(cfg, python))
        audit("service_install", task=cfg.task_name, mode=cfg.service.mode)
    elif args.action == "uninstall":
        print(service.uninstall(cfg))
        audit("service_uninstall", task=cfg.task_name)
    elif args.action == "start":
        print(service.start(cfg))
    elif args.action == "stop":
        print(service.stop(cfg))
    else:
        print(f"Installed: {'yes' if service.installed(cfg) else 'no'}   "
              f"Running: {'yes' if service.is_running(cfg) else 'no'}   Name: {cfg.task_name}")
    return 0


def cmd_grant_sql(args) -> int:
    from .engines import open_engine
    from .grants import backup_account_sql
    cfg = _load(args)
    try:
        with open_engine(cfg) as eng:
            info = eng.server_info()
            flavor, version, binlog = info.flavor, info.version_tuple, info.log_bin
    except AppError:
        flavor = cfg.database.engine if cfg.database.engine != "auto" else "mariadb"
        version, binlog = (99, 0, 0), False
    print("-- Run as an administrator (e.g. in HeidiSQL), replacing the password first.\n"
          "-- Then store that password with:  sqlbackup credentials set database.password\n"
          f"-- and set database.user = \"{args.user}\" in {cfg.path}\n")
    print(backup_account_sql(flavor, version, args.user, args.host, cfg.backup.databases, binlog,
                             "CHOOSE_A_STRONG_PASSWORD"))
    return 0


def cmd_fix_permissions(args) -> int:
    cfg = _load(args, require_complete=False)
    for path in (cfg.path, cfg.secrets_path, cfg.data_dir, cfg.log_dir, cfg.backup_dir):
        if path.exists():
            print(f"{'restricted' if restrict(path) else 'FAILED    '}  {path}")
    return 0


def cmd_uninstall(args) -> int:
    from . import service
    cfg = _load(args, require_complete=False)
    if cfg.general.instance_name:
        print(service.uninstall(cfg))
    if args.remove_config:
        if not args.yes and not _confirm(f"Delete the configuration, stored secrets, state and logs of {cfg.path}?"):
            raise Aborted("Cancelled. Configuration kept.")
        for path in (cfg.path, cfg.secrets_path):
            if path.exists():
                path.unlink()
        for folder in (cfg.data_dir, cfg.log_dir):
            if folder.exists() and folder != cfg.backup_dir:
                shutil.rmtree(folder, ignore_errors=True)
        print("Configuration, secrets, state and logs removed.")
    print(f"Backups were NOT touched: {cfg.backup_dir}\nDelete that folder yourself if you no longer need them.")
    return 0



def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sqlbackup", description=f"{APP_NAME} {__version__}: automatic backup and "
                                "recovery for FiveM MySQL/MariaDB databases.")
    p.add_argument("--config", help=f"configuration file (default: {config_mod.CONFIG_ENV} or config.lua next to "
                                    "the program)")
    p.add_argument("-v", "--verbose", action="store_true", help="show detailed progress")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__} by {__author__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser("setup", help="guided first-time setup").set_defaults(func=cmd_setup)
    sub.add_parser("doctor", help="check configuration, database access, backups and the service").set_defaults(
        func=cmd_doctor)
    sub.add_parser("status", help="short overview").set_defaults(func=cmd_status)

    b = sub.add_parser("backup", help="create a backup now")
    b.add_argument("--note", help="text stored with the backup")
    b.add_argument("--database", action="append", help="only this database (repeatable)")
    b.add_argument("--pin", action="store_true", help="keep this backup regardless of retention")
    b.add_argument("--no-housekeeping", action="store_true", help="skip retention and remote copy")
    b.set_defaults(func=cmd_backup)

    sub.add_parser("run", help="run the background service in this window").set_defaults(func=cmd_run)

    ls = sub.add_parser("list", help="list backups")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)

    i = sub.add_parser("inspect", help="show what a backup contains")
    i.add_argument("backup", help="number from 'list', backup name, or 'latest'")
    i.add_argument("--tables", action="store_true", help="always list every table")
    i.set_defaults(func=cmd_inspect)

    v = sub.add_parser("verify", help="check backup files are complete and undamaged")
    v.add_argument("backup", nargs="?", help="number, name or 'latest' (default)")
    v.add_argument("--all", action="store_true", help="verify every backup")
    v.set_defaults(func=cmd_verify)

    r = sub.add_parser("restore", help="restore a backup (guided when run without arguments)")
    r.add_argument("backup", nargs="?", help="number from 'list', backup name, or 'latest'")
    r.add_argument("--database", help="database inside the backup (needed if it holds several)")
    r.add_argument("--target-db", help="restore into this NEW database (safe)")
    r.add_argument("--replace-live", action="store_true", help="overwrite the live database (safety backup first)")
    r.add_argument("--tables", help="only these tables, comma separated")
    r.add_argument("--until", help="point-in-time: replay binary logs up to 'YYYY-MM-DD HH:MM:SS'")
    r.add_argument("--yes-replace", metavar="DBNAME", help="confirm --replace-live non-interactively")
    r.add_argument("--skip-staging", action="store_true", help="skip the scratch-database test load (large DBs)")
    r.set_defaults(func=cmd_restore)

    nt = sub.add_parser("notify-test", help="send a test message to the configured Discord webhook(s)")
    nt.add_argument("kind", nargs="?", default="all", choices=["all", "success", "failure", "warning", "inactive"])
    nt.set_defaults(func=cmd_notify_test)

    ia = sub.add_parser("inactive", help="show tables whose data has not changed for a long time (report only)")
    ia.add_argument("--send", action="store_true", help="also send the report to Discord now")
    ia.set_defaults(func=cmd_inactive)

    t = sub.add_parser("tables", help="separate copies of selected tables: available | list | restore")
    t.add_argument("action", choices=["available", "list", "restore"])
    t.add_argument("table", nargs="?", help="database.table (for list/restore)")
    t.add_argument("version", nargs="?", help="restore: number from 'tables list' or part of the name (default latest)")
    t.add_argument("--target-db", help="restore into this NEW database (default: an automatically named one)")
    t.add_argument("--replace-live", action="store_true", help="replace the table in the live database")
    t.add_argument("--yes-replace", metavar="DBNAME", help="confirm --replace-live non-interactively")
    t.add_argument("--skip-staging", action="store_true", help="skip the scratch-database test load")
    t.set_defaults(func=cmd_tables)

    pr = sub.add_parser("prune", help="apply the retention policy now")
    pr.add_argument("--dry-run", action="store_true", help="only show what would be removed")
    pr.set_defaults(func=cmd_prune)

    for name, text in (("pin", "keep a backup forever"), ("unpin", "let retention manage a backup again")):
        pp = sub.add_parser(name, help=text)
        pp.add_argument("backup")
        pp.set_defaults(func=cmd_pin)

    c = sub.add_parser("credentials", help="manage stored passwords and the webhook URL")
    c.add_argument("action", choices=["list", "set", "delete", "test-webhook"])
    c.add_argument("name", nargs="?", help=", ".join(credentials.KNOWN_SECRETS))
    c.add_argument("--stdin", action="store_true", help="read the value from standard input")
    c.set_defaults(func=cmd_credentials)

    s = sub.add_parser("service", help="automatic start (Windows Task Scheduler / systemd)")
    s.add_argument("action", choices=["install", "uninstall", "start", "stop", "status"])
    s.add_argument("--python", help="interpreter for the service (default: the one running this command)")
    s.set_defaults(func=cmd_service)

    g = sub.add_parser("grant-sql", help="print SQL that creates a least-privilege backup account")
    g.add_argument("--user", default="fivem_backup")
    g.add_argument("--host", default="localhost")
    g.set_defaults(func=cmd_grant_sql)

    sub.add_parser("fix-permissions", help="restrict config, secrets, logs and backups to your account").set_defaults(
        func=cmd_fix_permissions)

    u = sub.add_parser("uninstall", help="remove the scheduled task/service (backups are always kept)")
    u.add_argument("--remove-config", action="store_true", help="also delete config, secrets, state and logs")
    u.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    u.set_defaults(func=cmd_uninstall)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except AppError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if exc.hint:
            print(f"Hint: {exc.hint}", file=sys.stderr)
        log.debug("Command failed", exc_info=True)
        return exc.exit_code
    except EOFError:
        print("\nCancelled: no input available. Nothing was changed.", file=sys.stderr)
        return Aborted.exit_code
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
