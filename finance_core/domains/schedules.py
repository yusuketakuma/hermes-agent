"""Business schedule lifecycle and calendar-event synchronization."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, TYPE_CHECKING

from finance_core.errors import FinanceConflictError, FinanceNotFoundError, FinanceValidationError
from finance_core.integrations.calendar import CalendarProvider

if TYPE_CHECKING:
    from finance_core.core import FinanceCore


_STATUSES = frozenset(
    {"PLANNED", "CONFIRMED", "IN_PROGRESS", "COMPLETED", "CANCELLED", "NO_SHOW"}
)
_ACTIVE_STATUSES = frozenset({"PLANNED", "CONFIRMED", "IN_PROGRESS"})
_TERMINAL_STATUSES = frozenset({"COMPLETED", "CANCELLED", "NO_SHOW"})
INVOICE_CALENDAR_NAME = "訪問薬剤管理"


def _text(value: Any, *, field: str, required: bool = False, limit: int = 500) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise FinanceValidationError(f"{field} is required")
    if len(result) > limit:
        raise FinanceValidationError(f"{field} is too long")
    return result


def _decimal(value: Any, *, field: str, required: bool = False) -> str | None:
    if value in (None, "") and not required:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FinanceValidationError(f"{field} must be a valid decimal") from exc
    if not amount.is_finite() or amount < 0:
        raise FinanceValidationError(f"{field} must be non-negative")
    return format(amount, "f")


def _datetime(value: Any, *, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise FinanceValidationError(f"{field} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise FinanceValidationError(f"{field} must include a timezone")
    return parsed.isoformat()


def _actual_interval(
    starts_at: Any,
    ends_at: Any,
) -> tuple[str, str, str, str]:
    """Normalize an actual interval and return seconds plus billable hours."""

    normalized_start = _datetime(starts_at, field="actual_starts_at")
    normalized_end = _datetime(ends_at, field="actual_ends_at")
    actual_start = datetime.fromisoformat(normalized_start)
    actual_end = datetime.fromisoformat(normalized_end)
    if actual_end <= actual_start:
        raise FinanceValidationError("actual_ends_at must be later than actual_starts_at")
    duration = actual_end - actual_start
    duration_seconds = (
        Decimal(duration.days * 86400 + duration.seconds)
        + (Decimal(duration.microseconds) / Decimal("1000000"))
    )
    return (
        normalized_start,
        normalized_end,
        format(duration_seconds, "f"),
        format(duration_seconds / Decimal("3600"), "f"),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScheduleService:
    """Keep planned work separate from actual work entries and invoices."""

    def __init__(self, core: FinanceCore, *, calendar_provider: CalendarProvider | None = None):
        self.core = core
        self.calendar_provider = calendar_provider

    def create_connection(
        self,
        *,
        provider: str,
        calendar_id: str | None = None,
        calendar_name: str = INVOICE_CALENDAR_NAME,
        timezone: str,
        created_by: str,
    ) -> dict[str, Any]:
        actor = _text(created_by, field="created_by", required=True, limit=200)
        normalized_provider = _text(provider, field="provider", required=True, limit=100).lower()
        if normalized_provider != "google_calendar":
            raise FinanceValidationError("provider must be google_calendar")
        normalized_calendar_name = str(calendar_name or "")
        if not normalized_calendar_name:
            raise FinanceValidationError("calendar_name is required")
        if len(normalized_calendar_name) > 200:
            raise FinanceValidationError("calendar_name is too long")
        if normalized_calendar_name != INVOICE_CALENDAR_NAME:
            raise FinanceValidationError(
                f"calendar_name must be {INVOICE_CALENDAR_NAME}"
            )
        normalized_calendar_id = _text(calendar_id, field="calendar_id", limit=300)
        if not normalized_calendar_id:
            resolver = getattr(self.calendar_provider, "resolve_calendar_id", None)
            if resolver is None:
                raise FinanceValidationError("calendar_id is required when Google Calendar is unavailable")
            normalized_calendar_id = _text(
                resolver(INVOICE_CALENDAR_NAME), field="calendar_id", required=True, limit=300
            )
        for existing in self.core.store.list_calendar_connections():
            if (
                existing.get("provider") == normalized_provider
                and existing.get("calendar_name") == INVOICE_CALENDAR_NAME
                and existing.get("calendar_id") != normalized_calendar_id
            ):
                raise FinanceConflictError("Only one invoice calendar connection is allowed")
        record = {
            "id": f"cal_{uuid.uuid4().hex}",
            "provider": normalized_provider,
            "calendar_id": normalized_calendar_id,
            "calendar_name": normalized_calendar_name,
            "timezone": _text(timezone, field="timezone", required=True, limit=100),
            "sync_token": None,
            "status": "ACTIVE",
            "created_by": actor,
        }
        stored = self.core.store.put_calendar_connection(record)
        self.core.store.record_audit(
            "calendar.connection_create",
            actor=actor,
            detail={"connection_id": stored["id"], "provider": normalized_provider},
        )
        return stored

    @staticmethod
    def assert_invoice_calendar(connection: dict[str, Any]) -> None:
        if connection.get("calendar_name") != INVOICE_CALENDAR_NAME:
            raise FinanceConflictError(
                "Calendar connection is not the configured invoice target calendar"
            )

    def get_connection(self, connection_id: str) -> dict[str, Any]:
        record = self.core.store.get_calendar_connection(connection_id)
        if record is None:
            raise FinanceNotFoundError(f"Calendar connection not found: {connection_id}")
        return record

    def list_connections(self) -> list[dict[str, Any]]:
        return self.core.store.list_calendar_connections()

    def create(
        self,
        *,
        title: str,
        starts_at: str,
        ends_at: str,
        timezone: str,
        created_by: str,
        customer_id: str | None = None,
        contract_id: str | None = None,
        billing_rule_id: str | None = None,
        project_id: str | None = None,
        planned_quantity: str | None = None,
    ) -> dict[str, Any]:
        actor = _text(created_by, field="created_by", required=True, limit=200)
        normalized_start = _datetime(starts_at, field="starts_at")
        normalized_end = _datetime(ends_at, field="ends_at")
        if normalized_end <= normalized_start:
            raise FinanceValidationError("ends_at must be later than starts_at")
        normalized_customer, normalized_contract, normalized_rule = self._validate_mapping(
            customer_id, contract_id, billing_rule_id
        )
        record = {
            "id": f"sch_{uuid.uuid4().hex}",
            "title": _text(title, field="title", required=True),
            "starts_at": normalized_start,
            "ends_at": normalized_end,
            "timezone": _text(timezone, field="timezone", required=True, limit=100),
            "customer_id": normalized_customer,
            "contract_id": normalized_contract,
            "billing_rule_id": normalized_rule,
            "project_id": _text(project_id, field="project_id", limit=200) or None,
            "planned_quantity": _decimal(planned_quantity, field="planned_quantity"),
            "actual_quantity": None,
            "actual_starts_at": None,
            "actual_ends_at": None,
            "actual_duration_seconds": None,
            "status": "PLANNED",
            "mapping_status": "CONFIRMED" if normalized_customer else "UNMAPPED",
            "billable_status": "NOT_READY",
            "source_type": "manual",
            "source_id": None,
            "external_event_id": None,
            "occurrence_start": None,
            "work_entry_id": None,
            "created_by": actor,
        }
        stored = self.core.store.put_schedule_event(record)
        self.core.store.record_audit(
            "schedule.create",
            actor=actor,
            detail={"schedule_id": stored["id"], "customer_id": normalized_customer},
        )
        return stored

    def get(self, schedule_id: str) -> dict[str, Any]:
        record = self.core.store.get_schedule_event(schedule_id)
        if record is None:
            raise FinanceNotFoundError(f"Schedule event not found: {schedule_id}")
        return record

    def list(self, **filters: Any) -> list[dict[str, Any]]:
        customer_id = filters.get("customer_id")
        if customer_id:
            self.core._customer(customer_id)
        status = filters.get("status")
        if status and str(status).upper() not in _STATUSES:
            raise FinanceValidationError(f"Unsupported schedule status: {status}")
        return self.core.store.list_schedule_events(
            customer_id=customer_id,
            status=str(status).upper() if status else None,
            from_time=filters.get("from_time"),
            to_time=filters.get("to_time"),
        )

    def link(
        self,
        schedule_id: str,
        *,
        customer_id: str,
        contract_id: str,
        billing_rule_id: str,
        linked_by: str,
    ) -> dict[str, Any]:
        actor = _text(linked_by, field="linked_by", required=True, limit=200)
        schedule = self.get(schedule_id)
        customer, contract, rule = self._validate_mapping(customer_id, contract_id, billing_rule_id)
        if schedule["status"] in _TERMINAL_STATUSES:
            raise FinanceConflictError("Completed or cancelled schedules cannot be remapped")
        updated = self.core.store.update_schedule_event(
            schedule_id,
            {
                "customer_id": customer,
                "contract_id": contract,
                "billing_rule_id": rule,
                "mapping_status": "CONFIRMED",
                "billable_status": "NOT_READY",
            },
        )
        self.core.store.record_audit(
            "schedule.link",
            actor=actor,
            detail={
                "schedule_id": schedule_id,
                "customer_id": customer,
                "contract_id": contract,
                "billing_rule_id": rule,
            },
        )
        return updated

    def complete(
        self,
        schedule_id: str,
        *,
        actual_starts_at: str,
        actual_ends_at: str,
        completed_by: str,
    ) -> dict[str, Any]:
        # ponytail: Calendar events are not proof of service; an operator must
        # submit the actual interval before it can become billable work.
        actor = _text(completed_by, field="completed_by", required=True, limit=200)
        schedule = self.get(schedule_id)
        if schedule["status"] not in _ACTIVE_STATUSES:
            raise FinanceConflictError(
                f"Schedule {schedule_id} cannot be completed from {schedule['status']}"
            )
        if not all(
            schedule.get(field)
            for field in ("customer_id", "contract_id", "billing_rule_id")
        ):
            raise FinanceConflictError(
                "Schedule must be linked to customer, contract, and billing rule before completion"
            )
        rule = self.core.get_billing_rule(schedule["billing_rule_id"])
        if str(rule.get("rule_type") or "").upper() != "HOURLY":
            raise FinanceConflictError(
                "Actual-time completion requires an HOURLY billing rule"
            )
        (
            normalized_actual_start,
            normalized_actual_end,
            duration_seconds,
            quantity,
        ) = _actual_interval(
            actual_starts_at,
            actual_ends_at,
        )
        updated = self.core.store.update_schedule_event(
            schedule_id,
            {
                "status": "COMPLETED",
                "actual_starts_at": normalized_actual_start,
                "actual_ends_at": normalized_actual_end,
                "actual_duration_seconds": duration_seconds,
                "actual_quantity": quantity,
                "completed_by": actor,
                "completed_at": _now(),
                "billable_status": "PENDING_APPROVAL",
            },
        )
        self.core.store.record_audit(
            "schedule.complete",
            actor=actor,
            detail={"schedule_id": schedule_id},
        )
        return updated

    def cancel(self, schedule_id: str, *, cancelled_by: str, reason: str) -> dict[str, Any]:
        actor = _text(cancelled_by, field="cancelled_by", required=True, limit=200)
        schedule = self.get(schedule_id)
        if schedule["status"] == "COMPLETED":
            raise FinanceConflictError("Completed schedules require an adjustment workflow")
        updated = self.core.store.update_schedule_event(
            schedule_id,
            {
                "status": "CANCELLED",
                "cancelled_by": actor,
                "cancelled_at": _now(),
                "cancellation_reason": _text(reason, field="reason", required=True),
                "billable_status": "NOT_READY",
            },
        )
        self.core.store.record_audit(
            "schedule.cancel",
            actor=actor,
            detail={"schedule_id": schedule_id},
        )
        return updated

    def sync_events(
        self,
        connection_id: str,
        *,
        events: list[dict[str, Any]],
        next_sync_token: str | None = None,
        synced_by: str,
    ) -> dict[str, Any]:
        actor = _text(synced_by, field="synced_by", required=True, limit=200)
        connection = self.get_connection(connection_id)
        self.assert_invoice_calendar(connection)
        created_count = 0
        updated_count = 0
        cancelled_count = 0
        for event in events:
            normalized = self._normalize_external_event(event, connection)
            link = self.core.store.get_calendar_event_link(
                connection_id,
                normalized["external_event_id"],
                normalized["occurrence_start"] or "",
            )
            if link is None:
                schedule = self._schedule_from_external(normalized, connection_id=connection_id)
                self.core.store.put_schedule_event(schedule)
                self.core.store.put_calendar_event_link(
                    {
                        "id": f"clink_{uuid.uuid4().hex}",
                        "connection_id": connection_id,
                        "external_event_id": normalized["external_event_id"],
                        "occurrence_start": normalized["occurrence_start"] or "",
                        "schedule_id": schedule["id"],
                        "etag": normalized.get("etag"),
                        "event": normalized,
                        "deleted_at": _now() if normalized["status"] == "CANCELLED" else None,
                    }
                )
                created_count += 1
                if normalized["status"] == "CANCELLED":
                    cancelled_count += 1
                continue

            schedule = self.get(link["schedule_id"])
            if link.get("etag") == normalized.get("etag"):
                continue
            updates: dict[str, Any] = {"external_status": normalized["status"]}
            updates["calendar_connection_id"] = connection_id
            if schedule["status"] not in _TERMINAL_STATUSES:
                updates["title"] = normalized["summary"]
            if (
                schedule["status"] not in _TERMINAL_STATUSES
                and normalized.get("starts_at")
                and normalized.get("ends_at")
            ):
                updates.update(
                    {
                        "starts_at": normalized["starts_at"],
                        "ends_at": normalized["ends_at"],
                        "timezone": normalized["timezone"],
                    }
                )
            if normalized["status"] == "CANCELLED":
                cancelled_count += 1
                if schedule["status"] in _ACTIVE_STATUSES:
                    updates.update({"status": "CANCELLED", "billable_status": "NOT_READY"})
            elif schedule["status"] == "CANCELLED":
                updates["status"] = "CONFIRMED"
            self.core.store.update_schedule_event(schedule["id"], updates)
            self.core.store.update_calendar_event_link(
                link["id"],
                {
                    "etag": normalized.get("etag"),
                    "event": normalized,
                    "deleted_at": _now() if normalized["status"] == "CANCELLED" else None,
                },
            )
            updated_count += 1

        self.core.store.update_calendar_connection(
            connection_id,
            {"sync_token": next_sync_token, "last_synced_at": _now(), "status": "ACTIVE"},
        )
        self.core.store.record_audit(
            "calendar.sync",
            actor=actor,
            detail={
                "connection_id": connection_id,
                "created_count": created_count,
                "updated_count": updated_count,
                "cancelled_count": cancelled_count,
            },
        )
        return {
            "connection_id": connection_id,
            "created_count": created_count,
            "updated_count": updated_count,
            "cancelled_count": cancelled_count,
            "next_sync_token": next_sync_token,
        }

    def sync_from_provider(
        self,
        connection_id: str,
        *,
        synced_by: str,
        time_min: str | None = None,
        time_max: str | None = None,
    ) -> dict[str, Any]:
        if self.calendar_provider is None:
            raise FinanceConflictError("Calendar provider is not configured")
        connection = self.get_connection(connection_id)
        self.assert_invoice_calendar(connection)
        result = self.calendar_provider.list_events(
            calendar_id=connection["calendar_id"],
            time_min=time_min,
            time_max=time_max,
            sync_token=connection.get("sync_token"),
        )
        return self.sync_events(
            connection_id,
            events=list(result.get("events") or []),
            next_sync_token=result.get("next_sync_token"),
            synced_by=synced_by,
        )

    def _validate_mapping(
        self,
        customer_id: str | None,
        contract_id: str | None,
        billing_rule_id: str | None,
    ) -> tuple[str | None, str | None, str | None]:
        if not customer_id and not contract_id and not billing_rule_id:
            return None, None, None
        if not customer_id or not contract_id or not billing_rule_id:
            raise FinanceValidationError(
                "customer_id, contract_id, and billing_rule_id must be supplied together"
            )
        customer = self.core._customer(customer_id)
        contract = self.core.get_contract(contract_id)
        rule = self.core.get_billing_rule(billing_rule_id)
        if contract["customer_id"] != customer["id"] or rule["contract_id"] != contract["id"]:
            raise FinanceConflictError("Schedule master data does not match")
        if customer.get("active_status") != "ACTIVE":
            raise FinanceConflictError("Inactive customers cannot receive schedules")
        return customer["id"], contract["id"], rule["id"]

    @staticmethod
    def _normalize_external_event(
        event: dict[str, Any], connection: dict[str, Any]
    ) -> dict[str, Any]:
        event_calendar_id = _text(
            event.get("calendar_id") or connection["calendar_id"],
            field="calendar_id",
            required=True,
            limit=300,
        )
        if event_calendar_id != connection["calendar_id"]:
            raise FinanceConflictError("Event does not belong to the target calendar")
        external_id = _text(event.get("external_event_id"), field="external_event_id", required=True, limit=300)
        starts_at = event.get("starts_at")
        ends_at = event.get("ends_at")
        if event.get("status") != "CANCELLED" and (not starts_at or not ends_at):
            raise FinanceValidationError("Calendar events require starts_at and ends_at")
        if starts_at and ends_at:
            starts_at = _datetime(starts_at, field="starts_at")
            ends_at = _datetime(ends_at, field="ends_at")
            if ends_at <= starts_at:
                raise FinanceValidationError("Calendar event ends_at must be later than starts_at")
        return {
            "calendar_id": event_calendar_id,
            "external_event_id": external_id,
            "summary": _text(event.get("summary"), field="summary", limit=500) or "(no title)",
            "starts_at": starts_at,
            "ends_at": ends_at,
            "timezone": _text(event.get("timezone") or connection["timezone"], field="timezone", required=True, limit=100),
            "status": "CANCELLED" if str(event.get("status") or "").upper() == "CANCELLED" else "CONFIRMED",
            "etag": _text(event.get("etag"), field="etag", limit=300) or None,
            "is_all_day": bool(event.get("is_all_day")),
            "recurring_event_id": _text(event.get("recurring_event_id"), field="recurring_event_id", limit=300) or None,
            "occurrence_start": _text(event.get("occurrence_start"), field="occurrence_start", limit=100) or None,
        }

    @staticmethod
    def _schedule_from_external(event: dict[str, Any], *, connection_id: str) -> dict[str, Any]:
        schedule_id = f"sch_{uuid.uuid4().hex}"
        return {
            "id": schedule_id,
            "title": event["summary"],
            "starts_at": event["starts_at"] or "1970-01-01T00:00:00+00:00",
            "ends_at": event["ends_at"] or "1970-01-01T00:00:00+00:00",
            "timezone": event["timezone"],
            "customer_id": None,
            "contract_id": None,
            "billing_rule_id": None,
            "project_id": None,
            "planned_quantity": None,
            "actual_quantity": None,
            "actual_starts_at": None,
            "actual_ends_at": None,
            "actual_duration_seconds": None,
            "status": event["status"],
            "mapping_status": "UNMAPPED",
            "billable_status": "NOT_READY",
            "source_type": "google_calendar",
            "source_id": f"google:{event['calendar_id']}:{event['external_event_id']}:{event['occurrence_start'] or ''}",
            "calendar_connection_id": connection_id,
            "external_event_id": event["external_event_id"],
            "occurrence_start": event["occurrence_start"],
            "external_status": event["status"],
            "work_entry_id": None,
        }
