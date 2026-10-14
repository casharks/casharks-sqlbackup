"""Discord / JSON webhook alerts.

Each alert type (success, failure, warning, inactive) can go to its own webhook; empty falls back to
notify.webhook_url. Sending never raises: a Discord outage, rate limit or bad URL is logged and the
backup carries on. Identical failures/warnings are not repeated within repeat_error_after_minutes.
All text is passed through the secret redactor before it leaves the machine.
"""

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from . import APP_NAME, __author__, __version__
from .errors import AppError
from .logsetup import REDACTOR, get_logger

log = get_logger("notify")
KINDS = ("success", "failure", "warning", "inactive")
_COLORS = {"success": 0x2EAD57, "failure": 0xD9363E, "warning": 0xE8A317, "inactive": 0x5B7FD6, "test": 0x3B82F6}
_ENABLED_FLAG = {"success": "on_success", "failure": "on_failure", "warning": "on_warning", "inactive": None}
_MAX_FIELDS = 25


def validate_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise AppError("Webhook URLs must start with https:// (plain http is only allowed for localhost).")
    if not parts.hostname:
        raise AppError("The webhook URL has no host name.")


def secret_name(kind: str | None) -> str:
    return f"notify.{kind}_webhook_url" if kind else "notify.webhook_url"


def webhook_for(cfg, kind: str) -> str | None:
    """URL for an alert type: its own setting, else the shared one. 'store' reads the encrypted store."""
    from .engines import secret_store
    n = cfg.notify
    for key, value in ((f"{kind}_webhook_url", getattr(n, f"{kind}_webhook_url", "")), ("webhook_url", n.webhook_url)):
        if value == "store":
            value = secret_store(cfg).get(secret_name(None if key == "webhook_url" else kind)) or ""
        if value:
            REDACTOR.add(value)
            return value
    return None


def _clip(text: str, limit: int) -> str:
    text = REDACTOR.scrub(str(text))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _payload(cfg, fmt: str, kind: str, title: str, description: str, fields: list[tuple[str, str, bool]]) -> dict:
    now = datetime.now().astimezone()
    if fmt == "discord":
        embed = {
            "title": _clip(title, 256),
            "description": _clip(description, 4000),
            "color": _COLORS.get(kind, 0),
            "timestamp": now.isoformat(),
            "footer": {"text": _clip(f"{APP_NAME} {__version__} by {__author__} • {cfg.general.instance_name}", 200)},
            "fields": [{"name": _clip(n, 256), "value": _clip(v or "-", 1024), "inline": inline}
                       for n, v, inline in fields[:_MAX_FIELDS]],
        }
        return {"username": APP_NAME, "embeds": [embed]}
    return {"app": APP_NAME, "version": __version__, "instance": cfg.general.instance_name, "kind": kind,
            "title": _clip(title, 256), "description": _clip(description, 4000),
            "fields": {_clip(n, 256): _clip(v, 1024) for n, v, _ in fields}, "time": now.isoformat(timespec="seconds")}


def _post(url: str, payload: dict, timeout: int) -> bool:
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(3):
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "User-Agent": f"{APP_NAME}/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 2:
                try:
                    wait = float(json.loads(exc.read() or b"{}").get("retry_after", 1))
                except (ValueError, AttributeError):
                    wait = float(exc.headers.get("Retry-After", 1) or 1)
                log.info("Webhook rate limited; retrying in %.1fs", wait)
                time.sleep(min(max(wait, 0.5), 15))
                continue
            if 500 <= exc.code < 600 and attempt < 2:
                time.sleep(2)
                continue
            log.warning("Webhook answered HTTP %s%s", exc.code,
                        " (the webhook was probably deleted - create a new one)" if exc.code in (401, 404) else "")
            return False
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if attempt < 1:
                time.sleep(2)
                continue
            log.warning("Webhook unreachable: %s", REDACTOR.scrub(str(exc)))
            return False
    return False


def _throttled(cfg, kind: str, dedupe: str) -> tuple[bool, int]:
    """(skip?, number of identical alerts suppressed before this one)."""
    from .state import State
    minutes = cfg.notify.repeat_error_after_minutes
    key = kind + ":" + hashlib.sha1(dedupe.encode("utf-8", "replace")).hexdigest()[:16]
    now = time.time()
    result = {"skip": False, "suppressed": 0}

    def change(s):
        recent = {k: v for k, v in s.get("notify_recent", {}).items() if now - v["last"] < max(minutes, 1) * 120}
        entry = recent.get(key)
        if entry and now - entry["last"] < minutes * 60:
            entry["suppressed"] = entry.get("suppressed", 0) + 1
            result["skip"] = True
        else:
            result["suppressed"] = entry.get("suppressed", 0) if entry else 0
            recent[key] = {"last": now, "suppressed": 0}
        s["notify_recent"] = recent
    State(cfg.state_path).update(change)
    return result["skip"], result["suppressed"]


