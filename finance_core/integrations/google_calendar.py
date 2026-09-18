"""Google Calendar v3 adapter with bounded, normalized event output."""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path
from typing import Any

from finance_core.errors import FinanceAuthorizationError
from finance_core.integrations.calendar import (
    CalendarProviderError,
    CalendarSyncResetRequired,
)


def _fold_calendar_name(value: str) -> str:
    """Normalize Unicode and whitespace for provider-side name resolution."""

    return "".join(unicodedata.normalize("NFKC", str(value or "")).split())


def _event_time(value: dict[str, Any]) -> tuple[str | None, str | None, bool]:
    if not isinstance(value, dict):
        return None, None, False
    if value.get("dateTime"):
        return str(value["dateTime"]), str(value.get("timeZone") or "") or None, False
    if value.get("date"):
        return f"{value['date']}T00:00:00+00:00", str(value.get("timeZone") or "UTC"), True
    return None, None, False


def _normalize_event(calendar_id: str, event: dict[str, Any]) -> dict[str, Any]:
    starts_at, start_timezone, is_all_day = _event_time(event.get("start") or {})
    ends_at, end_timezone, _ = _event_time(event.get("end") or {})
    original_start, _, _ = _event_time(event.get("originalStartTime") or {})
    return {
        "calendar_id": calendar_id,
        "external_event_id": str(event.get("id") or ""),
        "summary": str(event.get("summary") or "(no title)")[:500],
        "starts_at": starts_at,
        "ends_at": ends_at,
        "timezone": start_timezone or end_timezone or "UTC",
        "status": "CANCELLED" if event.get("status") == "cancelled" else "CONFIRMED",
        "etag": str(event.get("etag") or "") or None,
        "is_all_day": is_all_day,
        "recurring_event_id": str(event.get("recurringEventId") or "") or None,
        "occurrence_start": original_start,
    }


class GoogleCalendarProvider:
    """Use an injected Google service in tests and lazy-load it in production."""

    # ponytail: read-only sync is the current ceiling; add write-back only
    # behind a separate approval and idempotency contract.

    def __init__(self, *, service: Any | None = None, token_path: str | Path | None = None):
        self._service = service
        self.token_path = Path(token_path) if token_path else None

    def _service_or_build(self) -> Any:
        if self._service is not None:
            return self._service
        if self.token_path is None or not self.token_path.is_file():
            raise FinanceAuthorizationError("Google Calendar OAuth token is not configured")
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            credentials = Credentials.from_authorized_user_file(str(self.token_path))
            if credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
                self.token_path.write_text(credentials.to_json(), encoding="utf-8")
                os.chmod(self.token_path.parent, 0o700)
                os.chmod(self.token_path, 0o600)
            if not credentials.valid:
                raise FinanceAuthorizationError("Google Calendar OAuth token is invalid")
            self._service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
            return self._service
        except FinanceAuthorizationError:
            raise
        except Exception as exc:
            raise CalendarProviderError("Google Calendar client could not be initialized") from exc

    def list_events(
        self,
        *,
        calendar_id: str,
        time_min: str | None = None,
        time_max: str | None = None,
        sync_token: str | None = None,
    ) -> dict[str, Any]:
        service = self._service_or_build()
        params: dict[str, Any] = {
            "calendarId": calendar_id,
            "singleEvents": True,
            "showDeleted": True,
            "maxResults": 2500,
        }
        if sync_token:
            params["syncToken"] = sync_token
        else:
            if time_min:
                params["timeMin"] = time_min
            if time_max:
                params["timeMax"] = time_max
            params["orderBy"] = "startTime"

        events: list[dict[str, Any]] = []
        page_token: str | None = None
        next_sync_token: str | None = None
        while True:
            request_params = dict(params)
            if page_token:
                request_params["pageToken"] = page_token
            try:
                response = service.events().list(**request_params).execute()
            except Exception as exc:
                status = getattr(getattr(exc, "resp", None), "status", None)
                if status == 410:
                    raise CalendarSyncResetRequired(
                        "Google Calendar sync cursor expired; a full resync is required"
                    ) from exc
                raise CalendarProviderError("Google Calendar event listing failed") from exc
            for event in response.get("items", []):
                normalized = _normalize_event(calendar_id, event)
                if normalized["external_event_id"]:
                    events.append(normalized)
            page_token = response.get("nextPageToken")
            next_sync_token = response.get("nextSyncToken") or next_sync_token
            if not page_token:
                break
        return {"events": events, "next_sync_token": next_sync_token}

    def list_calendars(self) -> list[dict[str, Any]]:
        """Return bounded calendar metadata used to select the invoice calendar."""

        service = self._service_or_build()
        calendars: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"maxResults": 250}
            if page_token:
                params["pageToken"] = page_token
            try:
                response = service.calendarList().list(**params).execute()
            except Exception as exc:
                raise CalendarProviderError("Google Calendar list request failed") from exc
            for item in response.get("items", []):
                calendar_id = str(item.get("id") or "")
                calendar_name = str(item.get("summaryOverride") or item.get("summary") or "")[:500]
                if calendar_id and calendar_name:
                    calendars.append(
                        {
                            "calendar_id": calendar_id,
                            "calendar_name": calendar_name,
                            "timezone": str(item.get("timeZone") or "UTC"),
                            "primary": bool(item.get("primary")),
                        }
                    )
            page_token = response.get("nextPageToken")
            if not page_token:
                return calendars

    def resolve_calendar_id(self, calendar_name: str) -> str:
        """Resolve one exact calendar name; reject missing or ambiguous matches."""

        target = str(calendar_name or "").strip()
        if not target:
            raise CalendarProviderError("Calendar name is required")
        calendars = self.list_calendars()
        exact_matches = [
            calendar
            for calendar in calendars
            if calendar["calendar_name"] == target
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]["calendar_id"]
        if len(exact_matches) > 1:
            raise CalendarProviderError(f"Calendar name is ambiguous: {target}")

        normalized_matches = [
            calendar
            for calendar in calendars
            if _fold_calendar_name(calendar["calendar_name"]) == _fold_calendar_name(target)
        ]
        if not normalized_matches:
            raise CalendarProviderError(f"Calendar not found: {target}")
        if len(normalized_matches) > 1:
            raise CalendarProviderError(f"Calendar name is ambiguous: {target}")
        return normalized_matches[0]["calendar_id"]
