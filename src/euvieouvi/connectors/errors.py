"""Stable connector error categories used by orchestration."""


class ConnectorError(Exception):
    """Base class for failures at an external connector boundary."""


class ConnectorConfigurationError(ConnectorError):
    """Connector configuration is invalid."""


class ConnectorAuthenticationError(ConnectorError):
    """External credentials were rejected."""


class ConnectorConnectionError(ConnectorError):
    """The external service could not be reached."""


class ConnectorTimeoutError(ConnectorConnectionError):
    """A finite connection or read timeout expired."""


class ConnectorRateLimitError(ConnectorError):
    """The external service asked the client to reduce request rate."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ConnectorNotFoundError(ConnectorError):
    """The requested external resource does not exist."""


class ConnectorResponseError(ConnectorError):
    """The external response is invalid or unsupported."""


class ConnectorPaginationError(ConnectorResponseError):
    """A page did not provide safe forward progress."""
