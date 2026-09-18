"""Finance Core domain errors."""

from __future__ import annotations


class FinanceError(Exception):
    """Base class for expected Finance Core failures."""


class FinanceValidationError(FinanceError, ValueError):
    """Input or invariant validation failed."""


class FinanceNotFoundError(FinanceError, LookupError):
    """A referenced Finance Core record does not exist."""


class FinanceConflictError(FinanceError):
    """The requested state transition conflicts with the immutable ledger."""


class FinanceAuthorizationError(FinanceError, PermissionError):
    """An authenticated operator is required or not authorized."""
