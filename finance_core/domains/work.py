"""Work-entry and timesheet service boundary."""

from __future__ import annotations

import uuid
import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, TYPE_CHECKING

from finance_core.errors import FinanceConflictError, FinanceValidationError

if TYPE_CHECKING:
    from finance_core.core import FinanceCore


class WorkService:
    """Keep operational work records separate from invoice documents."""

    def __init__(self, core: FinanceCore):
        self.core = core

    @staticmethod
    def _text(value: Any, *, field: str, required: bool = False, limit: int = 1000) -> str:
        result = str(value or "").strip()
        if required and not result:
            raise FinanceValidationError(f"{field} is required")
        if len(result) > limit:
            raise FinanceValidationError(f"{field} is too long")
        return result

    def record(
        self,
        *,
        customer_id: str,
        contract_id: str,
        billing_rule_id: str,
        service_date: str,
        quantity: str,
        source_id: str,
        created_by: str,
        description: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        actor = self._text(created_by, field="created_by", required=True, limit=200)
        record = self._build_record(
            customer_id=customer_id,
            contract_id=contract_id,
            billing_rule_id=billing_rule_id,
            service_date=service_date,
            quantity=quantity,
            source_id=source_id,
            actor=actor,
            description=description,
            project_id=project_id,
        )
        self.core.approvals.assert_period_open(record["service_period"], operation="work entry")
        stored = self.core.store.put_work_entry(record)
        self.core.store.record_audit(
            "work_entry.create",
            actor=actor,
            detail={"work_entry_id": stored["id"], "customer_id": customer_id},
        )
        return stored

    def record_from_schedule(self, schedule_id: str, *, created_by: str) -> dict[str, Any]:
        actor = self._text(created_by, field="created_by", required=True, limit=200)
        schedule = self.core.get_schedule(schedule_id)
        if schedule["status"] != "COMPLETED":
            raise FinanceConflictError("Only completed schedules can create work entries")
        for field in ("customer_id", "contract_id", "billing_rule_id"):
            if not schedule.get(field):
                raise FinanceConflictError("Schedule must be linked to customer, contract, and billing rule")
        source_id = f"schedule:{schedule_id}"
        existing = self.core.store.find_work_entry_by_source_id(source_id)
        if existing is not None:
            if schedule.get("work_entry_id") != existing["id"]:
                billable_status = {
                    "APPROVED": "READY_FOR_INVOICE",
                    "BILLED": "BILLED",
                }.get(existing.get("status"), "PENDING_APPROVAL")
                self.core.store.update_schedule_event(
                    schedule_id,
                    {"work_entry_id": existing["id"], "billable_status": billable_status},
                )
            return existing
        record = self._build_record(
            customer_id=schedule["customer_id"],
            contract_id=schedule["contract_id"],
            billing_rule_id=schedule["billing_rule_id"],
            service_date=str(schedule.get("actual_starts_at") or schedule["starts_at"])[:10],
            quantity=schedule["actual_quantity"],
            source_id=source_id,
            actor=actor,
            description=schedule.get("title"),
            project_id=schedule.get("project_id"),
            status="PENDING_APPROVAL",
        )
        self.core.approvals.assert_period_open(record["service_period"], operation="work entry")
        stored = self.core.store.put_work_entry(record)
        self.core.store.update_schedule_event(
            schedule_id,
            {"work_entry_id": stored["id"], "billable_status": "PENDING_APPROVAL"},
        )
        self.core.store.record_audit(
            "work_entry.from_schedule",
            actor=actor,
            detail={"work_entry_id": stored["id"], "schedule_id": schedule_id},
        )
        return stored

    def approve(self, entry_id: str, *, approved_by: str) -> dict[str, Any]:
        actor = self._text(approved_by, field="approved_by", required=True, limit=200)
        entry = self.core.store.get_work_entry(entry_id)
        if entry is None:
            raise FinanceValidationError(f"Work entry not found: {entry_id}")
        if entry.get("status") != "PENDING_APPROVAL":
            raise FinanceConflictError("Only pending work entries can be approved")
        self.core.approvals.assert_period_open(entry["service_period"], operation="work entry approval")
        updated = self.core.store.update_work_entry(
            entry_id,
            {"status": "APPROVED", "approved_by": actor, "approved_at": self.core._now()},
        )
        schedule = self.core.store.find_schedule_event_by_work_entry_id(entry_id)
        if schedule is not None:
            self.core.store.update_schedule_event(
                schedule["id"], {"billable_status": "READY_FOR_INVOICE"}
            )
        self.core.store.record_audit(
            "work_entry.approve",
            actor=actor,
            detail={"work_entry_id": entry_id},
        )
        return updated

    def reject(self, entry_id: str, *, rejected_by: str, reason: str) -> dict[str, Any]:
        actor = self._text(rejected_by, field="rejected_by", required=True, limit=200)
        entry = self.core.store.get_work_entry(entry_id)
        if entry is None:
            raise FinanceValidationError(f"Work entry not found: {entry_id}")
        if entry.get("status") != "PENDING_APPROVAL":
            raise FinanceConflictError("Only pending work entries can be rejected")
        updated = self.core.store.update_work_entry(
            entry_id,
            {"status": "REJECTED", "rejected_by": actor, "rejection_reason": self._text(reason, field="reason", required=True)},
        )
        schedule = self.core.store.find_schedule_event_by_work_entry_id(entry_id)
        if schedule is not None:
            self.core.store.update_schedule_event(
                schedule["id"], {"billable_status": "NOT_READY"}
            )
        self.core.store.record_audit(
            "work_entry.reject",
            actor=actor,
            detail={"work_entry_id": entry_id},
        )
        return updated

    def _build_record(
        self,
        *,
        customer_id: str,
        contract_id: str,
        billing_rule_id: str,
        service_date: str,
        quantity: str,
        source_id: str,
        actor: str,
        description: str | None = None,
        project_id: str | None = None,
        status: str = "UNBILLED",
    ) -> dict[str, Any]:
        customer = self.core._customer(customer_id)
        contract = self.core.get_contract(contract_id)
        rule = self.core.get_billing_rule(billing_rule_id)
        if contract["customer_id"] != customer_id or rule["contract_id"] != contract_id:
            raise FinanceConflictError("Work entry master data does not match")
        if customer.get("active_status") != "ACTIVE":
            raise FinanceConflictError("Inactive customers cannot receive work entries")
        try:
            normalized_date = date.fromisoformat(str(service_date)).isoformat()
        except (TypeError, ValueError) as exc:
            raise FinanceValidationError("service_date must use YYYY-MM-DD") from exc
        try:
            amount = Decimal(str(quantity))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise FinanceValidationError("quantity must be a valid decimal") from exc
        if not amount.is_finite() or amount <= 0:
            raise FinanceValidationError("quantity must be greater than zero")
        source = self._text(source_id, field="source_id", required=True, limit=300)
        return {
            "id": f"work_{uuid.uuid4().hex}",
            "source_id": source,
            "customer_id": customer_id,
            "contract_id": contract_id,
            "billing_rule_id": billing_rule_id,
            "service_date": normalized_date,
            "service_period": normalized_date[:7],
            "quantity": format(amount, "f"),
            "unit": rule["unit"],
            "description": self._text(description or rule["description"], field="description", required=True),
            "project_id": self._text(project_id, field="project_id", limit=200) or contract.get("project_id"),
            "status": status,
            "billed_invoice_id": None,
            "created_by": actor,
        }

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        return {
            key: record[key]
            for key in (
                "id",
                "source_id",
                "customer_id",
                "contract_id",
                "billing_rule_id",
                "service_date",
                "service_period",
                "quantity",
                "unit",
                "project_id",
                "status",
                "billed_invoice_id",
            )
            if key in record
        }

    def import_csv(
        self,
        *,
        csv_text: str,
        apply: bool = False,
        imported_by: str | None = None,
    ) -> dict[str, Any]:
        """Preview or atomically import operational work entries from CSV."""

        try:
            rows = self._parse_csv(csv_text)
            actor = self._text(imported_by, field="imported_by", required=True, limit=200) if apply else "preview"
            existing_sources = {
                str(row.get("source_id"))
                for row in self.core.store.list_work_entries()
            }
            records: list[dict[str, Any]] = []
            seen_sources: set[str] = set()
            for row in rows:
                source_id = str(row["source_id"])
                if source_id in seen_sources or source_id in existing_sources:
                    raise FinanceConflictError("A work entry with the same source_id already exists")
                seen_sources.add(source_id)
                records.append(
                    self._build_record(
                        customer_id=row["customer_id"],
                        contract_id=row["contract_id"],
                        billing_rule_id=row["billing_rule_id"],
                        service_date=row["service_date"],
                        quantity=row["quantity"],
                        source_id=source_id,
                        actor=actor,
                        description=row.get("description"),
                        project_id=row.get("project_id"),
                    )
                )

            if not apply:
                return {
                    "success": True,
                    "dry_run": True,
                    "rows": [self._public(record) | {"status": "ready"} for record in records],
                    "applied_count": 0,
                }

            for record in records:
                self.core.approvals.assert_period_open(record["service_period"], operation="work entry import")
            stored = self.core.store.put_work_entries_batch(records)
            self.core.store.record_audit(
                "work_entry.import",
                actor=actor,
                detail={"count": len(stored)},
            )
            return {
                "success": True,
                "dry_run": False,
                "applied_count": len(stored),
                "work_entries": [self._public(record) for record in stored],
            }
        except (FinanceConflictError, FinanceValidationError) as exc:
            return {"success": False, "errors": [{"error": str(exc)}]}

    @staticmethod
    def _parse_csv(csv_text: str) -> list[dict[str, str]]:
        if not isinstance(csv_text, str):
            raise FinanceValidationError("csv_text must be text")
        if not csv_text.strip():
            raise FinanceValidationError("csv_text is empty")
        if len(csv_text.encode("utf-8")) > 2_000_000:
            raise FinanceValidationError("csv_text is too large")
        if "\x00" in csv_text:
            raise FinanceValidationError("csv_text contains an invalid character")
        try:
            reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff"), newline=""), strict=True)
            raw_headers = reader.fieldnames
            if not raw_headers:
                raise FinanceValidationError("CSV header is required")
            headers = [str(header or "").strip().lower() for header in raw_headers]
            if any(not header for header in headers) or len(headers) != len(set(headers)):
                raise FinanceValidationError("CSV headers must be unique and non-empty")
            required = {
                "source_id",
                "customer_id",
                "contract_id",
                "billing_rule_id",
                "service_date",
                "quantity",
            }
            missing = sorted(required - set(headers))
            if missing:
                raise FinanceValidationError(f"CSV is missing required headers: {', '.join(missing)}")

            rows: list[dict[str, str]] = []
            for row_number, raw_row in enumerate(reader, start=2):
                if row_number > 10001:
                    raise FinanceValidationError("CSV contains too many rows")
                if None in raw_row:
                    raise FinanceValidationError(f"row {row_number} has too many columns")
                values = {
                    headers[index]: str(raw_row.get(raw_headers[index]) or "").strip()
                    for index in range(len(headers))
                }
                if not any(values.values()):
                    continue
                rows.append(values)
            if not rows:
                raise FinanceValidationError("CSV must contain at least one work entry row")
            return rows
        except csv.Error as exc:
            raise FinanceValidationError("CSV could not be parsed") from exc

    def list(self, **filters: Any) -> list[dict[str, Any]]:
        customer_id = filters.get("customer_id")
        if customer_id:
            self.core._customer(customer_id)
        return self.core.store.list_work_entries(**filters)

    def draft_from_entries(
        self,
        *,
        issuer_id: str,
        billing_rule_id: str,
        service_period: str,
        issue_date: str,
        billing_key: str,
        created_by: str,
        due_date: str | None = None,
        template_id: str | None = None,
        template_version: str = "1.0.0",
        notes: str | None = None,
        eligible_entry_ids: set[str] | None = None,
        approved_only: bool = False,
        line_source_id: str | None = None,
        generated_by: str = "work_entry",
    ) -> dict[str, Any]:
        existing = self.core.store.find_invoice_by_billing_key(billing_key)
        if existing is not None:
            return existing
        period = self._text(service_period, field="service_period", required=True)
        rule = self.core.get_billing_rule(billing_rule_id)
        entries = self.core.store.list_work_entries(
            billing_rule_id=billing_rule_id,
            service_period=period,
            unbilled_only=True,
        )
        entries = [entry for entry in entries if entry.get("status") in {"UNBILLED", "APPROVED"}]
        if eligible_entry_ids is not None:
            entries = [entry for entry in entries if entry["id"] in eligible_entry_ids]
        if approved_only:
            entries = [entry for entry in entries if entry.get("status") == "APPROVED"]
        if not entries:
            raise FinanceConflictError("No unbilled work entries exist for this period")
        quantity = sum((Decimal(str(row["quantity"])) for row in entries), Decimal("0"))
        entry_ids = [row["id"] for row in entries]
        draft = self.core.draft_from_billing_rule(
            issuer_id=issuer_id,
            billing_rule_id=billing_rule_id,
            service_period=period,
            issue_date=issue_date,
            quantity=format(quantity, "f"),
            due_date=due_date,
            template_id=template_id,
            template_version=template_version,
            notes=notes,
            billing_key=billing_key,
            created_by=created_by,
            line_source_type="work_entry_batch",
            line_source_id=line_source_id or f"work:{billing_rule_id}:{period}",
            generated_by=generated_by,
            work_entry_ids=entry_ids,
        )
        self.core.store.mark_work_entries_billed(entry_ids, invoice_id=draft["id"])
        if eligible_entry_ids is not None:
            for schedule in self.core.store.list_schedule_events():
                if schedule.get("work_entry_id") in entry_ids:
                    self.core.store.update_schedule_event(
                        schedule["id"], {"billable_status": "BILLED"}
                    )
        return draft

    def draft_from_schedule_entries(
        self,
        *,
        issuer_id: str,
        calendar_connection_id: str,
        billing_rule_id: str,
        service_period: str,
        issue_date: str,
        billing_key: str,
        created_by: str,
        due_date: str | None = None,
        template_id: str | None = None,
        template_version: str = "1.0.0",
        notes: str | None = None,
    ) -> dict[str, Any]:
        # ponytail: billing is limited to approved schedule work; auto-issue and
        # auto-send remain a later workflow after invoice approval exists.
        connection = self.core.get_calendar_connection(calendar_connection_id)
        self.core.schedules.assert_invoice_calendar(connection)
        eligible_entry_ids = {
            str(schedule["work_entry_id"])
            for schedule in self.core.store.list_schedule_events()
            if schedule.get("calendar_connection_id") == calendar_connection_id
            and schedule.get("status") == "COMPLETED"
            and schedule.get("billing_rule_id") == billing_rule_id
            and schedule.get("work_entry_id")
        }
        if not eligible_entry_ids:
            raise FinanceConflictError("No completed calendar work entries exist for this period")
        return self.draft_from_entries(
            issuer_id=issuer_id,
            billing_rule_id=billing_rule_id,
            service_period=service_period,
            issue_date=issue_date,
            billing_key=billing_key,
            created_by=created_by,
            due_date=due_date,
            template_id=template_id,
            template_version=template_version,
            notes=notes,
            eligible_entry_ids=eligible_entry_ids,
            approved_only=True,
        )
