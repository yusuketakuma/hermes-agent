"""Bank import, matching, allocation, and settlement services."""

from __future__ import annotations

import csv
import io
import re
import unicodedata
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, TYPE_CHECKING

from finance_core.errors import (
    FinanceAuthorizationError,
    FinanceConflictError,
    FinanceNotFoundError,
    FinanceValidationError,
)

if TYPE_CHECKING:
    from finance_core.core import FinanceCore


def _text(value: Any, *, field: str, required: bool = False, limit: int = 1000) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise FinanceValidationError(f"{field} is required")
    if len(result) > limit:
        raise FinanceValidationError(f"{field} is too long")
    return result


def _date(value: Any, *, field: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise FinanceValidationError(f"{field} must use YYYY-MM-DD") from exc


def _money(value: Any, *, field: str) -> Decimal:
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FinanceValidationError(f"{field} must be a valid decimal") from exc
    if not amount.is_finite() or amount <= 0:
        raise FinanceValidationError(f"{field} must be greater than zero")
    return amount


def _money_text(value: Decimal) -> str:
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _payment_alias(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(normalized.casefold().split())


class PaymentService:
    """Keep payment privacy, matching, and allocation logic outside the facade."""

    def __init__(self, core: FinanceCore):
        self.core = core

    def import_payment_csv(
        self,
        *,
        csv_text: str,
        apply: bool = False,
        imported_by: str | None = None,
    ) -> dict[str, Any]:
        """Preview or atomically import bank transactions.

        The parser deliberately returns a redacted view. Raw payer names and
        remittance text are retained only in the Finance DB for matching and
        are never returned to the agent by this method.
        """

        try:
            rows = self._parse_payment_csv(csv_text)
            if not apply:
                return {
                    "success": True,
                    "dry_run": True,
                    "rows": [self._payment_row_public(row) | {"status": "ready"} for row in rows],
                    "applied_count": 0,
                }

            actor = _text(imported_by, field="imported_by", required=True)
            for row in rows:
                self.core.approvals.assert_period_open(
                    str(row["value_date"])[:7], operation="payment import"
                )
            existing = [
                row["bank_transaction_id"]
                for row in rows
                if self.core.store.find_payment_by_transaction_id(row["bank_transaction_id"])
            ]
            if existing:
                raise FinanceConflictError("A bank transaction already exists")

            batch_id = f"paybatch_{uuid.uuid4().hex}"
            records = []
            for row in rows:
                records.append(
                    {
                        **row,
                        "id": f"pay_{uuid.uuid4().hex}",
                        "import_batch_id": batch_id,
                        "status": "UNALLOCATED",
                    }
                )
            stored = self.core.store.create_payments_batch(
                {"id": batch_id, "source": "csv"},
                records,
                imported_by=actor,
            )
            return {
                "success": True,
                "dry_run": False,
                "applied_count": len(stored),
                "payments": [self._payment_public(payment) for payment in stored],
            }
        except (FinanceAuthorizationError, FinanceConflictError, FinanceValidationError) as exc:
            return {"success": False, "errors": [{"error": str(exc)}]}

    def add_payment_alias(
        self,
        customer_id: str,
        alias: str,
        *,
        added_by: str,
    ) -> dict[str, Any]:
        self.core._customer(customer_id)
        actor = _text(added_by, field="added_by", required=True)
        display = _text(alias, field="alias", required=True, limit=300)
        normalized = _payment_alias(display)
        if not normalized:
            raise FinanceValidationError("alias must contain searchable characters")
        stored = self.core.store.put_payment_alias(
            {
                "id": f"pal_{uuid.uuid4().hex}",
                "customer_id": customer_id,
                "alias_display": display,
                "alias_normalized": normalized,
                "active_status": "ACTIVE",
            }
        )
        self.core.store.record_audit(
            "payment_alias.create",
            actor=actor,
            detail={"customer_id": customer_id, "alias_id": stored["id"]},
        )
        return stored

    def list_payment_aliases(self, customer_id: str | None = None) -> list[dict[str, Any]]:
        if customer_id:
            self.core._customer(customer_id)
        return self.core.store.list_payment_aliases(customer_id)

    def match_payment(self, payment_id: str) -> list[dict[str, Any]]:
        """Return ranked deterministic candidates without exposing payer PII."""

        payment = self._payment(payment_id)
        remittance = str(payment.get("remittance_information") or "")
        payer_alias = str(payment.get("payer_alias_normalized") or "")
        payment_amount = Decimal(str(payment["amount"]))
        try:
            payment_date = date.fromisoformat(str(payment["value_date"]))
        except ValueError:
            payment_date = None
        candidates: list[tuple[int, int, str, dict[str, Any]]] = []
        for invoice in self.core.store.list_issued_invoices():
            number = str(invoice.get("invoice_number") or "")
            if not number:
                continue
            number_match = bool(
                re.search(
                    rf"(?<![A-Za-z0-9]){re.escape(number)}(?![A-Za-z0-9])",
                    remittance,
                    flags=re.IGNORECASE,
                )
            )
            customer = self.core.store.get_customer(invoice["customer_id"]) or {}
            profile = customer.get("billing_profile") or {}
            aliases = [
                customer.get("legal_name"),
                profile.get("billing_name"),
                *(customer.get("aliases") or []),
                *(
                    row["alias_normalized"]
                    for row in self.core.store.list_payment_aliases(invoice["customer_id"])
                ),
            ]
            normalized_aliases = {_payment_alias(alias) for alias in aliases if alias}
            payer_match = bool(payer_alias and payer_alias in normalized_aliases)
            collectible_total = Decimal(
                str((invoice.get("totals") or {}).get("collectible_total")
                    or (invoice.get("totals") or {}).get("total")
                    or "0")
            )
            amount_match = payment_amount == collectible_total
            try:
                due_date = date.fromisoformat(str(invoice["due_date"]))
            except (KeyError, ValueError):
                due_date = None
            days_from_due_date = (
                abs((payment_date - due_date).days)
                if payment_date is not None and due_date is not None
                else None
            )
            due_match = days_from_due_date is not None and days_from_due_date <= 7
            if number_match and amount_match:
                priority, reason, confidence = 0, "invoice_number_and_amount_match", "high"
            elif number_match:
                priority, reason, confidence = 1, "invoice_number_match_amount_mismatch", "medium"
            elif payer_match and amount_match:
                priority, reason, confidence = 2, "payer_alias_and_amount_match", "medium"
            elif payer_match and due_match:
                priority, reason, confidence = 3, "payer_alias_and_due_date_match", "low"
            elif amount_match and due_match:
                priority, reason, confidence = 4, "amount_and_due_date_match", "low"
            elif payer_match:
                priority, reason, confidence = 5, "payer_alias_match", "low"
            elif amount_match:
                priority, reason, confidence = 6, "amount_match", "low"
            else:
                continue
            candidates.append(
                (
                    priority,
                    days_from_due_date if days_from_due_date is not None else 999999,
                    number,
                    {
                        "invoice_id": invoice["id"],
                        "invoice_number": number,
                        "amount": _money_text(collectible_total),
                        "currency": invoice["currency"],
                        "due_date": invoice.get("due_date"),
                        "days_from_due_date": days_from_due_date,
                        "matched_reason": reason,
                        "confidence": confidence,
                    },
                )
            )
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        return [candidate for _priority, _days, _number, candidate in candidates]

    def allocate_payment(
        self,
        payment_id: str,
        *,
        allocations: list[dict[str, Any]],
        approved_by: str,
    ) -> dict[str, Any]:
        actor = _text(approved_by, field="approved_by", required=True)
        payment = self._payment(payment_id)
        self.core.approvals.assert_period_open(
            str(payment["value_date"])[:7], operation="payment allocation"
        )
        if not isinstance(allocations, list) or not allocations:
            raise FinanceConflictError("At least one payment allocation is required")
        normalized: list[dict[str, Any]] = []
        for item in allocations:
            if not isinstance(item, dict):
                raise FinanceConflictError("Each payment allocation must be an object")
            try:
                invoice_id = _text(item.get("invoice_id"), field="invoice_id", required=True)
                amount = _money(item.get("amount"), field="amount")
            except FinanceValidationError as exc:
                raise FinanceConflictError(str(exc)) from exc
            normalized.append(
                {
                    "invoice_id": invoice_id,
                    "amount": format(amount, "f"),
                    "allocation_method": _text(
                        item.get("allocation_method") or "manual",
                        field="allocation_method",
                        limit=100,
                    ),
                    "matched_reason": _text(
                        item.get("matched_reason"),
                        field="matched_reason",
                        limit=500,
                    ),
                }
            )
        try:
            return self.core.store.allocate_payment(payment_id, normalized, approved_by=actor)
        except FinanceNotFoundError as exc:
            # A missing invoice in a multi-row request is a conflict for the
            # atomic allocation operation, not a partial-success result.
            raise FinanceConflictError(str(exc)) from exc

    def check_payment(self, invoice_id: str) -> dict[str, Any]:
        invoice = self.core._invoice(invoice_id)
        gross_total = Decimal(str(invoice["totals"]["total"]))
        total = Decimal(
            str(invoice["totals"].get("collectible_total") or invoice["totals"]["total"])
        )
        allocated = sum(
            (Decimal(str(row["allocated_amount"])) for row in self.core.store.invoice_allocations(invoice_id)),
            Decimal("0"),
        )
        outstanding = max(total - allocated, Decimal("0"))
        overpaid = max(allocated - total, Decimal("0"))
        return {
            "invoice_id": invoice_id,
            "invoice_number": invoice.get("invoice_number"),
            "currency": invoice["currency"],
            "invoice_total": _money_text(gross_total),
            "collectible_total": _money_text(total),
            "withholding_tax": str(invoice["totals"].get("withholding_tax") or "0"),
            "allocated": _money_text(allocated),
            "outstanding": _money_text(outstanding),
            "overpaid": _money_text(overpaid),
            "settlement_status": invoice.get("settlement_status", "UNPAID"),
            "due_date": invoice.get("due_date"),
        }

    def unallocated_payments(self) -> list[dict[str, Any]]:
        return [self._payment_public(payment) for payment in self.core.store.list_unallocated_payments()]

    @staticmethod
    def _payment_row_public(row: dict[str, Any]) -> dict[str, Any]:
        return {
            key: row[key]
            for key in (
                "received_date",
                "value_date",
                "amount",
                "currency",
                "bank_transaction_id",
            )
            if key in row
        }

    @staticmethod
    def _payment_public(payment: dict[str, Any]) -> dict[str, Any]:
        return {
            key: payment[key]
            for key in (
                "id",
                "received_date",
                "value_date",
                "amount",
                "currency",
                "bank_transaction_id",
                "import_batch_id",
                "status",
            )
            if key in payment
        }

    @staticmethod
    def _parse_payment_csv(csv_text: str) -> list[dict[str, Any]]:
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
            required = {"received_date", "amount", "currency", "bank_transaction_id"}
            missing = sorted(required - set(headers))
            if missing:
                raise FinanceValidationError(f"CSV is missing required headers: {', '.join(missing)}")

            rows: list[dict[str, Any]] = []
            seen_transactions: set[str] = set()
            for row_number, raw_row in enumerate(reader, start=2):
                if row_number > 10001:
                    raise FinanceValidationError("CSV contains too many rows")
                values = {
                    headers[index]: raw_row.get(raw_headers[index])
                    for index in range(len(headers))
                }
                if not any(str(value or "").strip() for value in values.values()):
                    continue
                received_date = _date(values.get("received_date"), field=f"row {row_number}.received_date")
                value_date = _date(
                    values.get("value_date") or received_date,
                    field=f"row {row_number}.value_date",
                )
                currency = _text(values.get("currency"), field=f"row {row_number}.currency", required=True).upper()
                if len(currency) != 3 or not currency.isalpha():
                    raise FinanceValidationError(f"row {row_number}.currency must be a three-letter code")
                amount = _money(values.get("amount"), field=f"row {row_number}.amount")
                if currency == "JPY" and amount != amount.quantize(Decimal("1")):
                    raise FinanceValidationError(f"row {row_number}.amount cannot have a JPY fraction")
                transaction_id = _text(
                    values.get("bank_transaction_id"),
                    field=f"row {row_number}.bank_transaction_id",
                    required=True,
                    limit=200,
                )
                if transaction_id in seen_transactions:
                    raise FinanceConflictError("CSV contains a duplicate bank transaction")
                seen_transactions.add(transaction_id)
                rows.append(
                    {
                        "received_date": received_date,
                        "value_date": value_date,
                        "amount": format(amount, "f"),
                        "currency": currency,
                        "bank_transaction_id": transaction_id,
                        "payer_name_raw": _text(
                            values.get("payer_name_raw"),
                            field=f"row {row_number}.payer_name_raw",
                            limit=300,
                        ),
                        "payer_alias_normalized": _payment_alias(values.get("payer_name_raw")),
                        "remittance_information": _text(
                            values.get("remittance_information"),
                            field=f"row {row_number}.remittance_information",
                            limit=1000,
                        ),
                    }
                )
            if not rows:
                raise FinanceValidationError("CSV must contain at least one payment row")
        except csv.Error as exc:
            raise FinanceValidationError("CSV could not be parsed") from exc
        return rows

    def _payment(self, payment_id: str) -> dict[str, Any]:
        payment = self.core.store.get_payment(payment_id)
        if payment is None:
            raise FinanceNotFoundError(f"Payment not found: {payment_id}")
        return payment
