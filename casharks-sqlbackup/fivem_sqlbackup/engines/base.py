"""Engine interface. A new database family is added by implementing DatabaseEngine."""

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ConnectionSettings:
    host: str
    port: int
    socket: str
    user: str
    password: str | None
    tls: str = "default"
    connect_timeout: int = 10


@dataclass
class ServerInfo:
    flavor: str
    version: str
    version_tuple: tuple[int, ...]
    variables: dict[str, str] = field(default_factory=dict)

    @property
    def log_bin(self) -> bool:
        return self.variables.get("log_bin", "OFF").upper() in ("ON", "1")


@dataclass
class TableInfo:
    database: str
    name: str
    kind: str
    engine: str | None
    size_bytes: int


@dataclass
class DumpOptions:
    ignore_tables: list[str] = field(default_factory=list)
    schema_only_tables: list[str] = field(default_factory=list)
    routines: bool = True
    events: bool = True
    triggers: bool = True
    binlog_position: bool = False
    max_packet_mb: int = 512
    low_priority: bool = True
    no_data: bool = False


class DatabaseEngine(ABC):
    flavor: str

    @abstractmethod
    def __enter__(self): ...

    @abstractmethod
    def __exit__(self, *exc): ...

    @abstractmethod
    def query(self, sql: str, database: str | None = None, timeout: float = 120) -> list[list[str | None]]: ...

    @abstractmethod
    def execute_script(self, sql: str, database: str | None = None, timeout: float = 600) -> None:
        """Run SQL supplied on stdin (keeps statement text off the process command line)."""

    @abstractmethod
    def server_info(self) -> ServerInfo: ...

    @abstractmethod
    def list_databases(self) -> list[str]: ...

    @abstractmethod
    def tables(self, databases: list[str]) -> list[TableInfo]: ...

    @abstractmethod
    def start_dump(self, database: str, options: DumpOptions, info: ServerInfo) -> subprocess.Popen: ...

    @abstractmethod
    def start_load(self, database: str, max_packet_mb: int) -> subprocess.Popen: ...

    @abstractmethod
    def binlog_replay_command(self, files: list[str], start_position: int, stop_datetime: str,
                              source_db: str, target_db: str) -> list[str]: ...
