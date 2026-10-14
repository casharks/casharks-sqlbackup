"""Streaming analysis of mysqldump/mariadb-dump output.

While a dump is written, DumpAnalyzer records which tables it contains, the exact number of rows
per table, routines/events/triggers, binary-log coordinates and whether the completion marker was
reached. Those figures go into the backup manifest and are later used to verify restores.
"""

import gzip
import hashlib
import re
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

_TOKENS = re.compile(rb"'(?:[^'\\]|\\.)*'|[()]", re.S)
_SECTION = re.compile(rb"^-- (Table structure for table|Dumping data for table|Temporary table structure for view|"
                      rb"Final view structure for view) `((?:[^`]|``)+)`")
_GLOBAL_SECTION = re.compile(rb"^-- Dumping (routines|events) for database")
_CREATE_TABLE = re.compile(rb"^CREATE TABLE `((?:[^`]|``)+)`")
_INSERT = re.compile(rb"^INSERT INTO `((?:[^`]|``)+)`")
_ROUTINE = re.compile(rb"DROP (PROCEDURE|FUNCTION) IF EXISTS `")
_EVENT = re.compile(rb"DROP EVENT IF EXISTS `")
_TRIGGER = re.compile(rb"\*/ ?/\*!50003 TRIGGER |^CREATE (?:DEFINER=\S+ )?TRIGGER ")
_BINLOG = re.compile(rb"(?:MASTER|SOURCE)_LOG_FILE='([^']+)', (?:MASTER|SOURCE)_LOG_POS=(\d+)")
_GTID = re.compile(rb"gtid_slave_pos='([^']*)'")
_HEADER = re.compile(rb"^(?:/\*M!999999\\- enable the sandbox mode \*/|-- (?:MariaDB|MySQL) dump)")
FOOTER = b"-- Dump completed"


def _name(raw: bytes) -> str:
    return raw.decode("utf-8", "replace").replace("``", "`")


def count_tuples(data: bytes, depth: int = 0) -> tuple[int, int]:
    """Count top-level parenthesised value tuples, skipping string literals."""
    count = 0
    for m in _TOKENS.finditer(data):
        tok = m.group()
        if tok == b"(":
            if depth == 0:
                count += 1
            depth += 1
        elif tok == b")":
            depth = max(0, depth - 1)
    return count, depth


@dataclass
class DumpAnalyzer:
    tables: dict[str, int] = field(default_factory=dict)
    views: list[str] = field(default_factory=list)
    routines: int = 0
    events: int = 0
    triggers: int = 0
    binlog_file: str | None = None
    binlog_pos: int | None = None
    gtid: str | None = None
    header_ok: bool = False
    completed: bool = False
    bytes_seen: int = 0
    _buf: bytes = b""
    _lines: int = 0
    _insert_table: str | None = None
    _depth: int = 0
    _section: str = ""
    _last: bytes = b""
    _hashes: dict = field(default_factory=dict)

    def _hash(self, table: str, line: bytes) -> None:
        h = self._hashes.get(table)
        if h is None:
            h = self._hashes[table] = hashlib.blake2b(digest_size=16)
        h.update(line)
        h.update(b"\n")

    def feed(self, chunk: bytes) -> None:
        self.bytes_seen += len(chunk)
        data = self._buf + chunk
        lines = data.split(b"\n")
        self._buf = lines.pop()
        for line in lines:
            self._line(line.rstrip(b"\r"))

    def finish(self) -> None:
        if self._buf:
            self._line(self._buf.rstrip(b"\r"))
            self._buf = b""
        self.completed = self._last.startswith(FOOTER)

    def _line(self, line: bytes) -> None:
        self._lines += 1
        if line.strip():
            self._last = line
        if self._lines <= 3 and _HEADER.match(line):
            self.header_ok = True

        if self._insert_table is not None:
            if line.startswith(b"(") or line.startswith(b",("):
                n, self._depth = count_tuples(line, self._depth)
                self.tables[self._insert_table] = self.tables.get(self._insert_table, 0) + n
                self._hash(self._insert_table, line)
            if line.rstrip().endswith(b";") and self._depth == 0:
                self._insert_table = None
            return

        if line.startswith(b"INSERT INTO `"):
            m = _INSERT.match(line)
            table = _name(m.group(1))
            idx = line.find(b" VALUES ")
            idx = line.find(b"VALUES") if idx < 0 else idx
            n, self._depth = count_tuples(line[idx + 6:] if idx >= 0 else b"", 0)
            self.tables[table] = self.tables.get(table, 0) + n
            self._hash(table, line)
            if not (line.rstrip().endswith(b";") and self._depth == 0):
                self._insert_table = table
            return

        if line.startswith(b"--"):
            m = _SECTION.match(line)
            if m:
                kind, name = m.group(1), _name(m.group(2))
                self._section = "view" if b"view" in kind else "table"
                if self._section == "view":
                    if name not in self.views:
                        self.views.append(name)
                    self.tables.pop(name, None)
                return
            m = _GLOBAL_SECTION.match(line)
            if m:
                self._section = m.group(1).decode()
                return
            m = _BINLOG.search(line)
            if m:
                self.binlog_file, self.binlog_pos = m.group(1).decode(), int(m.group(2))
                return
            m = _GTID.search(line)
            if m:
                self.gtid = m.group(1).decode()
            return

        if self._section == "table" and line.startswith(b"CREATE TABLE `"):
            name = _name(_CREATE_TABLE.match(line).group(1))
            if name not in self.views:
                self.tables.setdefault(name, 0)
        elif self._section == "routines" and _ROUTINE.search(line):
            self.routines += 1
        elif self._section == "events" and _EVENT.search(line):
            self.events += 1
        elif _TRIGGER.search(line):
            self.triggers += 1

    def data_hashes(self) -> dict[str, str]:
        """Fingerprint of each table's rows; unchanged fingerprint = unchanged data."""
        empty = hashlib.blake2b(digest_size=16).hexdigest()
        return {t: (self._hashes[t].hexdigest() if t in self._hashes else empty) for t in sorted(self.tables)}

    def summary(self) -> dict:
        return {
            "tables": dict(sorted(self.tables.items())),
            "data_hashes": self.data_hashes(),
            "views": sorted(self.views),
            "routines": self.routines,
            "events": self.events,
            "triggers": self.triggers,
            "binlog": {"file": self.binlog_file, "position": self.binlog_pos, "gtid": self.gtid}
            if self.binlog_file else None,
        }


