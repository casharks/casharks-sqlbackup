"""Configuration schema, loading, validation and rendering.

Every user-adjustable setting is declared once in SPEC. The example configuration and the file
written by the setup wizard are both rendered from it, so they cannot drift apart.
"""

import json
import os
import re
import textwrap
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from . import APP_NAME
from .errors import ConfigError

APP_ROOT = Path(__file__).resolve().parent.parent
CONFIG_ENV = "FIVEM_SQLBACKUP_CONFIG"
DEFAULT_CONFIG_NAME = "config.lua"
SYSTEM_DATABASES = frozenset({"information_schema", "performance_schema", "mysql", "sys"})

SPEC: dict[str, dict[str, tuple[str, object, str]]] = {
    "general": {
        "instance_name": ("str", "", "Name for your server used in backup names and alerts."),
    },
    "database": {
        "engine": ("str", "auto", "Database type you use (auto, mariadb or mysql). Leave on auto."),
        "host": ("str", "", "Your database host. Usually localhost."),
        "port": ("int", 3306, "Your database port. Usually 3306."),
        "socket": ("str", "", "Leave empty."),
        "user": ("str", "", "Your database username."),
        "credential": ("str", "store", "\"none\" = no password. \"store\" = run: sqlbackup credentials set "
                                       "database.password"),
        "tls": ("str", "default", "Leave on default."),
        "client_bin_dir": ("str", "", "Folder with mariadb-dump.exe. Leave empty and it finds it."),
        "connect_timeout": ("int", 10, "Seconds to wait when connecting to the database."),
    },
    "backup": {
        "databases": ("list", [], "Databases to back up. Example: { \"my_database\" }. Use { \"*\" } for all."),
        "exclude_databases": ("list", [], "Databases to skip when using { \"*\" }."),
        "exclude_tables": ("list", [], "Tables to never back up. Example: { \"my_database.logs\" }"),
        "schema_only_tables": ("list", [], "Tables to save empty (structure only), like big log tables."),
        "directory": ("str", "backups", "Folder where backups are saved."),
        "full_backup_folder": ("str", "full_server", "Folder inside the backup folder for full backups."),
        "name_format": ("str", "Date{date}-FullServer_Time{time}", "Name of the full backup file."),
        "date_format": ("str", "%m-%d-%y", "How the date looks. %m-%d-%y = 10-14-26"),
        "time_format": ("str", "%H-%M-%S", "How the time looks. %H-%M-%S = 15-42-07, %I-%M-%S%p = 03-42-07PM"),
        "compression": ("str", "gzip", "\"none\" = normal .sql file, \"gzip\" = smaller .sql.gz file."),
        "compression_level": ("int", 6, "How hard to compress gzip files (1 fast - 9 small)."),
        "low_priority": ("bool", True, "Back up slower so the game server does not lag."),
        "routines": ("bool", True, "Also save stored procedures and functions."),
        "events": ("bool", True, "Also save database events."),
        "triggers": ("bool", True, "Also save triggers."),
        "max_allowed_packet_mb": ("int", 512, "Biggest row size allowed. Leave it."),
    },
    "selected_tables": {
        "tables": ("list", [], "Tables to also save on their own. Example: { \"my_database.players\" }"),
        "folder": ("str", "selected_tables", "Folder inside the backup folder for these table copies."),
        "name_format": ("str", "Date{date}-{table}_Time{time}", "Name of each table copy file."),
        "keep_last": ("int", 60, "How many copies to keep per table."),
        "max_age_days": ("int", 90, "Delete table copies older than this many days. 0 = never."),
        "min_keep": ("int", 3, "Always keep at least this many copies per table."),
    },
    "schedule": {
        "interval_minutes": ("int", 60, "Back up every this many minutes. 0 = off."),
        "only_when_server_running": ("bool", True, "Only do those timed backups while the server is on."),
        "daily_times": ("list", ["04:00"], "Daily backup times (24 hour). Example: { \"04:00\", \"16:00\" }"),
        "on_server_start": ("bool", True, "Back up after the server starts."),
        "server_start_delay_seconds": ("int", 90, "Seconds to wait after the server starts."),
        "on_server_stop": ("bool", True, "Back up when the server stops, crashes or restarts."),
        "min_minutes_between_event_backups": ("int", 10, "Skip the start backup if one was just made."),
        "retry_failed_after_minutes": ("int", 15, "Minutes to wait before trying again after a failed backup."),
        "on_service_start": ("bool", False, "Back up every time the backup program starts."),
    },
    "server_watch": {
        "enabled": ("bool", True, "Watch for the FiveM server starting and stopping."),
        "process_names": ("list", ["FXServer.exe", "FXServer"], "Name of the FiveM server program."),
        "command_line_contains": ("list", ["+exec"], "Leave it. Makes it work with txAdmin."),
        "poll_seconds": ("int", 5, "How often to check if the server is running (seconds)."),
    },
    "retention": {
        "keep_last": ("int", 48, "Always keep this many newest backups."),
        "keep_daily": ("int", 14, "Keep one backup per day for this many days."),
        "keep_weekly": ("int", 8, "Keep one backup per week for this many weeks."),
        "keep_monthly": ("int", 6, "Keep one backup per month for this many months."),
        "max_age_days": ("int", 180, "Delete backups older than this many days. 0 = never."),
        "min_keep": ("int", 3, "Always keep at least this many backups."),
    },
    "retry": {
        "attempts": ("int", 3, "How many times to try a backup if it fails."),
        "delay_seconds": ("int", 20, "Seconds to wait before trying again."),
        "backoff_factor": ("float", 2.0, "Wait longer each time it tries again."),
    },
    "disk": {
        "min_free_mb": ("int", 1024, "Do not back up if the disk has less than this much free space (MB)."),
        "warn_free_mb": ("int", 5120, "Warn me when free space gets below this (MB)."),
    },
    "verify": {
        "after_backup": ("bool", True, "Check every backup after it is made."),
    },
    "remote": {
        "enabled": ("bool", False, "Also copy backups to another place (another drive, NAS or cloud folder)."),
        "type": ("str", "directory", "\"directory\" = a folder, \"rclone\" = a cloud remote."),
        "path": ("str", "", "Where to copy them. Example: \"E:\\\\Backups\""),
        "rclone_binary": ("str", "rclone", "Only for rclone. Leave it."),
        "keep_last": ("int", 30, "How many backups to keep there. 0 = keep all."),
    },
    "notify": {
        "enabled": ("bool", False, "Send Discord messages."),
        "format": ("str", "discord", "Leave on discord."),
        "webhook_url": ("str", "", "Your Discord webhook. All messages go here."),
        "success_webhook_url": ("str", "", "Webhook for good backups. Empty = use the one above."),
        "failure_webhook_url": ("str", "", "Webhook for errors. Empty = use the main one."),
        "warning_webhook_url": ("str", "", "Webhook for warnings. Empty = use the main one."),
        "inactive_webhook_url": ("str", "", "Webhook for the unused tables report. Empty = use the main one."),
        "on_success": ("bool", True, "Send a message when a backup works."),
        "on_failure": ("bool", True, "Send a message when something fails."),
        "on_warning": ("bool", True, "Send a message for warnings."),
        "repeat_error_after_minutes": ("int", 60, "Do not send the same error again for this many minutes."),
        "timeout_seconds": ("int", 10, "Seconds to wait for Discord before giving up."),
    },
    "inactive_tables": {
        "enabled": ("bool", False, "Send a report of tables that have not changed in a long time."),
        "inactive_after_days": ("int", 30, "Days with no change before a table is reported."),
        "report_every_days": ("int", 7, "How often to send the report (days)."),
        "only_new": ("bool", True, "Only show a table in full the first time it is reported."),
        "include_empty_tables": ("bool", True, "Also report empty tables."),
        "exclude_databases": ("list", [], "Databases to leave out of the report."),
        "exclude_tables": ("list", [], "Tables to leave out. Example: { \"my_database.logs_*\" }"),
    },
    "logging": {
        "directory": ("str", "logs", "Folder for log files."),
        "level": ("str", "INFO", "How much to log (INFO is fine)."),
        "max_file_mb": ("int", 5, "Start a new log file at this size (MB)."),
        "keep_files": ("int", 10, "How many old log files to keep."),
    },
    "service": {
        "mode": ("str", "logon", "\"logon\" = starts when you sign in to Windows."),
        "task_name": ("str", "", "Name in Task Scheduler. Empty = automatic."),
        "watchdog_minutes": ("int", 10, "Restart the backup program within this many minutes if it stops."),
    },
    "restore": {
        "user": ("str", "", "Database user for restoring. Empty = same as above."),
        "credential": ("str", "prompt", "\"prompt\" = ask for the password each time. \"none\" = no password."),
        "refuse_while_server_running": ("bool", True, "Do not restore while the server is running (keeps data safe)."),
    },
    "pitr": {
        "binlog_dir": ("str", "", "Advanced. Leave empty."),
    },
    "security": {
        "restrict_permissions": ("bool", True, "Only your Windows account can open the config, logs and backups."),
        "dpapi_scope": ("str", "user", "Leave on user."),
    },
}