def send(cfg, kind: str, title: str, description: str = "", fields: list[tuple[str, str, bool]] | None = None,
         dedupe: str | None = None, force: bool = False) -> bool:
    """Send one alert. Never raises."""
    try:
        n = cfg.notify
        if not force:
            flag = _ENABLED_FLAG.get(kind)
            if not n.enabled or (flag and not getattr(n, flag)):
                return False
        url = webhook_for(cfg, kind)
        if not url:
            if n.enabled or force:
                log.warning("No webhook URL configured for %s alerts (notify.%s_webhook_url / notify.webhook_url)",
                            kind, kind)
            return False
        validate_url(url)
        fields = list(fields or [])
        if dedupe and not force and n.repeat_error_after_minutes > 0:
            skip, suppressed = _throttled(cfg, kind, dedupe)
            if skip:
                log.info("Not repeating identical %s alert (%s)", kind, title)
                return False
            if suppressed:
                fields.append(("Repeated", f"This alert was suppressed {suppressed} more time(s) since the last "
                                           "message.", False))
        ok = _post(url, _payload(cfg, n.format, kind, title, description, fields), n.timeout_seconds)
        if ok:
            log.info("Sent %s alert: %s", kind, title)
        return ok
    except Exception as exc:
        log.warning("Could not send %s alert: %s", kind, REDACTOR.scrub(str(exc)))
        return False


def _when(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts).astimezone() if ts else datetime.now().astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z").strip()


def _size(n: int) -> str:
    from .fsutil import human_size
    return human_size(n)


def backup_success(cfg, result, manifest: dict) -> None:
    files = [f"`{d['file']}` ({d['name']}, {_size(d['size'])}, {sum(d['tables'].values())} rows)"
             for d in manifest["databases"]]
    verified = "✅ Passed: checksum, completeness and row counts" if manifest.get("verified") \
        else "⚠️ Not re-read (verify.after_backup = false)"
    fields = [
        ("Type", f"Full database backup ({manifest['trigger']})", True),
        ("Size", _size(result.size), True),
        ("Duration", f"{result.duration:.1f}s", True),
        ("Database(s)", ", ".join(d["name"] for d in manifest["databases"]), True),
        ("Date / time", _when(manifest["created_ts"]), True),
        ("Validation", verified, False),
        ("File(s)", "\n".join(files), False),
    ]
    ok_copies = [i for i in result.selected if i.get("ok")]
    room = _MAX_FIELDS - len(fields) - 2
    for item in ok_copies[:room]:
        fields.append((f"📄 Table copy: {item['table']}",
                       f"`{item['name']}` • {_size(item['size'])} • {item['rows']} rows • ✅ validated", False))
    if len(ok_copies) > room:
        fields.append(("More table copies", f"{len(ok_copies) - room} more saved (see the log)", False))
    bad = [i for i in result.selected if not i.get("ok")]
    if bad:
        fields.append(("Table copies with problems", ", ".join(i["table"] for i in bad) + " (separate alert sent)",
                       False))
    send(cfg, "success", "✅ Backup completed", f"Run ID: `{manifest['id']}`", fields)


def backup_failure(cfg, trigger: str, error: AppError, databases: list[str]) -> None:
    fields = [
        ("Type", f"Full database backup ({trigger})", True),
        ("Database(s)", ", ".join(databases) or "-", True),
        ("Date / time", _when(), True),
        ("Error", str(error), False),
        ("What to do", error.hint or "Run `sqlbackup doctor` on the server and check logs/sqlbackup.log.", False),
        ("Result", "No backup was saved for this run. Earlier backups are untouched.", False),
    ]
    send(cfg, "failure", "❌ Backup FAILED", "The scheduled/requested backup could not be completed.", fields,
         dedupe=f"backup:{error}")


def table_copy_problem(cfg, run_id: str, item: dict) -> None:
    db, _, table = item["table"].partition(".")
    missing = "does not exist" in item["error"] or "not part of the full backup" in item["error"]
    kind = "warning" if missing else "failure"
    action = ("Check the table name in Config.selected_tables.tables (list real names with "
              "`sqlbackup tables available`), or remove it from the list.") if missing else \
        "Check free disk space and logs/sqlbackup.log. The full backup itself is complete and usable."
    fields = [
        ("Type", "Individual table copy", True),
        ("Database", db, True),
        ("Table", table, True),
        ("Run ID", f"`{run_id}`", False),
        ("Date / time", _when(), True),
        ("Problem", item["error"], False),
        ("What to do", action, False),
    ]
    title = "⚠️ Selected table warning" if missing else "❌ Table copy FAILED"
    send(cfg, kind, title, "The full backup succeeded; only this separate copy was affected.", fields,
         dedupe=f"table:{item['table']}:{item['error']}")


def warnings(cfg, run_id: str | None, messages: list[str]) -> None:
    if not messages:
        return
    fields = [("Run ID", f"`{run_id}`" if run_id else "-", True), ("Date / time", _when(), True)]
    fields += [(f"Warning {i}", m, False) for i, m in enumerate(messages[:20], 1)]
    send(cfg, "warning", "⚠️ Backup completed with warnings", "The backup was saved, but needs attention.", fields,
         dedupe="warn:" + "|".join(sorted(messages)))


def operation_failure(cfg, operation: str, error: Exception, details: dict[str, str] | None = None) -> None:
    hint = getattr(error, "hint", None)
    fields = [("Operation", operation, True), ("Date / time", _when(), True)]
    fields += [(k, v, True) for k, v in (details or {}).items()]
    fields += [("Error", str(error), False),
               ("What to do", hint or "Check logs/sqlbackup.log and run `sqlbackup doctor`.", False)]
    send(cfg, "failure", f"❌ {operation} FAILED", "", fields, dedupe=f"op:{operation}:{error}")


def test(cfg, kind: str) -> bool:
    fields = [("Alert type", kind, True), ("Date / time", _when(), True),
              ("Webhook", "This message proves the URL in your config works.", False)]
    return send(cfg, kind, f"🔔 Test {kind} alert", "If you can read this, alerts of this type will arrive here.",
                fields, force=True)
