"""Automatic start of the background service.

Windows: a Task Scheduler task. 'logon' mode runs as the signed-in user from logon (no admin
needed, can decrypt user-scoped secrets). 'boot' mode starts with Windows without anyone signed in
(S4U logon, needs admin to register, secrets must use dpapi_scope = "machine"). A repeating trigger
acts as a watchdog: if the service stops, the task starts it again; while it runs, Task Scheduler
ignores the trigger, and a second copy would exit anyway because of the instance lock.

Linux: a systemd user service with Restart=always.
"""

import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from . import APP_ID, APP_NAME
from .config import APP_ROOT
from .errors import AppError
from .fsutil import IS_WINDOWS, NO_WINDOW, current_user_sid
from .locks import is_locked


def interpreter(windowless: bool = True) -> Path:
    exe = Path(sys.executable)
    if IS_WINDOWS and windowless:
        candidate = exe.with_name("pythonw.exe")
        if candidate.is_file():
            return candidate
    return exe


def service_args(cfg) -> list[str]:
    return ["-m", "fivem_sqlbackup", "--config", str(cfg.path), "run"]


def is_running(cfg) -> bool:
    return is_locked(cfg.data_dir / "service.lock")


def wait_until_stopped(cfg, timeout: float = 20) -> bool:
    """schtasks /End and systemctl stop return before the process has exited; wait for its lock."""
    import time
    deadline = time.monotonic() + timeout
    while is_running(cfg):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.5)
    return True


def task_xml(cfg, python: Path, start: datetime | None = None) -> str:
    sid = current_user_sid()
    if not sid:
        raise AppError("Could not determine the current Windows user.")
    mode = cfg.service.mode
    start = (start or datetime.now()).replace(microsecond=0).isoformat()
    boot = mode == "boot"
    trigger = "<BootTrigger><Enabled>true</Enabled></BootTrigger>" if boot else \
        f"<LogonTrigger><Enabled>true</Enabled><UserId>{sid}</UserId></LogonTrigger>"
    args = subprocess.list2cmdline(service_args(cfg))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>{escape(APP_NAME)}</Author>
    <Description>{escape(f"{APP_NAME}: automatic database backups for '{cfg.general.instance_name}'. Config: {cfg.path}")}</Description>
  </RegistrationInfo>
  <Triggers>
    {trigger}
    <TimeTrigger>
      <Repetition><Interval>PT{int(cfg.service.watchdog_minutes)}M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{sid}</UserId>
      <LogonType>{"S4U" if boot else "InteractiveToken"}</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>10</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(python))}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(str(APP_ROOT))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, creationflags=NO_WINDOW)


def _unit_name(cfg) -> str:
    return f"{APP_ID}-{cfg.general.instance_name}.service"


def _unit_path(cfg) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "systemd" / "user" / _unit_name(cfg)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def _unit_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def install(cfg, python: Path | None = None) -> str:
    python = python or interpreter()
    if IS_WINDOWS:
        if installed(cfg):
            _schtasks("/End", "/TN", cfg.task_name)
        wait_until_stopped(cfg)
        xml = task_xml(cfg, python)
        fd, tmp = tempfile.mkstemp(suffix=".xml", dir=cfg.tmp_dir if cfg.tmp_dir.is_dir() else None)
        os.close(fd)
        try:
            Path(tmp).write_text(xml, encoding="utf-16")
            r = _schtasks("/Create", "/TN", cfg.task_name, "/XML", tmp, "/F")
        finally:
            os.unlink(tmp)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).strip()
            hint = "Boot mode must be installed from an administrator terminal." if cfg.service.mode == "boot" else None
            raise AppError(f"Could not create the scheduled task: {msg}", hint=hint)
        _schtasks("/Run", "/TN", cfg.task_name)
        return f"Scheduled task '{cfg.task_name}' created and started ({cfg.service.mode} mode, interpreter {python})."
    unit = _unit_path(cfg)
    unit.parent.mkdir(parents=True, exist_ok=True)
    exec_start = " ".join(_unit_quote(a) for a in [str(interpreter(False)), *service_args(cfg)])
    unit.write_text(f"""[Unit]
Description={APP_NAME} ({cfg.general.instance_name})
After=network-online.target mariadb.service mysql.service

[Service]
Type=simple
WorkingDirectory={_unit_quote(str(APP_ROOT))}
ExecStart={exec_start}
Restart=always
RestartSec=30
Nice=10

[Install]
WantedBy=default.target
""", encoding="utf-8")
    for args in (("daemon-reload",), ("enable", "--now", _unit_name(cfg))):
        r = _systemctl(*args)
        if r.returncode != 0:
            raise AppError(f"systemctl --user {' '.join(args)} failed: {r.stderr.strip()}")
    return (f"systemd user service {_unit_name(cfg)} enabled and started. To run it without an active login, "
            f"run once:  loginctl enable-linger {os.environ.get('USER', '<user>')}")


def uninstall(cfg) -> str:
    """Remove the scheduled task/service only. Backups, configuration and logs are left untouched."""
    if IS_WINDOWS:
        if _schtasks("/Query", "/TN", cfg.task_name).returncode != 0:
            return f"No scheduled task named '{cfg.task_name}' was found."
        _schtasks("/End", "/TN", cfg.task_name)
        wait_until_stopped(cfg)
        r = _schtasks("/Delete", "/TN", cfg.task_name, "/F")
        if r.returncode != 0:
            raise AppError(f"Could not delete the scheduled task: {(r.stderr or r.stdout).strip()}")
        return f"Scheduled task '{cfg.task_name}' stopped and removed."
    unit = _unit_path(cfg)
    if not unit.exists():
        return f"No systemd user service {_unit_name(cfg)} was found."
    _systemctl("disable", "--now", _unit_name(cfg))
    unit.unlink()
    _systemctl("daemon-reload")
    return f"systemd user service {_unit_name(cfg)} stopped and removed."


def installed(cfg) -> bool:
    if IS_WINDOWS:
        return _schtasks("/Query", "/TN", cfg.task_name).returncode == 0
    return _unit_path(cfg).exists()


def start(cfg) -> str:
    if IS_WINDOWS:
        r = _schtasks("/Run", "/TN", cfg.task_name)
    else:
        r = _systemctl("start", _unit_name(cfg))
    if r.returncode != 0:
        raise AppError(f"Could not start the service: {(r.stderr or r.stdout).strip()}",
                       hint="Install it first:  sqlbackup service install")
    return "Background service start requested."


def stop(cfg) -> str:
    if IS_WINDOWS:
        r = _schtasks("/End", "/TN", cfg.task_name)
    else:
        r = _systemctl("stop", _unit_name(cfg))
    if r.returncode != 0:
        raise AppError(f"Could not stop the service: {(r.stderr or r.stdout).strip()}")
    if not wait_until_stopped(cfg):
        raise AppError("The service did not stop within 20 seconds.", hint="Check Task Manager for pythonw.exe.")
    return "Background service stopped. (The watchdog trigger will start it again unless you uninstall it.)"