CHOICES = {
    ("database", "engine"): {"auto", "mariadb", "mysql"},
    ("database", "tls"): {"default", "required", "disabled"},
    ("backup", "compression"): {"gzip", "none"},
    ("remote", "type"): {"directory", "rclone"},
    ("notify", "format"): {"discord", "json"},
    ("logging", "level"): {"DEBUG", "INFO", "WARNING", "ERROR"},
    ("service", "mode"): {"logon", "boot"},
    ("security", "dpapi_scope"): {"user", "machine"},
}

_INSTANCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_ENV_RE = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")
_TASK_RE = re.compile(r'^[^\\/:*?"<>|]{1,100}$')


def credential_ok(value: str) -> bool:
    return value in ("store", "prompt", "none") or bool(_ENV_RE.match(value))


def validate_db_name(name: str) -> str | None:
    """Return an error message if the database name is unusable."""
    if not name or len(name) > 64:
        return "must be 1-64 characters"
    if any(c in name for c in "/\\.`\x00") or name.startswith("-") or name != name.strip():
        return "contains characters that are not allowed (/ \\ . ` or a leading '-' or space)"
    return None


def validate_table_ref(ref: str) -> str | None:
    db, sep, table = ref.partition(".")
    if not sep or not table or validate_db_name(db) or "`" in table or "\x00" in table or len(table) > 64:
        return "must be written as database.table"
    return None


