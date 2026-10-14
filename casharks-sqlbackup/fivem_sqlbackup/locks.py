"""Inter-process file locks. The OS releases them automatically if the holder crashes."""

import os
import time
from pathlib import Path

from .fsutil import IS_WINDOWS

if IS_WINDOWS:
    import msvcrt
else:
    import fcntl


class FileLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self, timeout: float = 0) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        while True:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                if IS_WINDOWS:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return True
            except OSError:
                os.close(fd)
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.2)

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if IS_WINDOWS:
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self):
        if not self.acquire(timeout=30):
            raise TimeoutError(f"Timed out waiting for lock {self.path}")
        return self

    def __exit__(self, *exc):
        self.release()


def is_locked(path: Path) -> bool:
    """True when another process currently holds the lock."""
    if not Path(path).exists():
        return False
    probe = FileLock(path)
    if probe.acquire():
        probe.release()
        return False
    return True
