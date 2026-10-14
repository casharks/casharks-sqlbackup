"""Background service: interval and daily backups plus backups around FiveM server starts/stops.

Only one instance runs per configuration (enforced by an OS file lock that is released
automatically if the process dies). All timing is persisted in the state file, so restarting the
service never causes a burst of duplicate backups, and daily times missed while the computer was
off are caught up once.
"""

import os
import signal
import threading
from datetime import datetime, timedelta

from . import activity, notify
from .backup import perform_backup, sync_pending_remote
from .fsutil import IS_WINDOWS
from .locks import FileLock
from .logsetup import audit, get_logger
from .procwatch import ServerWatcher
from .state import State, now_iso, parse_iso

log = get_logger("service")


def lower_own_priority() -> None:
    try:
        if IS_WINDOWS:
            import ctypes
            k32 = ctypes.WinDLL("kernel32")
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x4000)
        else:
            os.nice(10)
    except OSError:
        pass


def last_daily_slot(times: list[str], now: datetime) -> datetime | None:
    """Most recent scheduled daily time at or before now."""
    slots = []
    for t in times:
        hour, minute = map(int, t.split(":"))
        for day in (now.date(), now.date() - timedelta(days=1)):
            slot = datetime(day.year, day.month, day.day, hour, minute).astimezone()
            if slot <= now:
                slots.append(slot)
    return max(slots) if slots else None


class Scheduler:
    """Pure decision logic, separated from the loop so it can be tested."""

    def __init__(self, cfg):
        self.s = cfg.schedule
        self.pending_start_at: datetime | None = None

    def on_events(self, events: list[str], now: datetime, last_backup: datetime | None) -> str | None:
        trigger = None
        for event in events:
            if event == "stop":
                self.pending_start_at = None
                if self.s.on_server_stop:
                    trigger = "server-stop"
            elif event == "start" and self.s.on_server_start:
                self.pending_start_at = now + timedelta(seconds=self.s.server_start_delay_seconds)
        return trigger

    def due(self, now: datetime, last_backup: datetime | None, last_failure: datetime | None,
            server_running: bool) -> str | None:
        if last_failure and (last_backup is None or last_failure > last_backup) and \
                now - last_failure < timedelta(minutes=self.s.retry_failed_after_minutes):
            return None
        if self.pending_start_at and now >= self.pending_start_at:
            self.pending_start_at = None
            gap = timedelta(minutes=self.s.min_minutes_between_event_backups)
            if last_backup is None or now - last_backup >= gap:
                return "server-start"
        slot = last_daily_slot(self.s.daily_times, now)
        if slot and (last_backup is None or last_backup < slot):
            return "daily"
        if self.s.interval_minutes and (server_running or not self.s.only_when_server_running):
            if last_backup is None or now - last_backup >= timedelta(minutes=self.s.interval_minutes):
                return "scheduled"
        return None


class Daemon:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = State(cfg.state_path)
        self.stop_event = threading.Event()

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
            if sig is not None:
                try:
                    signal.signal(sig, lambda *_: self.stop_event.set())
                except (ValueError, OSError):
                    pass

    def run(self) -> int:
        lock = FileLock(self.cfg.data_dir / "service.lock")
        if not lock.acquire():
            log.info("Background service is already running for %s; exiting.", self.cfg.path)
            return 0
        try:
            self._install_signal_handlers()
            lower_own_priority()
            self.state.update(lambda s: s.update(service={"pid": os.getpid(), "started_at": now_iso(),
                                                          "heartbeat_at": now_iso()}))
            log.info("Background service started (pid %d)", os.getpid())
            audit("service_start", pid=os.getpid())
            self._loop()
        finally:
            self.state.update(lambda s: s.get("service", {}).update(stopped_at=now_iso()))
            log.info("Background service stopped")
            lock.release()
        return 0

    def _mtime(self) -> float | None:
        try:
            return self.cfg.path.stat().st_mtime
        except OSError:
            return None

    def _reload(self):
        """Pick up edits to the config file without a restart; keep the old settings if the new file is invalid."""
        from . import config as config_mod
        try:
            new_cfg = config_mod.load(str(self.cfg.path))
        except Exception as exc:
            log.error("Config file changed but has a problem; still using the previous settings: %s", exc)
            notify.operation_failure(self.cfg, "Reload configuration", exc)
            return None
        log.info("Config file changed; new settings loaded")
        audit("config_reload")
        return new_cfg

    def _loop(self) -> None:
        cfg = self.cfg
        watcher = None
        if cfg.server_watch.enabled:
            try:
                watcher = ServerWatcher(cfg)
                log.info("Watching for the FiveM server; currently %s",
                         "running" if watcher.running else "not running")
            except OSError as exc:
                log.error("Server process detection unavailable: %s", exc)
        scheduler = Scheduler(cfg)
        tick = min(cfg.server_watch.poll_seconds, 30)
        next_remote_retry = datetime.now().astimezone() + timedelta(minutes=10)
        first = cfg.schedule.on_service_start

        config_mtime = self._mtime()
        while not self.stop_event.is_set():
            if self._mtime() != config_mtime:
                config_mtime = self._mtime()
                new_cfg = self._reload()
                if new_cfg is not None:
                    cfg = self.cfg = new_cfg
                    pending = scheduler.pending_start_at
                    scheduler = Scheduler(cfg)
                    scheduler.pending_start_at = pending
                    if watcher:
                        watcher.cfg = cfg
                    tick = min(cfg.server_watch.poll_seconds, 30)
            now = datetime.now().astimezone()
            data = self.state.load()
            last_backup = parse_iso(data.get("last_success_at"))
            last_failure = parse_iso(data.get("last_failure_at"))

            events: list[str] = []
            if watcher:
                try:
                    events = watcher.poll()
                except OSError as exc:
                    log.warning("Process check failed: %s", exc)
            for e in events:
                log.info("FiveM server %s detected", "start" if e == "start" else "stop")
            trigger = "service-start" if first else scheduler.on_events(events, now, last_backup)
            first = False
            trigger = trigger or scheduler.due(now, last_backup, last_failure, bool(watcher and watcher.running))

            if trigger:
                try:
                    perform_backup(cfg, trigger)
                except Exception as exc:
                    log.exception("Unexpected error during backup")
                    notify.operation_failure(cfg, "Background backup", exc, {"Trigger": trigger})
            elif activity.due(cfg):
                try:
                    activity.send_report(cfg)
                except Exception as exc:
                    log.exception("Inactive table report failed")
                    notify.operation_failure(cfg, "Inactive table report", exc)
            elif cfg.remote.enabled and now >= next_remote_retry:
                next_remote_retry = now + timedelta(minutes=10)
                try:
                    sync_pending_remote(cfg)
                except Exception:
                    log.exception("Unexpected error while retrying remote copies")

            self.state.update(lambda s: s.setdefault("service", {}).update(heartbeat_at=now_iso(), pid=os.getpid()))
            self.stop_event.wait(tick)
