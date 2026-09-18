"""Provider contracts for calendar synchronization."""

from __future__ import annotations

from typing import Any, Protocol

from finance_core.errors import FinanceError


class CalendarProviderError(FinanceError):
    """An external calendar request failed without changing Finance data."""


class CalendarSyncResetRequired(CalendarProviderError):
    """The provider invalidated its incremental sync cursor."""


class CalendarProvider(Protocol):
    def list_events(
        self,
        *,
        calendar_id: str,
        time_min: str | None = None,
        time_max: str | None = None,
        sync_token: str | None = None,
    ) -> dict[str, Any]:
        """Return normalized events and an optional next sync token."""