@dataclass
class Config:
    path: Path
    sections: dict[str, SimpleNamespace]

    def __getattr__(self, item):
        try:
            return self.__dict__["sections"][item]
        except KeyError:
            raise AttributeError(item) from None

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    def resolve(self, value: str) -> Path:
        p = Path(os.path.expandvars(os.path.expanduser(value)))
        return Path(os.path.abspath(p if p.is_absolute() else self.base_dir / p))

    @property
    def backup_root(self) -> Path:
        return self.resolve(self.backup.directory)

    @property
    def backup_dir(self) -> Path:
        """Folder holding the full backup sets."""
        folder = self.backup.full_backup_folder
        return self.backup_root / folder if folder else self.backup_root

    @property
    def selected_dir(self) -> Path:
        p = Path(os.path.expandvars(os.path.expanduser(self.selected_tables.folder)))
        return Path(os.path.abspath(p if p.is_absolute() else self.backup_root / p))

    @property
    def log_dir(self) -> Path:
        return self.resolve(self.logging.directory)

    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def secrets_path(self) -> Path:
        return self.base_dir / "secrets.dat"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def task_name(self) -> str:
        return self.service.task_name or f"{APP_NAME} - {self.general.instance_name}"


def config_path(arg: str | None = None) -> Path:
    raw = arg or os.environ.get(CONFIG_ENV)
    if raw:
        return Path(os.path.abspath(Path(raw).expanduser()))
    lua, toml = APP_ROOT / DEFAULT_CONFIG_NAME, APP_ROOT / "config.toml"
    return toml if toml.exists() and not lua.exists() else lua


def defaults() -> dict[str, dict[str, object]]:
    return {s: {k: (list(v[1]) if isinstance(v[1], list) else v[1]) for k, v in keys.items()}
            for s, keys in SPEC.items()}


def _coerce(section: str, key: str, value, errors: list[str]):
    kind = SPEC[section][key][0]
    where = f"{section}.{key}"
    if kind == "bool":
        if isinstance(value, bool):
            return value
    elif kind == "int":
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    elif kind == "float":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    elif kind == "str":
        if isinstance(value, str):
            return value.strip()
    elif kind == "list":
        if value == {}:
            return []
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return [v.strip() for v in value]
    errors.append(f"{where} must be a {kind}")
    return SPEC[section][key][1]


def build(data: dict, path: Path, require_complete: bool = True) -> Config:
    errors: list[str] = []
    merged = defaults()
    for section, values in data.items():
        if section not in SPEC or not isinstance(values, dict):
            errors.append(f"unknown section [{section}] (Config.{section})")
            continue
        for key, value in values.items():
            if key not in SPEC[section]:
                errors.append(f"unknown setting {section}.{key}")
                continue
            merged[section][key] = _coerce(section, key, value, errors)

    validate(merged, errors, require_complete)
    from .logsetup import REDACTOR
    for key, value in merged["notify"].items():
        if key.endswith("webhook_url") and value and value != "store":
            REDACTOR.add(value)
    if errors:
        raise ConfigError(f"Configuration problems in {path}:\n  - " + "\n  - ".join(errors),
                          hint="Fix the settings above, or run the setup wizard again.")
    return Config(path=path, sections={s: SimpleNamespace(**v) for s, v in merged.items()})


