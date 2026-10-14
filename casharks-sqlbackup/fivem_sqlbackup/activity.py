"""Inactive-table report (report only; nothing is ever changed or deleted).

Evidence, strongest first:

1. Backup fingerprints (confirmed). Every full backup stores a hash of each table's rows. If the
   hash is identical in every backup across N days, the data was unchanged for at least N days.
   This works for every storage engine and survives database restarts. Resolution is limited to
   how often backups run: a change that was undone between two backups is invisible.
2. Server statistics (estimate). information_schema.TABLES.UPDATE_TIME. For InnoDB it is kept in
   memory only and is lost when the database restarts or the table leaves the cache, so it can only
   say "no change since the database started". For MyISAM/Aria it comes from the data file.
3. Otherwise the activity is reported as unknown - never as inactive.
"""

import fnmatch
import json
import time
from dataclasses import dataclass, field
from datetime import datetime

from .fsutil import atomic_write_text
from .logsetup import audit, get_logger

log = get_logger("activity")
DAY = 86400


def history_path(cfg):
    return cfg.data_dir / "table_activity.json"


def _load(cfg) -> dict:
    try:
        return json.loads(history_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"tables": {}}


def record(cfg, manifest: dict) -> None:
    """Update the per-table history from a finished full backup."""
    data = _load(cfg)
    tables = data.setdefault("tables", {})
    ts = manifest["created_ts"]
    for db in manifest["databases"]:
        hashes = db.get("data_hashes")
        if not hashes:
            continue
        schema_only = {t.split(".", 1)[1] for t in db.get("schema_only_tables", [])}
        present = set()
        for table, digest in hashes.items():
            key = f"{db['name']}.{table}"
            present.add(key)
            rows = db["tables"].get(table, 0)
            entry = tables.get(key)
            if entry is None or ts < entry["last_checked"]:
                if entry is None:
                    tables[key] = {"hash": digest, "rows": rows, "first_seen": ts, "unchanged_since": ts,
                                   "last_checked": ts, "changed_between": None, "schema_only": table in schema_only}
                continue
            if entry["hash"] != digest:
                entry.update(hash=digest, unchanged_since=ts, changed_between=[entry["last_checked"], ts])
            entry.update(rows=rows, last_checked=ts, schema_only=table in schema_only)
        for key in [k for k in tables if k.split(".", 1)[0] == db["name"] and k not in present]:
            del tables[key]
    atomic_write_text(history_path(cfg), json.dumps(data, indent=1) + "\n")


@dataclass
class Finding:
    table: str
    status: str
    rows: int | None
    unchanged_days: float | None
    last_activity: str
    evidence: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    active: int = 0
    excluded: int = 0
    notes: list[str] = field(default_factory=list)

    def by(self, status: str) -> list[Finding]:
        return [f for f in self.findings if f.status == status]


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _excluded(cfg, key: str) -> bool:
    ina = cfg.inactive_tables
    db = key.split(".", 1)[0]
    return db in ina.exclude_databases or any(fnmatch.fnmatchcase(key, p) for p in ina.exclude_tables)


def _server_stats(cfg, keys: list[str]) -> tuple[dict[str, tuple[str | None, str | None]], float | None, str | None]:
    """{db.table: (engine, update_time)}, server start timestamp, or an explanation if unavailable."""
    if not keys:
        return {}, None, None
    from .engines import open_engine, quote_literal
    from .errors import AppError
    dbs = sorted({k.split(".", 1)[0] for k in keys})
    try:
        with open_engine(cfg, "backup") as eng:
            rows = eng.query("SELECT TABLE_SCHEMA, TABLE_NAME, ENGINE, UPDATE_TIME FROM information_schema.TABLES "
                             "WHERE TABLE_SCHEMA IN (%s)" % ", ".join(quote_literal(d) for d in dbs))
            uptime = eng.query("SHOW GLOBAL STATUS LIKE 'Uptime'")
    except AppError as exc:
        return {}, None, f"Server statistics unavailable ({exc})"
    started = time.time() - int(uptime[0][1]) if uptime else None
    return {f"{r[0]}.{r[1]}": (r[2], r[3]) for r in rows}, started, None


