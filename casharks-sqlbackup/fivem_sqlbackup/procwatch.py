"""Detect the FiveM server process without third-party packages.

Windows uses the Toolhelp snapshot API and NtQueryInformationProcess for command lines; Linux reads
/proc; other systems fall back to 'ps'.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .fsutil import IS_WINDOWS, popen_kwargs


@dataclass(frozen=True)
class Proc:
    pid: int
    name: str
    cmdline: str | None


if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    class _ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    class _UnicodeString(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]

    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
                                                 ctypes.POINTER(wintypes.ULONG)]
    _ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    _INVALID = ctypes.c_void_p(-1).value

    def _win_cmdline(pid: int) -> str | None:
        handle = _k32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            size = wintypes.ULONG(0)
            _ntdll.NtQueryInformationProcess(handle, 60, None, 0, ctypes.byref(size))
            if not size.value:
                return None
            buf = ctypes.create_string_buffer(size.value)
            if _ntdll.NtQueryInformationProcess(handle, 60, buf, size, ctypes.byref(size)) != 0:
                return None
            us = _UnicodeString.from_buffer(buf)
            return ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer else ""
        finally:
            _k32.CloseHandle(handle)

    def _windows(names: set[str], want_cmdline: bool) -> list[Proc]:
        snap = _k32.CreateToolhelp32Snapshot(0x2, 0)
        if not snap or snap == _INVALID:
            raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
        found = []
        try:
            entry = _ProcessEntry()
            entry.dwSize = ctypes.sizeof(_ProcessEntry)
            more = _k32.Process32FirstW(snap, ctypes.byref(entry))
            while more:
                name = entry.szExeFile
                if name.lower() in names:
                    pid = entry.th32ProcessID
                    found.append(Proc(pid, name, _win_cmdline(pid) if want_cmdline else None))
                more = _k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            _k32.CloseHandle(snap)
        return found


def _linux(names: set[str]) -> list[Proc]:
    found = []
    me = os.getpid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            comm = (entry / "comm").read_text().strip()
        except OSError:
            continue
        cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        if comm.lower() in names or any(n in cmdline.lower() for n in names):
            found.append(Proc(int(entry.name), comm, cmdline))
    return found


def _ps(names: set[str]) -> list[Proc]:
    r = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True, **popen_kwargs())
    found = []
    for line in r.stdout.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit() and int(pid) != os.getpid() and any(n in args.lower() for n in names):
            found.append(Proc(int(pid), args.split(" ")[0].rsplit("/", 1)[-1], args))
    return found


def find_processes(process_names: list[str], must_contain: list[str]) -> list[Proc]:
    names = {n.lower() for n in process_names if n}
    needles = [m.lower() for m in must_contain if m]
    if IS_WINDOWS:
        procs = _windows(names, want_cmdline=bool(needles))
    elif Path("/proc/self").exists():
        procs = _linux(names)
    else:
        procs = _ps(names)
    if not needles:
        return procs
    return [p for p in procs if p.cmdline is not None and all(n in p.cmdline.lower() for n in needles)]


def server_processes(cfg) -> list[Proc]:
    sw = cfg.server_watch
    return find_processes(sw.process_names, sw.command_line_contains)


class ServerWatcher:
    """Turns process snapshots into 'start' / 'stop' events."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.pids: set[int] = {p.pid for p in server_processes(cfg)}

    @property
    def running(self) -> bool:
        return bool(self.pids)

    def poll(self) -> list[str]:
        current = {p.pid for p in server_processes(self.cfg)}
        events = []
        if self.pids and not current:
            events.append("stop")
        elif current and not self.pids:
            events.append("start")
        elif current and self.pids and not (current & self.pids):
            events += ["stop", "start"]
        self.pids = current
        return events
