"""CSV preview and atomic upsert workflow for customer master data."""

from __future__ import annotations

import csv
import io
from typing import Any, TYPE_CHECKING

from finance_core.errors import FinanceValidationError

if TYPE_CHECKING:
    from finance_core.domains.customers import CustomerService


class CustomerImportService:
    """Keep bulk import parsing and batch preparation out of CustomerService."""

    def __init__(self, owner: CustomerService):
        self.owner = owner

    @staticmethod
    def _parse_csv(csv_text: str) -> list[dict[str, str]]:
        if not isinstance(csv_text, str) or not csv_text.strip():
            raise FinanceValidationError("csv_text must contain customer rows")
        if len(csv_text.encode("utf-8")) > 2_000_000 or "\x00" in csv_text:
            raise FinanceValidationError("csv_text is invalid or too large")
        try:
            reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")), strict=True)
            raw_headers = reader.fieldnames or []
            headers = [str(header or "").strip().lower() for header in raw_headers]
            required = {"customer_kind", "legal_name", "billing_address"}
            if not raw_headers or any(not header for header in headers):
                raise FinanceValidationError("CSV header is required")
            if len(headers) != len(set(headers)):
                raise FinanceValidationError("CSV headers must be unique")
            missing = sorted(required - set(headers))
            if missing:
                raise FinanceValidationError(
                    f"CSV is missing required headers: {', '.join(missing)}"
                )
            rows: list[dict[str, str]] = []
            for row_number, raw in enumerate(reader, start=2):
                if row_number > 10001:
                    raise FinanceValidationError("CSV contains too many rows")
                if None in raw:
                    raise FinanceValidationError(f"row {row_number} has too many columns")
                values = {
                    headers[index]: str(raw.get(raw_headers[index]) or "").strip()
                    for index in range(len(headers))
                }
                if not any(values.values()):
                    continue
                values["__row_number"] = str(row_number)
                rows.append(values)
            if not rows:
                raise FinanceValidationError("CSV must contain at least one customer row")
            return rows
        except csv.Error as exc:
            raise FinanceValidationError("CSV could not be parsed") from exc

    @staticmethod
    def _profile(row: dict[str, str]) -> dict[str, Any]:
        due_days = row.get("payment_due_days")
        payment_due_rule = {"days_after_issue": int(due_days)} if due_days else None
        return {
            "billing_name": row.get("billing_name") or row.get("legal_name"),
            "billing_address": row.get("billing_address"),
            "email": row.get("email"),
            "department": row.get("department"),
            "contact_name": row.get("contact_name"),
            "credit_limit": row.get("credit_limit") or None,
            "credit_currency": row.get("credit_currency") or "JPY",
            "payment_due_rule": payment_due_rule,
        }

    @staticmethod
    def _tags(row: dict[str, str]) -> list[str]:
        return [
            tag.strip()
            for tag in (row.get("tags") or "").replace(",", "|").split("|")
            if tag.strip()
        ]

    @staticmethod
    def _match(existing: list[dict[str, Any]], row: dict[str, str]) -> dict[str, Any] | None:
        return next(
            (
                customer
                for customer in existing
                if (
                    row.get("external_id")
                    and customer.get("external_id") == row.get("external_id")
                )
                or (
                    row.get("corporate_number")
                    and customer.get("corporate_number") == row.get("corporate_number")
                )
            ),
            None,
        )

    @staticmethod
    def _has_duplicate(
        match: dict[str, Any] | None,
        row: dict[str, str],
        seen_customer_ids: set[str],
        seen_external_ids: set[str],
        seen_corporate_numbers: set[str],
    ) -> bool:
        external_id = row.get("external_id") or ""
        corporate_number = row.get("corporate_number") or ""
        return bool(
            (match and match["id"] in seen_customer_ids)
            or (external_id and external_id in seen_external_ids)
            or (corporate_number and corporate_number in seen_corporate_numbers)
        )

    @staticmethod
    def _remember_identity(
        match: dict[str, Any] | None,
        row: dict[str, str],
        seen_customer_ids: set[str],
        seen_external_ids: set[str],
        seen_corporate_numbers: set[str],
    ) -> None:
        if match:
            seen_customer_ids.add(match["id"])
        if row.get("external_id"):
            seen_external_ids.add(row["external_id"])
        if row.get("corporate_number"):
            seen_corporate_numbers.add(row["corporate_number"])

    def import_csv(
        self,
        *,
        csv_text: str,
        apply: bool = False,
        upsert: bool = False,
        imported_by: str | None = None,
    ) -> dict[str, Any]:
        rows = self._parse_csv(csv_text)
        existing = self.owner.core.store.list_customers()
        new_records: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        seen_customer_ids: set[str] = set()
        seen_external_ids: set[str] = set()
        seen_corporate_numbers: set[str] = set()

        for row in rows:
            row_number = int(row["__row_number"])
            try:
                match = self._match(existing, row)
                if self._has_duplicate(
                    match,
                    row,
                    seen_customer_ids,
                    seen_external_ids,
                    seen_corporate_numbers,
                ):
                    results.append(
                        {
                            "row_number": row_number,
                            "status": "duplicate_in_csv",
                            "matched_customer_id": match["id"] if match else None,
                        }
                    )
                    continue
                self._remember_identity(
                    match,
                    row,
                    seen_customer_ids,
                    seen_external_ids,
                    seen_corporate_numbers,
                )
                record = self.owner._build_customer_record(
                    customer_kind=row["customer_kind"],
                    legal_name=row["legal_name"],
                    billing_profile=self._profile(row),
                    corporate_number=row.get("corporate_number") or None,
                    external_id=row.get("external_id") or None,
                    owner_id=row.get("owner_id") or None,
                    tags=self._tags(row),
                    current=match,
                    customer_id=match["id"] if match else None,
                    revision=int(match.get("revision", 0)) + 1 if match else 0,
                )
                if match and not upsert:
                    results.append(
                        {
                            "row_number": row_number,
                            "status": "duplicate",
                            "matched_customer_id": match["id"],
                        }
                    )
                elif match:
                    updates.append(record)
                    results.append(
                        {
                            "row_number": row_number,
                            "status": "ready_update",
                            "matched_customer_id": match["id"],
                            "customer_id": match["id"],
                        }
                    )
                else:
                    new_records.append(record)
                    results.append(
                        {
                            "row_number": row_number,
                            "status": "ready",
                            "customer_id": record["id"],
                        }
                    )
            except (FinanceValidationError, ValueError) as exc:
                errors.append({"row_number": row_number, "error": str(exc)})

        has_duplicates = any(str(row["status"]).startswith("duplicate") for row in results)
        if errors or (not upsert and has_duplicates) or (apply and has_duplicates):
            if not apply:
                return {"success": True, "dry_run": True, "rows": results, "errors": errors}
            return {
                "success": False,
                "dry_run": False,
                "rows": results,
                "errors": errors or [{"error": "duplicate customer exists"}],
            }
        if not apply:
            return {
                "success": True,
                "dry_run": True,
                "rows": results,
                "errors": errors,
                "applied_count": 0,
            }

        actor = self.owner._actor(imported_by, field="imported_by")
        counts = self.owner.core.store.import_customers_batch(
            new_records, updates, actor=actor
        )
        return {
            "success": True,
            "dry_run": False,
            "rows": results,
            "applied_count": counts["inserted_count"] + counts["updated_count"],
            "inserted_count": counts["inserted_count"],
            "updated_count": counts["updated_count"],
            "customers": [
                {
                    "row_number": row["row_number"],
                    "customer_id": row["customer_id"],
                    "action": "updated" if row["status"] == "ready_update" else "created",
                }
                for row in results
                if row["status"] in {"ready", "ready_update"}
            ],
        }
