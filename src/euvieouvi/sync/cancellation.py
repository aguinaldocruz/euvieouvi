"""Thread-safe cooperative cancellation token."""

from threading import Event

from euvieouvi.sync.errors import SyncCancelledError


class CancellationToken:
    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise SyncCancelledError("Synchronization was cancelled.")
