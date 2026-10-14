class AppError(Exception):
    """An expected failure with a message suitable for the user."""

    exit_code = 1

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


class ConfigError(AppError):
    exit_code = 2


class DbError(AppError):
    def __init__(self, message: str, code: int | None = None, transient: bool = False, hint: str | None = None):
        super().__init__(message, hint)
        self.code = code
        self.transient = transient


class StorageError(AppError):
    def __init__(self, message: str, transient: bool = False, hint: str | None = None):
        super().__init__(message, hint)
        self.transient = transient


class BackupBusy(AppError):
    exit_code = 3


class Aborted(AppError):
    exit_code = 4
