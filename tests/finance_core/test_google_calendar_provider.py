"""Google Calendar provider normalization and incremental request behavior."""

from __future__ import annotations

from typing import Any


class _Request:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    def execute(self) -> dict[str, Any]:
        return self.payload


class _Events:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> _Request:
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return _Request(
                {
                    "items": [
                        {
                            "id": "event-1",
                            "summary": "Support session",
                            "status": "confirmed",
                            "etag": "etag-1",
                            "start": {"dateTime": "2026-08-20T10:00:00+09:00", "timeZone": "Asia/Tokyo"},
                            "end": {"dateTime": "2026-08-20T12:00:00+09:00", "timeZone": "Asia/Tokyo"},
                        }
                    ],
                    "nextPageToken": "page-2",
                }
            )
        return _Request({"items": [], "nextSyncToken": "sync-1"})


class _Service:
    def __init__(self):
        self.events_api = _Events()

    def events(self) -> _Events:
        return self.events_api


class _CalendarList:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> _Request:
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return _Request(
                {
                    "items": [
                        {"id": "other", "summary": "Other calendar"},
                    ],
                    "nextPageToken": "calendar-page-2",
                }
            )
        return _Request(
            {
                "items": [
                    {
                        "id": "visit-calendar",
                        "summary": "訪問薬剤管理",
                        "timeZone": "Asia/Tokyo",
                    }
                ]
            }
        )


class _CalendarService(_Service):
    def __init__(self):
        super().__init__()
        self.calendar_list_api = _CalendarList()

    def calendarList(self) -> _CalendarList:
        return self.calendar_list_api


class _GoneError(Exception):
    class _Response:
        status = 410

    resp = _Response()


class _GoneEvents:
    def list(self, **kwargs: Any) -> _Request:
        raise _GoneError("sync token expired")


class _GoneService:
    def events(self) -> _GoneEvents:
        return _GoneEvents()


def test_google_provider_paginates_and_normalizes_event():
    from finance_core.integrations.google_calendar import GoogleCalendarProvider

    service = _Service()
    provider = GoogleCalendarProvider(service=service)

    result = provider.list_events(
        calendar_id="primary",
        time_min="2026-08-20T00:00:00Z",
        time_max="2026-08-21T00:00:00Z",
    )

    assert result["next_sync_token"] == "sync-1"
    assert result["events"] == [
        {
            "calendar_id": "primary",
            "external_event_id": "event-1",
            "summary": "Support session",
            "starts_at": "2026-08-20T10:00:00+09:00",
            "ends_at": "2026-08-20T12:00:00+09:00",
            "timezone": "Asia/Tokyo",
            "status": "CONFIRMED",
            "etag": "etag-1",
            "is_all_day": False,
            "recurring_event_id": None,
            "occurrence_start": None,
        }
    ]
    assert service.events_api.calls[1]["pageToken"] == "page-2"
    assert service.events_api.calls[0]["showDeleted"] is True


def test_google_provider_resolves_the_exact_invoice_calendar_name():
    from finance_core.integrations.google_calendar import GoogleCalendarProvider

    service = _CalendarService()
    provider = GoogleCalendarProvider(service=service)

    assert provider.resolve_calendar_id("訪問薬剤管理") == "visit-calendar"
    assert service.calendar_list_api.calls[1]["pageToken"] == "calendar-page-2"


def test_google_provider_requires_full_resync_after_expired_cursor():
    import pytest

    from finance_core.integrations.calendar import CalendarSyncResetRequired
    from finance_core.integrations.google_calendar import GoogleCalendarProvider

    with pytest.raises(CalendarSyncResetRequired):
        GoogleCalendarProvider(service=_GoneService()).list_events(
            calendar_id="visit-calendar", sync_token="expired"
        )
