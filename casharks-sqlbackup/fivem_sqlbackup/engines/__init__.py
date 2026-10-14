"""Engine factory. Credentials are resolved here and never stored on engine objects beyond use."""

from .. import credentials
from ..errors import ConfigError
from .base import ConnectionSettings, DatabaseEngine, DumpOptions, ServerInfo, TableInfo
from .mysql import MySQLEngine, quote_ident, quote_literal
from .tools import ToolSet, find_tools

__all__ = ["ConnectionSettings", "DatabaseEngine", "DumpOptions", "ServerInfo", "TableInfo", "MySQLEngine",
           "ToolSet", "find_tools", "open_engine", "quote_ident", "quote_literal", "secret_store", "load_tools"]

_tools_cache: dict[tuple[str, str], ToolSet] = {}


def secret_store(cfg) -> credentials.SecretStore:
    return credentials.SecretStore(cfg.secrets_path, cfg.security.dpapi_scope)


def load_tools(cfg) -> ToolSet:
    key = (cfg.database.client_bin_dir, cfg.database.engine)
    if key not in _tools_cache:
        prefer = None if cfg.database.engine == "auto" else cfg.database.engine
        _tools_cache[key] = find_tools(cfg.database.client_bin_dir, prefer)
    return _tools_cache[key]


def open_engine(cfg, purpose: str = "backup", password: str | None = None, user: str | None = None,
                use_password_arg: bool = False) -> DatabaseEngine:
    """Build an engine for the configured server. purpose is 'backup' or 'restore'."""
    db = cfg.database
    if purpose == "restore" and cfg.restore.user:
        account, method, secret = cfg.restore.user, cfg.restore.credential, credentials.RESTORE_PASSWORD
    else:
        account, method, secret = db.user, db.credential, credentials.DB_PASSWORD
    if user is not None:
        account = user
    if not use_password_arg:
        password = credentials.resolve(method, secret_store(cfg), secret, f"password for database user '{account}'")
    if not account:
        raise ConfigError("No database user configured.", hint="Run:  sqlbackup setup")
    conn = ConnectionSettings(db.host, db.port, db.socket, account, password, db.tls, db.connect_timeout)
    return MySQLEngine(conn, load_tools(cfg), cfg.tmp_dir)
