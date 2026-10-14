"""Interactive first-time setup. Writes config.lua and stores secrets; never echoes passwords."""

import getpass
import re
import secrets
import sys
from pathlib import Path

from . import APP_NAME, config as config_mod, credentials
from .engines import find_tools, open_engine
from .errors import AppError, ConfigError
from .fivem import find_connection_string, locate_server_cfg, parse_connection_string
from .fsutil import IS_WINDOWS, atomic_write_text, human_size, restrict

HEADER = ""


def _ask(prompt: str, default: str | None = None, check=None) -> str:
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        answer = input(f"{prompt}{suffix}: ").strip()
        if not answer and default is not None:
            answer = default
        problem = check(answer) if check else None
        if problem:
            print(f"  ! {problem}")
            continue
        return answer


def _ask_int(prompt: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    def check(v):
        if not re.fullmatch(r"\d+", v):
            return "Enter a whole number."
        if int(v) < minimum or (maximum is not None and int(v) > maximum):
            return f"Enter a number between {minimum} and {maximum if maximum is not None else 'any'}."
        return None
    return int(_ask(prompt, str(default), check))


def _ask_bool(prompt: str, default: bool) -> bool:
    answer = _ask(f"{prompt} (y/n)", "y" if default else "n",
                  lambda v: None if v.lower() in ("y", "yes", "n", "no") else "Answer y or n.")
    return answer.lower().startswith("y")


def _ask_choice(prompt: str, options: list[tuple[str, str]], default: str) -> str:
    print(prompt)
    for i, (value, label) in enumerate(options, 1):
        print(f"  {i}) {label}{'  (default)' if value == default else ''}")
    keys = {str(i): v for i, (v, _) in enumerate(options, 1)}
    answer = _ask("Choose", next(k for k, v in keys.items() if v == default),
                  lambda v: None if v in keys else f"Enter 1-{len(options)}.")
    return keys[answer]


def _ask_list(prompt: str, default: list[str]) -> list[str]:
    answer = _ask(f"{prompt} (comma separated, '-' for none)", ", ".join(default) or "-")
    return [] if answer == "-" else [p.strip() for p in answer.split(",") if p.strip()]


def _new_password(label: str) -> str:
    while True:
        first = getpass.getpass(f"{label}: ")
        if not first:
            print("  ! The password cannot be empty (choose credential method 'none' for accounts without one).")
            continue
        if getpass.getpass("Repeat to confirm: ") == first:
            return first
        print("  ! The two entries did not match.")


def _section(title: str) -> None:
    print(f"\n== {title} ==")


def _detect_from_server_cfg() -> dict:
    raw = _ask("Path to your FiveM server.cfg or server-data folder (Enter to skip)", "")
    if not raw:
        return {}
    cfg_file = locate_server_cfg(Path(raw.strip('"')).expanduser())
    if not cfg_file:
        print("  ! No server.cfg found there; continuing without it.")
        return {}
    conn = find_connection_string(cfg_file)
    if not conn:
        print(f"  ! {cfg_file} has no mysql_connection_string; continuing without it.")
        return {}
    found = parse_connection_string(conn)
    shown = {k: v for k, v in found.items() if k != "password"}
    print(f"  Found database settings: {shown}" + ("  (a password is also set)" if found.get("password") else ""))
    return found


def _connect(values: dict, path: Path, password: str | None):
    cfg = config_mod.build(values, path, require_complete=False)
    eng = open_engine(cfg, "backup", password=password, use_password_arg=True)
    with eng:
        info = eng.server_info()
        databases = eng.list_databases()
        tables = eng.tables([d for d in databases if d.lower() not in config_mod.SYSTEM_DATABASES])
    return info, databases, tables


def run_setup(config_arg: str | None) -> int:
    if not (sys.stdin and sys.stdin.isatty()):
        raise ConfigError("Setup needs an interactive terminal.",
                          hint="Alternatively copy config.example.lua to config.lua and edit it.")
    path = config_mod.config_path(config_arg)
    values = config_mod.defaults()
    print(f"{APP_NAME} setup\nConfiguration file: {path}\nPress Enter to accept the value in [brackets].")
    if path.exists():
        try:
            values = config_mod.to_dict(config_mod.load(str(path), require_complete=False))
        except ConfigError as exc:
            print(f"  ! The existing file has problems and will be replaced:\n{exc}")
        if not _ask_bool("A configuration already exists. Update it?", True):
            return 0

    g, db, b, s, r = values["general"], values["database"], values["backup"], values["schedule"], values["retention"]

    _section("Server")
    g["instance_name"] = _ask("Short name for this server (letters, digits, - _)", g["instance_name"] or "fivem",
                              lambda v: None if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", v) else
                              "Use 1-40 letters, digits, '-' or '_'.")
    detected = _detect_from_server_cfg()

    _section("Automatic start")
    if IS_WINDOWS:
        values["service"]["mode"] = _ask_choice("When should the background service start?", [
            ("logon", "When I sign in to Windows (recommended; no admin rights needed)"),
            ("boot", "When Windows starts, even if nobody signs in (needs an administrator terminal)")],
            values["service"]["mode"])
        values["security"]["dpapi_scope"] = "machine" if values["service"]["mode"] == "boot" else "user"
    store = credentials.SecretStore(path.parent / "secrets.dat", values["security"]["dpapi_scope"])

    _section("Database client tools")
    while True:
        try:
            tools = find_tools(db["client_bin_dir"], None if db["engine"] == "auto" else db["engine"])
            print(f"  Using {tools.flavor} tools in {tools.bin_dir}\n  ({tools.version})")
            break
        except ConfigError as exc:
            print(f"  ! {exc}")
            db["client_bin_dir"] = _ask("Folder containing mariadb-dump / mysqldump", db["client_bin_dir"] or None)

    _section("Database connection")
    password: str | None = None
    while True:
        db["host"] = _ask("Database host", detected.get("host") or db["host"] or "127.0.0.1")
        db["port"] = _ask_int("Database port", detected.get("port") or db["port"], 1, 65535)
        db["user"] = _ask("Database user for backups", detected.get("user") or db["user"] or None)
        db["credential"] = _ask_choice("How should the password be provided?", [
            ("store", "Store it encrypted on this computer (recommended)"),
            ("none", "This account has no password"),
            ("env", "Read it from an environment variable")],
            "none" if (detected and not detected.get("password") and not db["credential"] == "env") else "store")
        if db["credential"] == "store":
            if detected.get("password") and _ask_bool("Use the password from server.cfg?", True):
                password = detected["password"]
            else:
                existing = store.get(credentials.DB_PASSWORD) if credentials.DB_PASSWORD in store.names() else None
                if existing and _ask_bool("Keep the password already stored?", True):
                    password = existing
                else:
                    password = _new_password(f"Password for '{db['user']}'")
        elif db["credential"] == "env":
            var = _ask("Environment variable name", "FIVEM_SQLBACKUP_DB_PASSWORD",
                       lambda v: None if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v) else "Invalid variable name.")
            db["credential"] = f"env:{var}"
            password = credentials.resolve(db["credential"], store, credentials.DB_PASSWORD, "database password") \
                if _ask_bool(f"Is {var} already set in this terminal (to test the connection)?", False) else None
        else:
            password = None
        b["databases"] = b["databases"] or ["*"]
        try:
            info, databases, tables = _connect(values, path, password)
            print(f"  Connected: {info.flavor} {info.version}")
            db["engine"] = info.flavor
            break
        except AppError as exc:
            print(f"  ! Connection failed: {exc}" + (f"\n    {exc.hint}" if exc.hint else ""))
            if not _ask_bool("Try different connection details?", True):
                databases, tables = [], []
                break
    if db["credential"] == "store" and password:
        store.set(credentials.DB_PASSWORD, password)
        print("  Password stored in the encrypted secret store.")

    _section("What to back up")
    user_dbs = [d for d in databases if d.lower() not in config_mod.SYSTEM_DATABASES]
    if user_dbs:
        sizes = {d: sum(t.size_bytes for t in tables if t.database == d) for d in user_dbs}
        for i, d in enumerate(user_dbs, 1):
            print(f"  {i}) {d}  ({sum(1 for t in tables if t.database == d)} tables, {human_size(sizes[d])})")
        default = detected.get("database")
        default_sel = str(user_dbs.index(default) + 1) if default in user_dbs else "*"
        if b["databases"] and b["databases"] != ["*"] and all(d in user_dbs for d in b["databases"]):
            default_sel = ",".join(str(user_dbs.index(d) + 1) for d in b["databases"])
        choice = _ask("Databases to back up (numbers separated by commas, or * for all)", default_sel,
                      lambda v: None if v == "*" or all(p.strip().isdigit() and 1 <= int(p) <= len(user_dbs)
                                                         for p in v.split(",")) else "Enter numbers from the list or *.")
        b["databases"] = ["*"] if choice == "*" else [user_dbs[int(p) - 1] for p in choice.split(",")]
    else:
        b["databases"] = _ask_list("Databases to back up", [d for d in b["databases"] if d != "*"] or
                                   ([detected["database"]] if detected.get("database") else []))
    b["exclude_tables"] = _ask_list("Tables to skip completely, as database.table", b["exclude_tables"])

    sel = values["selected_tables"]
    print("\nImportant tables (lockers, inventories, vehicles...) can ALSO be saved as separate, timestamped copies "
          "so you can find them without opening the full backup. They stay in the full backup as well.")
    known = {f"{t.database}.{t.name}" for t in tables}
    while True:
        sel["tables"] = _ask_list("Tables to keep separate copies of, as database.table", sel["tables"])
        unknown = [r for r in sel["tables"] if known and r not in known]
        if not unknown:
            break
        print(f"  ! Not found on the server: {', '.join(unknown)}")
        hints = [k for k in sorted(known) if any(u.split('.')[-1].lower() in k.lower() for u in unknown)][:10]
        if hints:
            print(f"    Similar names: {', '.join(hints)}")
    if sel["tables"]:
        sel["keep_last"] = _ask_int("Copies to keep per table", sel["keep_last"])

    _section("Backup storage")
    default_dir = b["directory"]
    while True:
        b["directory"] = _ask("Backup folder (relative paths start from the config folder)", default_dir)
        target = (path.parent / b["directory"]).resolve() if not Path(b["directory"]).is_absolute() \
            else Path(b["directory"])
        if any(part.lower() == "resources" for part in target.parts):
            print("  ! This folder is inside a FiveM 'resources' folder. Backups there can be lost when resources "
                  "are updated or redeployed, and may be shared with people who can access server files.")
            if not _ask_bool("Use it anyway?", False):
                continue
        try:
            target.mkdir(parents=True, exist_ok=True)
            restrict(target)
            break
        except OSError as exc:
            print(f"  ! Cannot use {target}: {exc}")
    b["compression"] = _ask_choice("File type for backups:", [
        ("none", "Plain .sql files (open directly in HeidiSQL; larger)"),
        ("gzip", "Compressed .sql.gz files (about 5-10x smaller)")], b["compression"])
    print(f"Backups are named like '{b['name_format']}' with {{date}} as month-day-year, e.g. 10-14-26-FullServer. "
          "Change backup.name_format / backup.date_format in the config to use another pattern.")

    _section("Schedule")
    s["interval_minutes"] = _ask_int("Back up every how many minutes? (0 = off)", s["interval_minutes"])
    if s["interval_minutes"]:
        s["only_when_server_running"] = _ask_bool("Only while the FiveM server is running?",
                                                  s["only_when_server_running"])
    s["daily_times"] = _ask_list("Daily backup times (HH:MM, 24-hour)", s["daily_times"])
    s["on_server_start"] = _ask_bool("Back up after the FiveM server starts?", s["on_server_start"])
    s["on_server_stop"] = _ask_bool("Back up after the FiveM server stops or restarts?", s["on_server_stop"])

    _section("Retention")
    r["keep_last"] = _ask_int("Always keep the newest N backups", r["keep_last"])
    r["keep_daily"] = _ask_int("Also keep one backup per day for N days", r["keep_daily"])
    r["keep_weekly"] = _ask_int("Also keep one backup per week for N weeks", r["keep_weekly"])
    r["keep_monthly"] = _ask_int("Also keep one backup per month for N months", r["keep_monthly"])
    r["max_age_days"] = _ask_int("Delete backups older than N days (0 = never)", r["max_age_days"])
    r["min_keep"] = _ask_int("Never keep fewer than N backups", r["min_keep"], 1)

    _section("Off-site copy")
    rem = values["remote"]
    kind = _ask_choice("Copy every backup to a second location?", [
        ("none", "No"), ("directory", "Another folder (second disk, NAS share, OneDrive/Google Drive folder)"),
        ("rclone", "rclone remote (S3, B2, Drive, SFTP...; configure with 'rclone config' first)")],
        rem["type"] if rem["enabled"] else "none")
    rem["enabled"] = kind != "none"
    if rem["enabled"]:
        rem["type"] = kind
        rem["path"] = _ask("Folder path" if kind == "directory" else "rclone destination (remote:folder)",
                           rem["path"] or None)
        rem["keep_last"] = _ask_int("Backups to keep there (0 = never delete)", rem["keep_last"])

    _section("Alerts")
    n = values["notify"]
    n["enabled"] = _ask_bool("Send alerts to Discord?", n["enabled"])
    if n["enabled"]:
        from .notify import validate_url
        print("  In Discord: Server Settings > Integrations > Webhooks > New Webhook > pick a channel > "
              "Copy Webhook URL.")

        def url_check(v):
            if v in ("", "-"):
                return None
            try:
                validate_url(v)
            except AppError as exc:
                return str(exc)
            return None
        n["webhook_url"] = _ask("Webhook URL for all alerts", n["webhook_url"] or None, url_check)
        if _ask_bool("Use different channels for successes / failures / warnings / inactive-table reports?", False):
            for kind in ("success", "failure", "warning", "inactive"):
                v = _ask(f"  {kind} webhook URL ('-' = use the one above)", n[f"{kind}_webhook_url"] or "-", url_check)
                n[f"{kind}_webhook_url"] = "" if v == "-" else v
        n["on_success"] = _ask_bool("Send a message after every successful backup?", n["on_success"])
        ina = values["inactive_tables"]
        ina["enabled"] = _ask_bool("Send a weekly report of tables that have not changed for a long time?",
                                   ina["enabled"])
        if ina["enabled"]:
            ina["inactive_after_days"] = _ask_int("Report tables unchanged for how many days?",
                                                  ina["inactive_after_days"], 1)

    _section("Saving")
    config_mod.build(values, path, require_complete=True)
    atomic_write_text(path, config_mod.render_for(path, values, HEADER), private=values["security"]["restrict_permissions"])
    print(f"  Saved {path}")
    cfg = config_mod.load(str(path))

    if db["user"].lower() == "root":
        _offer_backup_account(cfg, values, path, store, password)
        cfg = config_mod.load(str(path))

    from . import service
    from .backup import perform_backup
    if _ask_bool("Run a test backup now?", True):
        result = perform_backup(cfg, "manual", note="Created by setup")
        if result.status == "success":
            print(f"  Test backup OK: {result.set_id} ({human_size(result.size)}, {result.duration:.1f}s)")
        else:
            print(f"  ! Test backup {result.status}: {result.error}" + (f"\n    {result.hint}" if result.hint else ""))
    if _ask_bool("Install the background service so backups run automatically?", True):
        try:
            print("  " + service.install(cfg))
        except AppError as exc:
            print(f"  ! {exc}" + (f"\n    {exc.hint}" if exc.hint else ""))
    print("\nSetup complete. Check everything with:  sqlbackup doctor")
    return 0


