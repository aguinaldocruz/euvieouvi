"""Domain and configuration exceptions."""


class EuvieouviError(Exception):
    """Base exception for expected euvieouvi failures."""


class ConfigurationError(EuvieouviError):
    """Raised when application configuration is invalid."""
