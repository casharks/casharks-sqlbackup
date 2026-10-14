"""Separate, per-table copies of selected tables.

After each full backup, the configured tables are extracted from the dump that was just written,
so every copy comes from exactly the same consistent snapshot as the full backup and puts no
extra load on the database. Copies are stored as

    <selected folder>/<database>/<table>/<name>.sql[.gz]   (+ .json naming the full backup it came from)

and are verified against the row counts recorded in the full backup's manifest. A failure here is
reported but never affects the full backup.
"""

import gzip
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import naming
from .dumpstream import FOOTER, DumpAnalyzer, iter_dump_lines, verify_file, _GLOBAL_SECTION, _SECTION, _name
from .errors import AppError
from .fsutil import atomic_write_text, safe_component
from .logsetup import audit, get_logger

log = get_logger("selected")


def _sidecar(dump: Path) -> Path:
    return dump.with_name(dump.name.removesuffix(".gz").removesuffix(".sql") + ".json")


def table_dir(cfg, database: str, table: str) -> Path:
    return cfg.selected_dir / safe_component(database, "db") / safe_component(table, "table")


@dataclass
class TableCopy:
    database: str
    table: str
    path: Path
    meta: dict

    @property
    def id(self) -> str:
        return self.path.name.removesuffix(".gz").removesuffix(".sql")

    @property
    def created(self) -> datetime:
        ts = self.meta.get("created_ts")
        return datetime.fromtimestamp(ts if ts else self.path.stat().st_mtime).astimezone()

    @property
    def size(self) -> int:
        return self.path.stat().st_size

    @property
    def rows(self) -> int | None:
        return self.meta.get("rows")

    def database_entry(self) -> dict:
        return {"name": self.database, "file": self.path.name, "size": self.meta.get("size"),
                "sha256": self.meta.get("sha256"), "tables": {self.table: self.meta.get("rows", 0)}, "views": [],
                "charset": self.meta.get("charset"), "collation": self.meta.get("collation"),
                "schema_only_tables": [], "routines": 0, "triggers": 0, "events": 0}


class TableCopySet:
    """Adapter that lets the restore functions treat one table copy like a backup set."""

    def __init__(self, copy: TableCopy):
        self.copy = copy
        self.id = f"{copy.database}.{copy.table}@{copy.id}"
        self.path = copy.path.parent
        self.created = copy.created

    def database(self, name: str) -> dict:
        if name != self.copy.database:
            raise AppError(f"This table copy belongs to database '{self.copy.database}'.")
        return self.copy.database_entry()