def validate(c: dict, errors: list[str], require_complete: bool = True) -> None:
    for (section, key), allowed in CHOICES.items():
        if c[section][key] not in allowed:
            errors.append(f"{section}.{key} must be one of: {', '.join(sorted(allowed))}")

    g, db, b = c["general"], c["database"], c["backup"]
    if require_complete:
        if not g["instance_name"]:
            errors.append("general.instance_name is empty (run setup)")
        if not db["host"] and not db["socket"]:
            errors.append("database.host is empty (run setup)")
        if not db["user"]:
            errors.append("database.user is empty (run setup)")
        if not b["databases"]:
            errors.append("backup.databases is empty (run setup)")
    if g["instance_name"] and not _INSTANCE_RE.match(g["instance_name"]):
        errors.append("general.instance_name may only contain letters, digits, '-' and '_' (max 40)")
    if not 1 <= db["port"] <= 65535:
        errors.append("database.port must be between 1 and 65535")
    if not credential_ok(db["credential"]):
        errors.append("database.credential must be store, prompt, none or env:VARIABLE_NAME")
    if c["restore"]["credential"] and not credential_ok(c["restore"]["credential"]):
        errors.append("restore.credential must be store, prompt, none or env:VARIABLE_NAME")
    if db["connect_timeout"] < 1:
        errors.append("database.connect_timeout must be at least 1")

    if b["databases"] != ["*"]:
        for name in b["databases"]:
            if name == "*":
                errors.append('backup.databases: "*" must be the only entry')
            elif (problem := validate_db_name(name)):
                errors.append(f"backup.databases: '{name}' {problem}")
    for name in b["exclude_databases"]:
        if (problem := validate_db_name(name)):
            errors.append(f"backup.exclude_databases: '{name}' {problem}")
    for key in ("exclude_tables", "schema_only_tables"):
        for ref in b[key]:
            if (problem := validate_table_ref(ref)):
                errors.append(f"backup.{key}: '{ref}' {problem}")
    folder = b["full_backup_folder"]
    if folder and (folder.startswith(".") or not re.fullmatch(r"[A-Za-z0-9_. -]{1,60}", folder)):
        errors.append("backup.full_backup_folder must be a simple folder name (letters, digits, - _ . space)")

    from . import naming
    if (problem := naming.problem(b["name_format"], b["date_format"], naming.FIELDS_FULL, b["time_format"])):
        errors.append(f"backup.name_format: {problem}")
    if (problem := naming.problem(c["selected_tables"]["name_format"], b["date_format"], naming.FIELDS_TABLE,
                                  b["time_format"])):
        errors.append(f"selected_tables.name_format: {problem}")

    sel = c["selected_tables"]
    seen: set[str] = set()
    for ref in sel["tables"]:
        if (problem := validate_table_ref(ref)):
            errors.append(f"selected_tables.tables: '{ref}' {problem}")
            continue
        if ref in seen:
            errors.append(f"selected_tables.tables: '{ref}' is listed twice")
        seen.add(ref)
        if ref in b["exclude_tables"] or ref in b["schema_only_tables"]:
            errors.append(f"selected_tables.tables: '{ref}' is also in backup.exclude_tables/schema_only_tables; "
                          "selected tables must be fully included in the full backup")
        db = ref.partition(".")[0]
        if b["databases"] and b["databases"] != ["*"] and db not in b["databases"]:
            errors.append(f"selected_tables.tables: database '{db}' of '{ref}' is not in backup.databases")
        elif b["databases"] == ["*"] and db in b["exclude_databases"]:
            errors.append(f"selected_tables.tables: database '{db}' of '{ref}' is in backup.exclude_databases")
    if not sel["folder"]:
        errors.append("selected_tables.folder cannot be empty")
    for key in ("keep_last", "max_age_days"):
        if sel[key] < 0:
            errors.append(f"selected_tables.{key} cannot be negative")
    if sel["min_keep"] < 1:
        errors.append("selected_tables.min_keep must be at least 1")

    if not 1 <= b["compression_level"] <= 9:
        errors.append("backup.compression_level must be between 1 and 9")
    if not 1 <= b["max_allowed_packet_mb"] <= 1024:
        errors.append("backup.max_allowed_packet_mb must be between 1 and 1024")

    s = c["schedule"]
    for t in s["daily_times"]:
        if not _TIME_RE.match(t):
            errors.append(f"schedule.daily_times: '{t}' must be HH:MM (24-hour)")
    for key in ("interval_minutes", "server_start_delay_seconds", "min_minutes_between_event_backups",
                "retry_failed_after_minutes"):
        if s[key] < 0:
            errors.append(f"schedule.{key} cannot be negative")
    if c["server_watch"]["poll_seconds"] < 2:
        errors.append("server_watch.poll_seconds must be at least 2")
    if c["server_watch"]["enabled"] and not c["server_watch"]["process_names"]:
        errors.append("server_watch.process_names cannot be empty when server_watch is enabled")

    for key, value in c["retention"].items():
        if value < 0:
            errors.append(f"retention.{key} cannot be negative")
    if c["retention"]["min_keep"] < 1:
        errors.append("retention.min_keep must be at least 1")
    if c["retry"]["attempts"] < 1:
        errors.append("retry.attempts must be at least 1")
    if c["retry"]["delay_seconds"] < 0 or c["retry"]["backoff_factor"] < 1:
        errors.append("retry.delay_seconds must be >= 0 and retry.backoff_factor >= 1")
    if c["disk"]["min_free_mb"] < 0 or c["disk"]["warn_free_mb"] < 0:
        errors.append("disk values cannot be negative")

    r = c["remote"]
    if r["enabled"] and not r["path"]:
        errors.append("remote.path is required when remote.enabled = true")
    if r["enabled"] and r["type"] == "rclone" and ":" not in r["path"]:
        errors.append("remote.path for rclone must look like remote:folder")
    if r["keep_last"] < 0:
        errors.append("remote.keep_last cannot be negative")
    n = c["notify"]
    if n["timeout_seconds"] < 1:
        errors.append("notify.timeout_seconds must be at least 1")
    if n["repeat_error_after_minutes"] < 0:
        errors.append("notify.repeat_error_after_minutes cannot be negative")
    for key in ("webhook_url", "success_webhook_url", "failure_webhook_url", "warning_webhook_url",
                "inactive_webhook_url"):
        url = n[key]
        if url and url != "store" and not (url.startswith("https://") or url.startswith("http://localhost")
                                           or url.startswith("http://127.0.0.1")):
            errors.append(f"notify.{key} must be empty, \"store\" or a https:// webhook URL")
    ina = c["inactive_tables"]
    if ina["inactive_after_days"] < 1 or ina["report_every_days"] < 1:
        errors.append("inactive_tables.inactive_after_days and report_every_days must be at least 1")
    for ref in ina["exclude_tables"]:
        if "." not in ref:
            errors.append(f"inactive_tables.exclude_tables: '{ref}' must be written as database.table")
    if c["service"]["task_name"] and not _TASK_RE.match(c["service"]["task_name"]):
        errors.append('service.task_name cannot contain \\ / : * ? " < > |')
    if c["service"]["watchdog_minutes"] < 1:
        errors.append("service.watchdog_minutes must be at least 1")
    if c["service"]["mode"] == "boot" and db["credential"] == "store" and c["security"]["dpapi_scope"] != "machine" \
            and os.name == "nt":
        errors.append('service.mode = "boot" with stored credentials requires security.dpapi_scope = "machine"')


