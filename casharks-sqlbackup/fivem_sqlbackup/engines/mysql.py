"""MariaDB and MySQL support through their native command-line tools.

Credentials are passed in a short-lived option file readable only by the current user, never on
the command line, so they do not appear in process listings.
"""

import contextlib
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

from ..errors import DbError
from ..fsutil import popen_kwargs, restrict
from ..logsetup import REDACTOR
from .base import ConnectionSettings, DatabaseEngine, DumpOptions, ServerInfo, TableInfo
from .tools import ToolSet, parse_version

TRANSIENT_CODES = {1040, 1053, 1205, 1213, 2002, 2003, 2005, 2006, 2013, 2055}
_HINTS = {
    1044: "The account lacks privileges on this database. See:  sqlbackup grant-sql",
    1045: "Wrong user name or password. Store the password again with:  sqlbackup credentials set database.password",
    1049: "That database does not exist. Check backup.databases in the configuration.",
    1142: "The account lacks a required privilege. See:  sqlbackup grant-sql",
    1227: "The account lacks a required privilege. See:  sqlbackup grant-sql",
    1698: "This account must log in through the operating system (unix_socket). Use a password account instead.",
    2002: "The database server is not reachable. Is it running? Check database.host/port/socket.",
    2003: "The database server is not reachable. Is it running? Check database.host and database.port.",
    2005: "Unknown host name. Check database.host.",
    2026: "TLS/SSL connection problem. Try database.tls = \"disabled\" for local connections.",
}
_CODE_PATTERNS = [re.compile(p) for p in (r"ERROR (\d{4})", r"Got error: (\d{4})", r"[Ee]rror:? (\d{4})",
                                         r"\((\d{4})\)")]


def quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def quote_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _option_value(value: str) -> str:
    if any(c in value for c in "\r\n\x00"):
        raise DbError("Connection settings cannot contain line breaks.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def clean_stderr(text: str) -> str:
    lines = [ln.strip() for ln in REDACTOR.scrub(text).splitlines()]
    return "\n".join(ln for ln in lines if ln and "Using a password on the command line" not in ln)


def classify_error(stderr: str, fallback: str = "Database command failed") -> DbError:
    text = clean_stderr(stderr) or fallback
    code = None
    for pattern in _CODE_PATTERNS:
        m = pattern.search(text)
        if m:
            code = int(m.group(1))
            break
    return DbError(text, code=code, transient=code in TRANSIENT_CODES, hint=_HINTS.get(code))


class _StderrReader(threading.Thread):
    """Drains a pipe in the background so the child never blocks on a full stderr buffer."""

    def __init__(self, stream):
        super().__init__(daemon=True)
        self.stream = stream
        self.chunks: list[bytes] = []

    def run(self):
        for chunk in iter(lambda: self.stream.read(8192), b""):
            if sum(map(len, self.chunks)) < 1_000_000:
                self.chunks.append(chunk)

    @property
    def text(self) -> str:
        self.join(timeout=30)
        return b"".join(self.chunks).decode("utf-8", "replace")


class MySQLEngine(DatabaseEngine):
    def __init__(self, conn: ConnectionSettings, tools: ToolSet, tmp_dir: Path):
        self.conn = conn
        self.tools = tools
        self.flavor = tools.flavor
        self.tmp_dir = Path(tmp_dir)
        self._optfile: Path | None = None
        REDACTOR.add(conn.password)

    def __enter__(self):
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        restrict(self.tmp_dir)
        lines = ["[client]", f"user={_option_value(self.conn.user)}"]
        if self.conn.password is not None:
            lines.append(f"password={_option_value(self.conn.password)}")
        if self.conn.socket:
            lines.append(f"socket={_option_value(self.conn.socket)}")
        if self.conn.host:
            lines.append(f"host={_option_value(self.conn.host)}")
        lines.append(f"port={int(self.conn.port)}")
        if self.conn.tls == "required":
            lines.append("ssl" if self.flavor == "mariadb" else "ssl-mode=REQUIRED")
        elif self.conn.tls == "disabled":
            lines.append("skip-ssl" if self.flavor == "mariadb" else "ssl-mode=DISABLED")
        fd, name = tempfile.mkstemp(prefix="conn-", suffix=".cnf", dir=self.tmp_dir)
        self._optfile = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        restrict(self._optfile)
        return self

    def __exit__(self, *exc):
        if self._optfile:
            with contextlib.suppress(OSError):
                self._optfile.unlink()
            self._optfile = None

    def _base(self, exe: Path) -> list[str]:
        if self._optfile is None:
            raise RuntimeError("Engine used outside of its 'with' block")
        return [str(exe), f"--defaults-extra-file={self._optfile}"]

    def _client(self, database: str | None, extra: list[str]) -> list[str]:
        cmd = self._base(self.tools.client) + [f"--connect-timeout={self.conn.connect_timeout}",
                                                "--default-character-set=utf8mb4", *extra]
        if database:
            cmd.append(f"--database={database}")
        return cmd

    def query(self, sql: str, database: str | None = None, timeout: float = 120) -> list[list[str | None]]:
        cmd = self._client(database, ["--batch", "--skip-column-names", "--raw", "-e", sql])
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout, **popen_kwargs())
        except subprocess.TimeoutExpired:
            raise DbError(f"Query timed out after {timeout:.0f}s", transient=True) from None
        except OSError as exc:
            raise DbError(f"Cannot run {self.tools.client}: {exc}") from None
        if r.returncode != 0:
            raise classify_error(r.stderr.decode("utf-8", "replace"))
        rows = []
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            rows.append([None if v == "NULL" else v for v in line.split("\t")])
        return rows

    def execute_script(self, sql: str, database: str | None = None, timeout: float = 600) -> None:
        cmd = self._client(database, ["--batch"])
        try:
            r = subprocess.run(cmd, input=sql.encode("utf-8"), capture_output=True, timeout=timeout,
                               **popen_kwargs())
        except subprocess.TimeoutExpired:
            raise DbError(f"SQL script timed out after {timeout:.0f}s") from None
        if r.returncode != 0:
            raise classify_error(r.stderr.decode("utf-8", "replace"))

    def server_info(self) -> ServerInfo:
        wanted = ("version", "version_comment", "log_bin", "log_bin_basename", "binlog_format",
                  "binlog_expire_logs_seconds", "expire_logs_days", "datadir", "bind_address", "skip_networking",
                  "innodb_flush_log_at_trx_commit", "sync_binlog", "gtid_mode", "max_allowed_packet",
                  "lower_case_table_names")
        rows = self.query("SHOW GLOBAL VARIABLES WHERE Variable_name IN (%s)"
                          % ", ".join(quote_literal(w) for w in wanted))
        variables = {r[0].lower(): (r[1] if len(r) > 1 and r[1] is not None else "") for r in rows}
        version = variables.get("version", "")
        flavor = "mariadb" if "mariadb" in (version + variables.get("version_comment", "")).lower() else "mysql"
        return ServerInfo(flavor, version, parse_version(version), variables)

    def list_databases(self) -> list[str]:
        return [r[0] for r in self.query("SHOW DATABASES") if r and r[0]]

    def tables(self, databases: list[str]) -> list[TableInfo]:
        if not databases:
            return []
        rows = self.query(
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, ENGINE, "
            "COALESCE(DATA_LENGTH,0)+COALESCE(INDEX_LENGTH,0) FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA IN (%s) ORDER BY TABLE_SCHEMA, TABLE_NAME"
            % ", ".join(quote_literal(d) for d in databases))
        return [TableInfo(r[0], r[1], r[2] or "", r[3], int(r[4] or 0)) for r in rows]

    def database_charset(self, database: str) -> tuple[str, str]:
        rows = self.query("SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA "
                          "WHERE SCHEMA_NAME = " + quote_literal(database))
        return (rows[0][0], rows[0][1]) if rows else ("utf8mb4", "")

    def start_dump(self, database: str, options: DumpOptions, info: ServerInfo) -> subprocess.Popen:
        cmd = self._base(self.tools.dump) + [
            "--single-transaction", "--quick", "--hex-blob", "--default-character-set=utf8mb4",
            "--no-tablespaces", f"--max-allowed-packet={options.max_packet_mb}M",
        ]
        if options.routines:
            cmd.append("--routines")
        if options.events:
            cmd.append("--events")
        if not options.triggers:
            cmd.append("--skip-triggers")
        if options.no_data:
            cmd.append("--no-data")
        if self.flavor == "mysql":
            cmd.append("--set-gtid-purged=OFF")
            if info.flavor == "mariadb" and self.tools.version_tuple >= (8, 0, 0):
                cmd.append("--column-statistics=0")
        if options.binlog_position:
            if self.flavor == "mysql" and self.tools.version_tuple >= (8, 0, 26):
                cmd.append("--source-data=2")
            else:
                cmd.append("--master-data=2")
        cmd += [f"--ignore-table={t}" for t in options.ignore_tables]
        cmd += [f"--ignore-table-data={t}" for t in options.schema_only_tables]
        cmd.append(database)
        try:
            return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    **popen_kwargs(options.low_priority))
        except OSError as exc:
            raise DbError(f"Cannot run {self.tools.dump}: {exc}") from None

    def start_load(self, database: str, max_packet_mb: int) -> subprocess.Popen:
        cmd = self._client(database, ["--binary-mode", "--batch", f"--max-allowed-packet={max_packet_mb}M"])
        try:
            return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    **popen_kwargs())
        except OSError as exc:
            raise DbError(f"Cannot run {self.tools.client}: {exc}") from None

    def binlog_replay_command(self, files: list[str], start_position: int, stop_datetime: str,
                              source_db: str, target_db: str) -> list[str]:
        if not self.tools.binlog:
            raise DbError("mariadb-binlog/mysqlbinlog was not found next to the other client tools.")
        cmd = [str(self.tools.binlog), f"--start-position={int(start_position)}", f"--stop-datetime={stop_datetime}"]
        if target_db != source_db:
            cmd += [f"--rewrite-db={source_db}->{target_db}",
                    f"--database={source_db if self.flavor == 'mariadb' else target_db}"]
        else:
            cmd.append(f"--database={source_db}")
        return cmd + files


def stderr_reader(stream) -> _StderrReader:
    reader = _StderrReader(stream)
    reader.start()
    return reader