def _offer_backup_account(cfg, values: dict, path: Path, store, admin_password: str | None) -> None:
    from .grants import backup_account_sql
    print("\nBackups currently use an administrator account, which could change or delete any data if its "
          "password leaked. A dedicated account that can only read is safer.")
    if not _ask_bool("Create a read-only backup account now (using the account you just entered)?", True):
        print("  Skipped. You can print the SQL later with:  sqlbackup grant-sql")
        return
    name = _ask("New account name", "fivem_backup",
                lambda v: None if re.fullmatch(r"[A-Za-z0-9_]{1,32}", v) else "Use letters, digits and _ (max 32).")
    host = _ask("Allowed from host", "localhost" if cfg.database.host in ("127.0.0.1", "localhost", "::1") else "%")
    new_password = secrets.token_urlsafe(24)
    try:
        with open_engine(cfg, "backup", password=admin_password, use_password_arg=True) as eng:
            info = eng.server_info()
            eng.execute_script(backup_account_sql(info.flavor, info.version_tuple, name, host,
                                                  cfg.backup.databases, info.log_bin, new_password))
    except AppError as exc:
        print(f"  ! Could not create the account: {exc}")
        return
    store.set(credentials.DB_PASSWORD, new_password)
    values["database"]["user"] = name
    values["database"]["credential"] = "store"
    if not values["restore"]["user"]:
        values["restore"]["user"] = cfg.database.user
        values["restore"]["credential"] = "prompt" if admin_password else "none"
    atomic_write_text(path, config_mod.render_for(path, values, HEADER), private=values["security"]["restrict_permissions"])
    print(f"  Created '{name}'@'{host}' with a random password (stored encrypted). Restores will keep using "
          f"'{cfg.database.user}'" + (" and ask for its password." if admin_password else "."))
