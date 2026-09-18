"""Monthly closing and immutable schedule-to-invoice target selection."""

from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Any, TYPE_CHECKING

from finance_core.errors import FinanceConflictError, FinanceNotFoundError, FinanceValidationError

if TYPE_CHECKING:
    from finance_core.core import FinanceCore


_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_SUMMARY_KEYS = (
    "eligible",
    "unmapped",
    "pending_completion",
    "awaiting_approval",
    "cancelled",
    "already_billed",
    "different_billing_rule",
)


def _text(value: Any, *, field: str, required: bool = False, limit: int = 500) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise FinanceValidationError(f"{field} is required")
    if len(result) > limit:
        raise FinanceValidationError(f"{field} is too long")
    return result


def _period(value: Any) -> str:
    normalized = _text(value, field="service_period", required=True, limit=7)
    if not _PERIOD_RE.fullmatch(normalized):
        raise FinanceValidationError("service_period must use YYYY-MM")
    date.fromisoformat(f"{normalized}-01")
    return normalized


class BillingRunService:
    """Close a reviewed schedule set before invoice drafting."""

    def __init__(self, core: FinanceCore):
        self.core = core

    def preview(
        self,
        *,
        calendar_connection_id: str,
        billing_rule_id: str,
        service_period: str,
    ) -> dict[str, Any]:
        connection, rule, period = self._scope(
            calendar_connection_id, billing_rule_id, service_period
        )
        schedule_ids = {key: [] for key in _SUMMARY_KEYS}
        eligible_work_entry_ids: list[str] = []
        for schedule in self.core.store.list_schedule_events():
            if schedule.get("calendar_connection_id") != connection["id"]:
                continue
            if self._schedule_period(schedule) != period:
                continue
            category, work_entry_id = self._classify(schedule, rule_id=rule["id"])
            schedule_ids[category].append(schedule["id"])
            if category == "eligible" and work_entry_id:
                eligible_work_entry_ids.append(work_entry_id)
        summary = {key: len(schedule_ids[key]) for key in _SUMMARY_KEYS}
        return {
            "calendar_connection_id": connection["id"],
            "billing_rule_id": rule["id"],
            "service_period": period,
            "schedule_ids": schedule_ids,
            "eligible_work_entry_ids": eligible_work_entry_ids,
            "summary": summary,
        }

    def finalize(
        self,
        *,
        calendar_connection_id: str,
        billing_rule_id: str,
        service_period: str,
        schedule_ids: list[str],
        finalized_by: str,
    ) -> dict[str, Any]:
        actor = _text(finalized_by, field="finalized_by", required=True, limit=200)
        connection, rule, period = self._scope(
            calendar_connection_id, billing_rule_id, service_period
        )
        normalized_ids = self._schedule_ids(schedule_ids)
        existing = self.core.store.find_billing_run_by_scope(
            calendar_connection_id=connection["id"],
            billing_rule_id=rule["id"],
            service_period=period,
        )
        if existing is not None:
            if set(existing.get("schedule_ids") or []) == set(normalized_ids):
                return existing
            raise FinanceConflictError(
                "A billing run already exists for this calendar, rule, and period"
            )

        preview = self.preview(
            calendar_connection_id=connection["id"],
            billing_rule_id=rule["id"],
            service_period=period,
        )
        eligible_ids = set(preview["schedule_ids"]["eligible"])
        if not set(normalized_ids).issubset(eligible_ids):
            raise FinanceConflictError(
                "All schedule_ids must refer to eligible schedule records"
            )
        ordered_ids = [
            schedule_id
            for schedule_id in preview["schedule_ids"]["eligible"]
            if schedule_id in set(normalized_ids)
        ]
        if not ordered_ids:
            raise FinanceConflictError("At least one eligible schedule is required")
        snapshots: list[dict[str, Any]] = []
        work_entry_ids: list[str] = []
        for schedule_id in ordered_ids:
            schedule = self.core.get_schedule(schedule_id)
            work_entry_id = _text(
                schedule.get("work_entry_id"),
                field="work_entry_id",
                required=True,
                limit=200,
            )
            work_entry_ids.append(work_entry_id)
            snapshots.append(
                {
                    "schedule_id": schedule["id"],
                    "work_entry_id": work_entry_id,
                    "actual_starts_at": schedule.get("actual_starts_at"),
                    "actual_ends_at": schedule.get("actual_ends_at"),
                    "actual_duration_seconds": schedule.get("actual_duration_seconds"),
                    "actual_quantity": schedule.get("actual_quantity"),
                    "external_event_id": schedule.get("external_event_id"),
                    "occurrence_start": schedule.get("occurrence_start"),
                    "source_id": schedule.get("source_id"),
                }
            )
        billing_key = f"schedule-run:{connection['id']}:{rule['id']}:{period}"
        record = {
            "id": f"brun_{uuid.uuid4().hex}",
            "calendar_connection_id": connection["id"],
            "billing_rule_id": rule["id"],
            "service_period": period,
            "billing_key": billing_key,
            "status": "FINALIZED",
            "schedule_ids": ordered_ids,
            "work_entry_ids": work_entry_ids,
            "schedule_snapshots": snapshots,
            "finalized_by": actor,
            "finalized_at": self.core._now(),
            "invoice_id": None,
        }
        stored = self.core.store.put_billing_run(record)
        self.core.store.record_audit(
            "billing_run.finalize",
            actor=actor,
            detail={
                "billing_run_id": stored["id"],
                "calendar_connection_id": connection["id"],
                "billing_rule_id": rule["id"],
                "service_period": period,
                "schedule_count": len(ordered_ids),
            },
        )
        return stored

    def get(self, run_id: str) -> dict[str, Any]:
        record = self.core.store.get_billing_run(run_id)
        if record is None:
            raise FinanceNotFoundError(f"Billing run not found: {run_id}")
        return record

    def list(
        self,
        *,
        calendar_connection_id: str | None = None,
        service_period: str | None = None,
    ) -> list[dict[str, Any]]:
        if calendar_connection_id:
            self.core.get_calendar_connection(calendar_connection_id)
        normalized_period = _period(service_period) if service_period else None
        return self.core.store.list_billing_runs(
            calendar_connection_id=calendar_connection_id,
            service_period=normalized_period,
        )

    def draft_invoice(
        self,
        run_id: str,
        *,
        issuer_id: str,
        issue_date: str,
        created_by: str,
        due_date: str | None = None,
        template_id: str | None = None,
        template_version: str = "1.0.0",
        notes: str | None = None,
    ) -> dict[str, Any]:
        actor = _text(created_by, field="created_by", required=True, limit=200)
        run = self.get(run_id)
        if run.get("status") == "DRAFT_CREATED" and run.get("invoice_id"):
            invoice = self.core.get_invoice(run["invoice_id"])
            if invoice is not None:
                return invoice
            raise FinanceConflictError("Billing run references a missing invoice")
        if run.get("status") != "FINALIZED":
            raise FinanceConflictError("Only finalized billing runs can create invoice drafts")
        entry_ids = [str(entry_id) for entry_id in run.get("work_entry_ids") or []]
        if not entry_ids:
            raise FinanceConflictError("Billing run has no work entries")
        existing_invoice = self.core.store.find_invoice_by_billing_key(run["billing_key"])
        if existing_invoice is not None and set(existing_invoice.get("work_entry_ids") or []) != set(entry_ids):
            raise FinanceConflictError(
                "The billing key is already used by a different work-entry set"
            )
        for snapshot in run.get("schedule_snapshots") or []:
            schedule = self.core.get_schedule(snapshot["schedule_id"])
            if (
                schedule.get("status") != "COMPLETED"
                or schedule.get("work_entry_id") != snapshot["work_entry_id"]
                or schedule.get("actual_quantity") != snapshot.get("actual_quantity")
            ):
                raise FinanceConflictError(
                    "Billing run no longer matches its finalized schedule snapshot"
                )
        for entry_id in entry_ids:
            entry = self.core.store.get_work_entry(entry_id)
            if entry is None:
                raise FinanceNotFoundError(f"Work entry not found: {entry_id}")
            if entry.get("status") != "APPROVED":
                raise FinanceConflictError(
                    "All finalized work entries must remain approved before drafting"
                )
            if entry.get("billed_invoice_id") not in (None, ""):
                raise FinanceConflictError(f"Work entry is already billed: {entry_id}")
        draft = self.core.work.draft_from_entries(
            issuer_id=issuer_id,
            billing_rule_id=run["billing_rule_id"],
            service_period=run["service_period"],
            issue_date=issue_date,
            billing_key=run["billing_key"],
            created_by=actor,
            due_date=due_date,
            template_id=template_id,
            template_version=template_version,
            notes=notes,
            eligible_entry_ids=set(entry_ids),
            approved_only=True,
            line_source_id=f"billing_run:{run['id']}",
            generated_by="schedule_billing_run",
        )
        self._reconcile_billed_targets(run, draft["id"])
        updated = self.core.store.update_billing_run(
            run_id,
            {
                "status": "DRAFT_CREATED",
                "invoice_id": draft["id"],
                "draft_created_by": actor,
                "draft_created_at": self.core._now(),
            },
        )
        self.core.store.record_audit(
            "billing_run.draft_create",
            actor=actor,
            detail={"billing_run_id": run_id, "invoice_id": draft["id"]},
        )
        return self.core.get_invoice(updated["invoice_id"])

    def _scope(
        self,
        calendar_connection_id: str,
        billing_rule_id: str,
        service_period: str,
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        connection = self.core.get_calendar_connection(calendar_connection_id)
        self.core.schedules.assert_invoice_calendar(connection)
        rule = self.core.get_billing_rule(billing_rule_id)
        if str(rule.get("rule_type") or "").upper() != "HOURLY":
            raise FinanceConflictError("Schedule billing runs require an HOURLY billing rule")
        contract = self.core.get_contract(rule["contract_id"])
        if rule.get("active_status") != "ACTIVE" or contract.get("active_status") != "ACTIVE":
            raise FinanceConflictError("Only active contracts and billing rules can be closed")
        period = _period(service_period)
        return connection, rule, period

    @staticmethod
    def _schedule_period(schedule: dict[str, Any]) -> str:
        service_time = schedule.get("actual_starts_at") or schedule.get("starts_at") or ""
        return str(service_time)[:7]

    def _classify(
        self,
        schedule: dict[str, Any],
        *,
        rule_id: str,
    ) -> tuple[str, str | None]:
        if schedule.get("status") in {"CANCELLED", "NO_SHOW"}:
            return "cancelled", None
        if not all(schedule.get(field) for field in ("customer_id", "contract_id", "billing_rule_id")):
            return "unmapped", None
        if schedule.get("billing_rule_id") != rule_id:
            return "different_billing_rule", schedule.get("work_entry_id")
        if schedule.get("status") != "COMPLETED":
            return "pending_completion", None
        if schedule.get("billable_status") == "BILLED":
            return "already_billed", schedule.get("work_entry_id")
        work_entry_id = schedule.get("work_entry_id")
        if not work_entry_id:
            return "awaiting_approval", None
        work_entry = self.core.store.get_work_entry(work_entry_id)
        if work_entry is None or work_entry.get("status") != "APPROVED":
            return "awaiting_approval", str(work_entry_id)
        if work_entry.get("billed_invoice_id") not in (None, ""):
            return "already_billed", str(work_entry_id)
        return "eligible", str(work_entry_id)

    @staticmethod
    def _schedule_ids(schedule_ids: list[str]) -> list[str]:
        if not isinstance(schedule_ids, list):
            raise FinanceValidationError("schedule_ids must be a list")
        normalized: list[str] = []
        seen: set[str] = set()
        for value in schedule_ids:
            schedule_id = _text(value, field="schedule_id", required=True, limit=200)
            if schedule_id in seen:
                raise FinanceValidationError("schedule_ids must not contain duplicates")
            seen.add(schedule_id)
            normalized.append(schedule_id)
        if not normalized:
            raise FinanceValidationError("schedule_ids must contain at least one item")
        return normalized

    def _reconcile_billed_targets(self, run: dict[str, Any], invoice_id: str) -> None:
        unbilled_ids: list[str] = []
        for entry_id in run.get("work_entry_ids") or []:
            entry = self.core.store.get_work_entry(entry_id)
            if entry is None:
                raise FinanceNotFoundError(f"Work entry not found: {entry_id}")
            billed_invoice_id = entry.get("billed_invoice_id")
            if billed_invoice_id in (None, ""):
                unbilled_ids.append(entry_id)
            elif billed_invoice_id != invoice_id:
                raise FinanceConflictError(f"Work entry is already billed: {entry_id}")
        if unbilled_ids:
            self.core.store.mark_work_entries_billed(unbilled_ids, invoice_id=invoice_id)
        for schedule_id in run.get("schedule_ids") or []:
            self.core.store.update_schedule_event(
                schedule_id,
                {"billable_status": "BILLED"},
            )