def build(cfg, now: float | None = None) -> Report:
    now = now or time.time()
    ina = cfg.inactive_tables
    threshold = ina.inactive_after_days * DAY
    report = Report()
    tables = _load(cfg).get("tables", {})
    if not tables:
        report.notes.append("No backup history yet. The report becomes reliable once backups have run for at least "
                            f"{ina.inactive_after_days} days.")
    undecided: list[str] = []
    for key, e in sorted(tables.items()):
        if _excluded(cfg, key):
            report.excluded += 1
            continue
        if e.get("schema_only"):
            report.findings.append(Finding(key, "unknown", None, None, "unknown",
                                           "Structure-only backups (backup.schema_only_tables) hold no data to "
                                           "compare."))
            continue
        if e["rows"] == 0 and not ina.include_empty_tables:
            report.excluded += 1
            continue
        unchanged = e["last_checked"] - e["unchanged_since"]
        if e["changed_between"]:
            last = f"between {_fmt(e['changed_between'][0])} and {_fmt(e['changed_between'][1])}"
        else:
            last = f"before {_fmt(e['first_seen'])} (earlier than the oldest backup on record)"
        if unchanged >= threshold:
            report.findings.append(Finding(
                key, "confirmed", e["rows"], unchanged / DAY, last,
                f"Identical data in every backup from {_fmt(e['unchanged_since'])} to {_fmt(e['last_checked'])}."))
        elif e["changed_between"] is None:
            undecided.append(key)
        else:
            report.active += 1

    if undecided:
        stats, started, problem = _server_stats(cfg, undecided)
        if problem:
            report.notes.append(problem)
        for key in undecided:
            e = tables[key]
            engine, update_time = stats.get(key, (None, None))
            history_days = (e["last_checked"] - e["first_seen"]) / DAY
            if update_time:
                changed = datetime.fromisoformat(update_time).timestamp()
                if now - changed >= threshold:
                    report.findings.append(Finding(
                        key, "estimate", e["rows"], (now - changed) / DAY, f"about {_fmt(changed)} (server statistic)",
                        f"Server reports the last write at {_fmt(changed)} ({engine}). Backup history covers only "
                        f"{history_days:.1f} days."))
                else:
                    report.active += 1
            elif engine and engine.lower() == "innodb" and started and now - started >= threshold:
                report.findings.append(Finding(
                    key, "estimate", e["rows"], (now - started) / DAY, f"not since the database started ({_fmt(started)})",
                    "InnoDB reports no write since the database started. This in-memory statistic is lost on "
                    "restarts, so it is an estimate."))
            else:
                report.findings.append(Finding(
                    key, "unknown", e["rows"], None, "unknown",
                    f"No change seen in {history_days:.1f} days of backups; not enough history yet for "
                    f"{ina.inactive_after_days} days."))
    return report


def due(cfg, now: float | None = None) -> bool:
    from .state import State
    if not cfg.inactive_tables.enabled:
        return False
    last = State(cfg.state_path).load().get("inactive_report", {}).get("last_report_ts", 0)
    return (now or time.time()) - last >= cfg.inactive_tables.report_every_days * DAY


def send_report(cfg, force: bool = False) -> Report:
    """Build the report, send it to Discord (if anything to say) and remember what was reported."""
    from . import notify
    from .state import State
    report = build(cfg)
    state = State(cfg.state_path)
    previously = set(state.load().get("inactive_report", {}).get("reported", []))
    inactive = report.by("confirmed") + report.by("estimate")
    new = [f for f in inactive if f.table not in previously] if cfg.inactive_tables.only_new else inactive
    still = [f for f in inactive if f not in new]
    unknown = report.by("unknown")

    if new or still or force:
        fields: list[tuple[str, str, bool]] = []
        for f in new[:18]:
            label = "✅ Confirmed" if f.status == "confirmed" else "≈ Estimate"
            rows = "empty" if f.rows == 0 else f"{f.rows} rows"
            fields.append((f"{f.table}", f"{label}: unchanged for {f.unchanged_days:.0f}+ days ({rows})\n"
                                        f"Last activity: {f.last_activity}\nEvidence: {f.evidence}", False))
        if len(new) > 18:
            fields.append(("More", f"{len(new) - 18} more tables (run `sqlbackup inactive` for the full list)", False))
        if still:
            fields.append(("Still inactive (reported before)", ", ".join(f.table for f in still)[:1000], False))
        if unknown:
            fields.append(("Not enough information yet", f"{len(unknown)} table(s): " +
                           ", ".join(f.table for f in unknown)[:900], False))
        fields.append(("How this is measured",
                       "Confirmed = identical data in every backup over the period (most reliable). Estimate = "
                       "database statistics only (InnoDB forgets them on restart). Nothing was changed or deleted; "
                       "some tables are legitimately used only rarely.", False))
        notify.send(cfg, "inactive", "🗂️ Inactive table report",
                    f"{len(new)} newly inactive, {len(still)} still inactive, {report.active} active, "
                    f"threshold {cfg.inactive_tables.inactive_after_days} days.", fields, force=force)

    def remember(s):
        s["inactive_report"] = {"last_report_ts": time.time(), "reported": sorted({f.table for f in inactive})}
    state.update(remember)
    audit("inactive_report", new=[f.table for f in new], still=[f.table for f in still], unknown=len(unknown))
    log.info("Inactive table report: %d new, %d still inactive, %d unknown", len(new), len(still), len(unknown))
    return report
