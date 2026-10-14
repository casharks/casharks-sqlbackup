"""Retention policy.

A backup is kept if any rule selects it: the newest keep_last; the newest of each of the last
keep_daily / keep_weekly / keep_monthly periods that have backups; pinned; or among the newest
min_keep. max_age_days then removes old backups unless they are pinned or within min_keep.
Only valid backup sets created by this tool are ever deleted.
"""

import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta

from .catalog import MANIFEST, BackupSet, list_sets
from .fsutil import is_within
from .logsetup import audit, get_logger

log = get_logger("retention")


@dataclass
class Item:
    id: str
    created: datetime
    pinned: bool = False


def _periodic(items: list[Item], count: int, key) -> set[str]:
    keep: set[str] = set()
    seen: set = set()
    for item in items:
        period = key(item.created)
        if period in seen:
            continue
        if len(seen) >= count:
            break
        seen.add(period)
        keep.add(item.id)
    return keep


def plan(items: list[Item], policy, now: datetime) -> tuple[list[str], dict[str, list[str]]]:
    """Return (ids to delete, reasons each kept id survives). items may be in any order."""
    items = sorted(items, key=lambda i: i.created, reverse=True)
    reasons: dict[str, list[str]] = {i.id: [] for i in items}
    rules_active = any((policy.keep_last, policy.keep_daily, policy.keep_weekly, policy.keep_monthly))

    def mark(ids, why):
        for i in ids:
            reasons[i].append(why)

    if rules_active:
        mark([i.id for i in items[:policy.keep_last]], "last")
        if policy.keep_daily:
            mark(_periodic(items, policy.keep_daily, lambda d: d.date()), "daily")
        if policy.keep_weekly:
            mark(_periodic(items, policy.keep_weekly, lambda d: d.isocalendar()[:2]), "weekly")
        if policy.keep_monthly:
            mark(_periodic(items, policy.keep_monthly, lambda d: (d.year, d.month)), "monthly")
    else:
        mark([i.id for i in items], "no count limits")

    protected = {i.id for i in items[:policy.min_keep]} | {i.id for i in items if i.pinned}
    mark([i.id for i in items if i.pinned], "pinned")
    mark([i.id for i in items[:policy.min_keep]], "min_keep")

    if policy.max_age_days:
        cutoff = now - timedelta(days=policy.max_age_days)
        for i in items:
            if i.created < cutoff and i.id not in protected:
                reasons[i.id] = []

    delete = [i.id for i in items if not reasons[i.id]]
    return delete, {k: v for k, v in reasons.items() if v}


def prune(cfg, dry_run: bool = False) -> list[BackupSet]:
    backup_dir = cfg.backup_dir
    sets = [s for s in list_sets(backup_dir) if s.valid]
    items = [Item(s.id, s.created, s.pinned) for s in sets]
    delete_ids, _ = plan(items, cfg.retention, datetime.now().astimezone())
    removed = []
    for bset in sets:
        if bset.id not in delete_ids:
            continue
        if not (is_within(bset.path, backup_dir) and (bset.path / MANIFEST).is_file()):
            log.error("Refusing to delete %s: not a backup set inside %s", bset.path, backup_dir)
            continue
        if dry_run:
            removed.append(bset)
            continue
        try:
            shutil.rmtree(bset.path)
            removed.append(bset)
            log.info("Retention removed %s", bset.id)
        except OSError as exc:
            log.error("Could not remove %s: %s", bset.path, exc)
    if removed and not dry_run:
        audit("retention", removed=[s.id for s in removed])
    return removed