def filter_tables(lines: Iterable[bytes], wanted: set[str]) -> Iterator[bytes]:
    """Yield only the dump lines that belong to the wanted tables/views (plus the session header)."""
    include = True
    seen_section = False
    for line in lines:
        if line.startswith(b"-- "):
            m = _SECTION.match(line)
            if m:
                seen_section = True
                include = _name(m.group(2)) in wanted
            elif _GLOBAL_SECTION.match(line):
                seen_section = True
                include = False
        if include or not seen_section:
            yield line


def iter_dump_lines(path: Path, chunk_size: int = 1 << 20) -> Iterator[bytes]:
    """Yield raw lines (with their newline) from a .sql or .sql.gz dump file."""
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rb") as f:
        while True:
            line = f.readline(64 << 20)
            if not line:
                break
            yield line


@dataclass
class FileCheck:
    path: str
    ok: bool
    problems: list[str]
    sha256: str | None = None
    size: int = 0


def verify_file(path: Path, expected_sha256: str | None = None, expected_size: int | None = None) -> FileCheck:
    """Check checksum, compression integrity and the dump completion marker in a single pass."""
    problems: list[str] = []
    if not path.is_file():
        return FileCheck(str(path), False, ["file is missing"])
    size = path.stat().st_size
    if expected_size is not None and size != expected_size:
        problems.append(f"size is {size} bytes, expected {expected_size}")
    digest = hashlib.sha256()
    compressed = path.name.endswith(".gz")
    decomp = zlib.decompressobj(16 + zlib.MAX_WBITS) if compressed else None
    tail = b""
    first = b""
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
                data = decomp.decompress(chunk) if decomp else chunk
                if not first:
                    first = data[:256]
                tail = (tail + data)[-8192:]
        if decomp:
            tail = (tail + decomp.flush())[-8192:]
            if not decomp.eof:
                problems.append("compressed data is truncated")
            elif decomp.unused_data:
                problems.append("unexpected data after the end of the compressed stream")
    except (zlib.error, OSError) as exc:
        problems.append(f"cannot be read: {exc}")
    sha = digest.hexdigest()
    if expected_sha256 and sha != expected_sha256:
        problems.append("checksum does not match the manifest (file changed or corrupted)")
    if not problems:
        if not _HEADER.match(first.lstrip()):
            problems.append("does not start with a mysqldump/mariadb-dump header")
        last = [ln for ln in tail.splitlines() if ln.strip()]
        if not last or not last[-1].startswith(FOOTER):
            problems.append("dump completion marker missing (backup was cut off)")
    return FileCheck(str(path), not problems, problems, sha, size)
