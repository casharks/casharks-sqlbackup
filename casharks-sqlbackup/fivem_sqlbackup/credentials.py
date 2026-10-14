"""Secret storage.

Windows: values are encrypted with DPAPI (bound to the current user, or to the machine when
security.dpapi_scope = "machine"). Other systems: values are kept in a file readable only by the
owner (mode 0600); use env:VARIABLE credentials if that is not sufficient.
"""

import base64
import getpass
import json
import os
import sys
from pathlib import Path

from .errors import ConfigError
from .fsutil import IS_WINDOWS, atomic_write_text
from .logsetup import REDACTOR

DB_PASSWORD = "database.password"
RESTORE_PASSWORD = "restore.password"
WEBHOOK_URL = "notify.webhook_url"
KNOWN_SECRETS = {
    DB_PASSWORD: "password for the backup database account",
    RESTORE_PASSWORD: "password for the restore database account",
    WEBHOOK_URL: "default webhook URL (used when notify.webhook_url = \"store\")",
    "notify.success_webhook_url": "success webhook (notify.success_webhook_url = \"store\")",
    "notify.failure_webhook_url": "failure webhook (notify.failure_webhook_url = \"store\")",
    "notify.warning_webhook_url": "warning webhook (notify.warning_webhook_url = \"store\")",
    "notify.inactive_webhook_url": "inactive-table report webhook (notify.inactive_webhook_url = \"store\")",
}
_ENTROPY = b"fivem-sqlbackup/v1"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _PROTO = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
              wintypes.DWORD, ctypes.POINTER(_Blob)]
    _crypt32.CryptProtectData.argtypes = _PROTO
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
                                            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL

    def _blob(data: bytes) -> tuple[_Blob, object]:
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _dpapi(data: bytes, protect: bool, machine: bool) -> bytes:
        flags = 0x1 | (0x4 if machine and protect else 0)
        src, _keep = _blob(data)
        ent, _keep2 = _blob(_ENTROPY)
        out = _Blob()
        if protect:
            ok = _crypt32.CryptProtectData(ctypes.byref(src), "fivem-sqlbackup", ctypes.byref(ent), None, None,
                                           flags, ctypes.byref(out))
        else:
            ok = _crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None, None, flags,
                                             ctypes.byref(out))
        if not ok:
            raise OSError(ctypes.get_last_error(), "DPAPI operation failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)


class SecretStore:
    def __init__(self, path: Path, scope: str = "user"):
        self.path = Path(path)
        self.scope = scope

    @property
    def protection(self) -> str:
        if IS_WINDOWS:
            return "dpapi-machine" if self.scope == "machine" else "dpapi-user"
        return "file"

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "secrets": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data.get("secrets"), dict):
                raise ValueError("missing 'secrets'")
            return data
        except (OSError, ValueError) as exc:
            raise ConfigError(f"Secret store {self.path} is unreadable: {exc}") from None

    def _save(self, data: dict) -> None:
        atomic_write_text(self.path, json.dumps(data, indent=2) + "\n", private=True)

    def names(self) -> list[str]:
        return sorted(self._load()["secrets"])

    def get(self, name: str) -> str | None:
        entry = self._load()["secrets"].get(name)
        if entry is None:
            return None
        raw = base64.b64decode(entry["value"])
        kind = entry.get("protection")
        if kind in ("dpapi-user", "dpapi-machine"):
            if not IS_WINDOWS:
                raise ConfigError(f"Secret '{name}' was encrypted on Windows and cannot be read here.")
            try:
                raw = _dpapi(raw, protect=False, machine=kind == "dpapi-machine")
            except OSError:
                raise ConfigError(f"Secret '{name}' cannot be decrypted by this account.",
                                  hint=f"It was stored by another user or computer. Store it again:  "
                                       f"sqlbackup credentials set {name}") from None
        elif kind != "file":
            raise ConfigError(f"Secret '{name}' uses unknown protection '{kind}'.")
        value = raw.decode("utf-8")
        REDACTOR.add(value)
        return value

    def set(self, name: str, value: str) -> None:
        if not value or "\x00" in value or "\n" in value or "\r" in value:
            raise ConfigError("Secret values cannot be empty or contain line breaks.")
        raw = value.encode("utf-8")
        if IS_WINDOWS:
            raw = _dpapi(raw, protect=True, machine=self.scope == "machine")
        data = self._load()
        data["secrets"][name] = {"protection": self.protection, "value": base64.b64encode(raw).decode("ascii")}
        self._save(data)
        REDACTOR.add(value)

    def delete(self, name: str) -> bool:
        data = self._load()
        if data["secrets"].pop(name, None) is None:
            return False
        self._save(data)
        return True


def resolve(method: str, store: SecretStore, secret_name: str, label: str) -> str | None:
    """Return the secret described by a credential method, or None for method 'none'."""
    if method == "none":
        return None
    if method == "store":
        value = store.get(secret_name)
        if value is None:
            raise ConfigError(f"No {label} is stored.", hint=f"Store it with:  sqlbackup credentials set {secret_name}")
    elif method.startswith("env:"):
        var = method[4:]
        value = os.environ.get(var)
        if not value:
            raise ConfigError(f"Environment variable {var} (expected to hold the {label}) is not set.")
    elif method == "prompt":
        if not (sys.stdin and sys.stdin.isatty()):
            raise ConfigError(f"The {label} must be typed in, but no interactive terminal is available.",
                              hint="Use credential = \"store\" or env:VARIABLE for unattended runs.")
        value = getpass.getpass(f"Enter {label}: ")
    else:
        raise ConfigError(f"Unknown credential method '{method}'.")
    REDACTOR.add(value)
    return value