def list_copies(cfg, database: str | None = None, table: str | None = None) -> list[TableCopy]:
    copies = []
    for ref in cfg.selected_tables.tables:
        db, _, tb = ref.partition(".")
        if (database and db != database) or (table and tb != table):
            continue
        folder = table_dir(cfg, db, tb)
        if not folder.is_dir():
            continue
        for f in folder.iterdir():
            if not f.is_file() or f.name.startswith(".") or not f.name.endswith((".sql", ".sql.gz")):
                continue
            try:
                meta = json.loads(_sidecar(f).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if meta.get("table") == tb:
                copies.append(TableCopy(db, tb, f, meta))
    copies.sort(key=lambda c: (c.database, c.table, c.created), reverse=False)
    return copies


def resolve_copy(cfg, ref: str, version: str = "latest") -> TableCopy:
    db, sep, tb = ref.partition(".")
    if not sep:
        raise AppError("Give the table as database.table.")
    if ref not in cfg.selected_tables.tables:
        raise AppError(f"{ref} is not in selected_tables.tables.", hint="Add it there to keep separate copies.")
    copies = sorted(list_copies(cfg, db, tb), key=lambda c: c.created, reverse=True)
    if not copies:
        raise AppError(f"No separate copies of {ref} exist yet.")
    if version in ("latest", "newest"):
        return copies[0]
    if version.isdigit() and 1 <= int(version) <= len(copies) and len(version) < 6:
        return copies[int(version) - 1]
    matches = [c for c in copies if version in c.id]
    if len(matches) == 1:
        return matches[0]
    raise AppError(f"'{version}' does not identify exactly one copy of {ref}.",
                   hint=f"Run:  sqlbackup tables list {ref}")


def export_from_set(cfg, bset_path: Path, manifest: dict) -> tuple[list[dict], list[str]]:
    """Extract every selected table from a finished backup set. Returns (results, warnings)."""
    results: list[dict] = []
    warnings: list[str] = []
    wanted: dict[str, list[str]] = {}
    for ref in cfg.selected_tables.tables:
        db, _, tb = ref.partition(".")
        wanted.setdefault(db, []).append(tb)

    entries = {d["name"]: d for d in manifest["databases"]}
    for db, tables in wanted.items():
        entry = entries.get(db)
        if entry is None:
            results += [{"table": f"{db}.{tb}", "ok": False,
                         "error": f"database '{db}' is not part of the full backup (check backup.databases)"}
                        for tb in tables]
            continue
        present = [t for t in tables if t in entry["tables"] or t in entry.get("views", [])]
        results += [{"table": f"{db}.{tb}", "ok": False,
                     "error": "table does not exist in the database (check the name; names are case-sensitive)"}
                    for tb in tables if tb not in present]
        if present:
            try:
                results += _extract(cfg, bset_path / entry["file"], manifest, entry, present)
            except (OSError, AppError) as exc:
                results += [{"table": f"{db}.{tb}", "ok": False, "error": f"export failed: {exc}"} for tb in present]
    for r in results:
        if r["ok"]:
            log.info("Selected table %s saved (%s rows) -> %s", r["table"], r["rows"], r["file"])
        else:
            log.error("Selected table copy of %s failed: %s", r["table"], r["error"])
            warnings.append(f"Separate copy of {r['table']} failed: {r['error']}")
    audit("selected_tables", backup=manifest["id"], ok=[r["table"] for r in results if r["ok"]],
          failed=[r["table"] for r in results if not r["ok"]])
    return results, warnings


def _extract(cfg, dump: Path, manifest: dict, entry: dict, tables: list[str]) -> list[dict]:
    """One pass over the full dump writes all wanted tables of this database."""
    db = entry["name"]
    set_id = manifest["id"]
    created = datetime.fromtimestamp(manifest["created_ts"])
    compressed = cfg.backup.compression == "gzip"
    ext = ".sql.gz" if compressed else ".sql"
    outputs = {}
    try:
        for tb in tables:
            folder = table_dir(cfg, db, tb)
            folder.mkdir(parents=True, exist_ok=True)
            name = naming.render(cfg.selected_tables.name_format, created, cfg.backup.date_format,
                                 cfg.backup.time_format, table=tb, database=db, trigger=manifest["trigger"],
                                 instance=cfg.general.instance_name)
            final = folder / f"{name}{ext}"
            tmp = folder / f".partial-{name}{ext}"
            raw = open(tmp, "wb")
            digest = hashlib.sha256()
            writer = _Hashing(raw, digest)
            out = gzip.GzipFile(filename="", mode="wb", fileobj=writer, compresslevel=cfg.backup.compression_level) \
                if compressed else writer
            outputs[tb] = {"final": final, "tmp": tmp, "raw": raw, "out": out, "writer": writer, "digest": digest,
                           "analyzer": DumpAnalyzer()}

        def emit(targets, line):
            for o in targets:
                o["out"].write(line)
                o["analyzer"].feed(line)

        everyone = list(outputs.values())
        current: list = everyone
        seen_section = False
        for line in iter_dump_lines(dump):
            if line.startswith(b"-- "):
                m = _SECTION.match(line)
                if m:
                    seen_section = True
                    o = outputs.get(_name(m.group(2)))
                    current = [o] if o else []
                elif _GLOBAL_SECTION.match(line):
                    seen_section = True
                    current = []
                elif line.startswith(FOOTER):
                    current = []
            if current or not seen_section:
                emit(current if seen_section else everyone, line)
        stamp = f"{FOOTER.decode()} (table copy from backup {set_id})\n".encode()
        for o in everyone:
            emit([o], stamp)
            if o["out"] is not o["writer"]:
                o["out"].close()
            o["raw"].flush()
            os.fsync(o["raw"].fileno())
            o["raw"].close()
            o["analyzer"].finish()
    except BaseException:
        for o in outputs.values():
            o["raw"].close()
            o["tmp"].unlink(missing_ok=True)
        raise

    results = []
    for tb, o in outputs.items():
        expected = entry["tables"].get(tb)
        got = o["analyzer"].tables.get(tb, 0)
        size = o["tmp"].stat().st_size
        sha = o["digest"].hexdigest()
        check = verify_file(o["tmp"], sha, size)
        problems = list(check.problems)
        if expected is not None and got != expected:
            problems.append(f"copy has {got} rows but the full backup has {expected}")
        if problems:
            o["tmp"].unlink(missing_ok=True)
            results.append({"table": f"{db}.{tb}", "ok": False, "error": "; ".join(problems)})
            continue
        stem = naming.unique(o["final"].name[:-len(ext)], lambda c: (o["final"].with_name(c + ext)).exists()
                             or (o["final"].with_name(c + ".json")).exists(),
                             None if "{time}" in cfg.selected_tables.name_format else created, cfg.backup.time_format)
        final = o["final"].with_name(stem + ext)
        os.rename(o["tmp"], final)
        meta = {"database": db, "table": tb, "backup_id": set_id, "created_at": manifest["created_at"],
                "created_ts": manifest["created_ts"], "trigger": manifest["trigger"], "rows": got, "size": size,
                "sha256": sha, "charset": entry.get("charset"), "collation": entry.get("collation"),
                "server": manifest["server"]}
        atomic_write_text(_sidecar(final), json.dumps(meta, indent=2) + "\n")
        results.append({"table": f"{db}.{tb}", "ok": True, "rows": got, "file": str(final), "name": final.name,
                        "size": size, "validated": True})
    return results


class _Hashing:
    def __init__(self, raw, digest):
        self.raw, self.digest = raw, digest

    def write(self, data) -> int:
        self.digest.update(data)
        return self.raw.write(data)

    def flush(self) -> None:
        self.raw.flush()


def prune(cfg, dry_run: bool = False) -> list[Path]:
    """Per-table retention: keep_last newest, drop older than max_age_days, never below min_keep."""
    st = cfg.selected_tables
    removed: list[Path] = []
    now = datetime.now().astimezone()
    groups: dict[tuple[str, str], list[TableCopy]] = {}
    for c in list_copies(cfg):
        groups.setdefault((c.database, c.table), []).append(c)
    for copies in groups.values():
        copies.sort(key=lambda c: c.created, reverse=True)
        for i, c in enumerate(copies):
            if i < st.min_keep:
                continue
            too_many = st.keep_last and i >= st.keep_last
            too_old = st.max_age_days and c.created < now - timedelta(days=st.max_age_days)
            if not (too_many or too_old):
                continue
            removed.append(c.path)
            if not dry_run:
                c.path.unlink(missing_ok=True)
                _sidecar(c.path).unlink(missing_ok=True)
    if removed and not dry_run:
        log.info("Selected-table retention removed %d old copies", len(removed))
        audit("selected_retention", removed=[str(p) for p in removed])
    return removed
