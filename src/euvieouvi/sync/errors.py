"""Synchronization control-flow errors."""


class SyncError(Exception):
    """Base synchronization failure."""


class SyncAlreadyRunningError(SyncError):
    """Another execution already owns the installation lock."""


class SyncSourceUnavailableError(SyncError):
    """The requested source is absent or disabled."""


class SyncCancelledError(SyncError):
    """Cancellation was observed at a safe boundary."""