def load(arg: str | None = None, require_complete: bool = True) -> Config:
    path = config_path(arg)
    if not path.exists():
        raise ConfigError(f"No configuration file found at {path}",
                          hint="Run the setup wizard first:  sqlbackup setup")
    try:
        if path.suffix.lower() == ".lua":
            from . import luaconfig
            data = luaconfig.loads(path.read_text(encoding="utf-8-sig"))
        else:
            with open(path, "rb") as f:
                data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}", hint="Check the line mentioned above (missing comma or quote?).") \
            from None
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from None
    return build(data, path, require_complete)


def render_for(path: Path, values: dict, header: str) -> str:
    """Render in the format matching the file extension (.lua or .toml)."""
    if Path(path).suffix.lower() == ".lua":
        from . import luaconfig
        return luaconfig.render(SPEC, values, header)
    return render(values, header)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(v, ensure_ascii=False) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def render(values: dict[str, dict[str, object]], header: str) -> str:
    lines = [f"# {line}" if line else "#" for line in header.splitlines()]
    for section, keys in SPEC.items():
        lines += ["", f"[{section}]"]
        for key, (_kind, default, doc) in keys.items():
            lines += ["# " + part for part in textwrap.wrap(doc, 100)]
            lines.append(f"{key} = {_toml_value(values.get(section, {}).get(key, default))}")
    return "\n".join(lines) + "\n"


def to_dict(cfg: Config) -> dict[str, dict[str, object]]:
    return {s: dict(vars(ns)) for s, ns in cfg.sections.items()}
