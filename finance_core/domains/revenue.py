"""Invoice search, receivables, revenue, and accounting-report services."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, TYPE_CHECKING

from finance_core.errors import FinanceConflictError, FinanceValidationError

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


class RevenueService:
    """Keep search and reporting calculations separate from invoice mutations."""

    def __init__(self, core: FinanceCore):
        self.core = core

    def search_invoices(
        self,
        *,
        invoice_number: str | None = None,
        customer_id: str | None = None,
        document_status: str | None = None,
        settlement_status: str | None = None,
        issue_date_from: str | None = None,
        issue_date_to: str | None = None,
        due_date_from: str | None = None,
        due_date_to: str | None = None,
        amount_min: str | None = None,
        amount_max: str | None = None,
    ) -> list[dict[str, Any]]:
        def optional_date(value: str | None, field: str) -> str | None:
            return _date(value, field=field) if value else None

        issue_start = optional_date(issue_date_from, "issue_date_from")
        issue_end = optional_date(issue_date_to, "issue_date_to")
        due_start = optional_date(due_date_from, "due_date_from")
        due_end = optional_date(due_date_to, "due_date_to")
        if issue_start and issue_end and issue_end < issue_start:
            raise FinanceValidationError("issue_date_to cannot be earlier than issue_date_from")
        if due_start and due_end and due_end < due_start:
            raise FinanceValidationError("due_date_to cannot be earlier than due_date_from")

        def optional_amount(value: str | None, field: str) -> Decimal | None:
            if value in (None, ""):
                return None
            try:
                amount = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise FinanceValidationError(f"{field} must be a valid decimal") from exc
            if not amount.is_finite() or amount < 0:
                raise FinanceValidationError(f"{field} must be non-negative")
            return amount

        minimum = optional_amount(amount_min, "amount_min")
        maximum = optional_amount(amount_max, "amount_max")
        if minimum is not None and maximum is not None and maximum < minimum:
            raise FinanceValidationError("amount_max cannot be less than amount_min")
        number_filter = str(invoice_number or "").strip().casefold()
        customer_filter = str(customer_id or "").strip()
        status_filter = str(document_status or "").strip().upper()
        settlement_filter = str(settlement_status or "").strip().upper()
        rows: list[dict[str, Any]] = []
        for invoice in self.core.store.list_invoices():
            if number_filter and number_filter not in str(invoice.get("invoice_number") or "").casefold():
                continue
            if customer_filter and invoice.get("customer_id") != customer_filter:
                continue
            if status_filter and invoice.get("document_status") != status_filter:
                continue
            if settlement_filter and invoice.get("settlement_status") != settlement_filter:
                continue
            issue_value = str(invoice.get("issue_date") or "")
            due_value = str(invoice.get("due_date") or "")
            if issue_start and issue_value < issue_start:
                continue
            if issue_end and issue_value > issue_end:
                continue
            if due_start and due_value < due_start:
                continue
            if due_end and due_value > due_end:
                continue
            total = Decimal(str((invoice.get("totals") or {}).get("total") or "0"))
            if minimum is not None and total < minimum:
                continue
            if maximum is not None and total > maximum:
                continue
            rows.append(invoice)
        return rows

    def overdue_invoices(self, *, today: str | None = None) -> list[dict[str, Any]]:
        cutoff = _date(today, field="today") if today else date.today().isoformat()
        rows: list[dict[str, Any]] = []
        for invoice in self.search_invoices(document_status="ISSUED"):
            if str(invoice.get("due_date") or "") >= cutoff:
                continue
            check = self.core.check_payment(invoice["id"])
            if Decimal(str(check["outstanding"])) <= 0:
                continue
            rows.append(
                {
                    "invoice_id": invoice["id"],
                    "invoice_number": invoice.get("invoice_number"),
                    "customer_id": invoice["customer_id"],
                    "due_date": invoice["due_date"],
                    "currency": invoice["currency"],
                    "invoice_total": check["invoice_total"],
                    "outstanding": check["outstanding"],
                    "settlement_status": check["settlement_status"],
                }
            )
        return rows

    def reminder_candidates(
        self,
        *,
        today: str | None = None,
        reminder_days: int = 0,
    ) -> list[dict[str, Any]]:
        base = date.fromisoformat(today) if today else date.today()
        try:
            horizon_days = int(reminder_days)
        except (TypeError, ValueError) as exc:
            raise FinanceValidationError("reminder_days must be an integer") from exc
        if horizon_days < 0 or horizon_days > 365:
            raise FinanceValidationError("reminder_days must be between 0 and 365")
        horizon = base + timedelta(days=horizon_days)
        rows: list[dict[str, Any]] = []
        for invoice in self.search_invoices(document_status="ISSUED"):
            due = date.fromisoformat(invoice["due_date"])
            check = self.core.check_payment(invoice["id"])
            if Decimal(str(check["outstanding"])) <= 0 or due > horizon:
                continue
            rows.append(
                {
                    "invoice_id": invoice["id"],
                    "invoice_number": invoice.get("invoice_number"),
                    "customer_id": invoice["customer_id"],
                    "due_date": invoice["due_date"],
                    "days_overdue": max((base - due).days, 0),
                    "reminder_type": "overdue" if due < base else "due_soon",
                    "currency": invoice["currency"],
                    "outstanding": check["outstanding"],
                }
            )
        return rows

    def record_revenue(
        self,
        *,
        customer_id: str,
        recognition_date: str,
        service_period: str,
        amount: str,
        currency: str,
        description: str,
        source_type: str,
        source_id: str,
        invoice_id: str | None = None,
        project_id: str | None = None,
        department_id: str | None = None,
        created_by: str,
    ) -> dict[str, Any]:
        actor = _text(created_by, field="created_by", required=True)
        customer = self.core._customer(customer_id)
        recognition = _date(recognition_date, field="recognition_date")
        self.core.approvals.assert_period_open(recognition[:7], operation="revenue recognition")
        period = _text(service_period, field="service_period", required=True, limit=100)
        total = _money(amount, field="amount")
        normalized_currency = _text(currency, field="currency", required=True).upper()
        if len(normalized_currency) != 3 or not normalized_currency.isalpha():
            raise FinanceValidationError("currency must be a three-letter code")
        normalized_source_type = _text(source_type, field="source_type", required=True, limit=100).lower()
        if normalized_source_type not in {"service_period", "invoice", "milestone", "manual", "adjustment"}:
            raise FinanceValidationError("source_type is unsupported")
        normalized_source_id = _text(source_id, field="source_id", required=True, limit=300)
        if normalized_source_type == "invoice" and invoice_id is None:
            invoice_id = normalized_source_id
        if invoice_id:
            invoice = self.core._invoice(invoice_id)
            if invoice.get("document_status") != "ISSUED":
                raise FinanceConflictError("Revenue may reference only an ISSUED invoice")
            if invoice.get("customer_id") != customer_id or invoice.get("currency") != normalized_currency:
                raise FinanceConflictError("Revenue and invoice master data do not match")
        existing = self.core.store.find_revenue_entry(normalized_source_type, normalized_source_id)
        if existing is not None:
            return existing
        record = {
            "id": f"rev_{uuid.uuid4().hex}",
            "customer_id": customer_id,
            "recognition_date": recognition,
            "service_period": period,
            "amount": _money_text(total),
            "currency": normalized_currency,
            "description": _text(description, field="description", required=True, limit=1000),
            "source_type": normalized_source_type,
            "source_id": normalized_source_id,
            "invoice_id": invoice_id,
            "project_id": _text(project_id, field="project_id", limit=200) or None,
            "department_id": _text(department_id, field="department_id", limit=200) or None,
            "status": "ACTIVE",
        }
        stored = self.core.store.put_revenue_entry(record)
        self.core.store.record_audit(
            "revenue.create",
            actor=actor,
            detail={"revenue_entry_id": stored["id"], "customer_id": customer_id},
        )
        return stored

    def search_revenue_entries(
        self,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        customer_id: str | None = None,
        project_id: str | None = None,
        department_id: str | None = None,
    ) -> list[dict[str, Any]]:
        start = _date(from_date, field="from_date") if from_date else None
        end = _date(to_date, field="to_date") if to_date else None
        if start and end and end < start:
            raise FinanceValidationError("to_date cannot be earlier than from_date")
        if customer_id:
            self.core._customer(customer_id)
        return self.core.store.list_revenue_entries(
            from_date=start,
            to_date=end,
            customer_id=customer_id,
            project_id=_text(project_id, field="project_id", limit=200) or None,
            department_id=_text(department_id, field="department_id", limit=200) or None,
        )

    def revenue_by_project(self, *, from_date: str, to_date: str) -> list[dict[str, Any]]:
        grouped: dict[str, Decimal] = {}
        for entry in self.search_revenue_entries(from_date=from_date, to_date=to_date):
            project = str(entry.get("project_id") or "unassigned")
            grouped[project] = grouped.get(project, Decimal("0")) + Decimal(str(entry["amount"]))
        return [
            {"project_id": project, "recognized_revenue_total": _money_text(total)}
            for project, total in sorted(grouped.items())
        ]

    def _recognized_revenue_total(self, *, from_date: str, to_date: str) -> Decimal:
        return sum(
            (Decimal(str(entry["amount"])) for entry in self.search_revenue_entries(from_date=from_date, to_date=to_date)),
            Decimal("0"),
        )

    def _report_invoices(self, *, from_date: str, to_date: str) -> list[dict[str, Any]]:
        start = _date(from_date, field="from_date")
        end = _date(to_date, field="to_date")
        if end < start:
            raise FinanceValidationError("to_date cannot be earlier than from_date")
        return [
            invoice
            for invoice in self.search_invoices(
                document_status="ISSUED", issue_date_from=start, issue_date_to=end
            )
        ]

    def _invoice_report_row(self, invoice: dict[str, Any]) -> dict[str, Any]:
        check = self.core.check_payment(invoice["id"])
        return {
            "invoice_id": invoice["id"],
            "invoice_number": invoice.get("invoice_number"),
            "customer_id": invoice["customer_id"],
            "issue_date": invoice["issue_date"],
            "currency": invoice["currency"],
            "invoiced_total": check["invoice_total"],
            "received_total": check["allocated"],
            "outstanding": check["outstanding"],
        }

    def revenue_summary(self, *, from_date: str, to_date: str) -> dict[str, Any]:
        rows = [self._invoice_report_row(invoice) for invoice in self._report_invoices(from_date=from_date, to_date=to_date)]
        total = sum((Decimal(row["invoiced_total"]) for row in rows), Decimal("0"))
        received = sum((Decimal(row["received_total"]) for row in rows), Decimal("0"))
        outstanding = sum((Decimal(row["outstanding"]) for row in rows), Decimal("0"))
        recognized = self._recognized_revenue_total(from_date=from_date, to_date=to_date)
        return {
            "from_date": from_date,
            "to_date": to_date,
            "invoice_count": len(rows),
            "invoiced_total": _money_text(total),
            "received_total": _money_text(received),
            "outstanding_total": _money_text(outstanding),
            "recognized_revenue_total": _money_text(recognized),
        }

    def revenue_by_customer(self, *, from_date: str, to_date: str) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Decimal]] = {}
        for row in (self._invoice_report_row(invoice) for invoice in self._report_invoices(from_date=from_date, to_date=to_date)):
            bucket = grouped.setdefault(row["customer_id"], {"invoiced": Decimal("0"), "received": Decimal("0"), "outstanding": Decimal("0"), "recognized": Decimal("0")})
            bucket["invoiced"] += Decimal(row["invoiced_total"])
            bucket["received"] += Decimal(row["received_total"])
            bucket["outstanding"] += Decimal(row["outstanding"])
        for entry in self.search_revenue_entries(from_date=from_date, to_date=to_date):
            bucket = grouped.setdefault(entry["customer_id"], {"invoiced": Decimal("0"), "received": Decimal("0"), "outstanding": Decimal("0"), "recognized": Decimal("0")})
            bucket["recognized"] += Decimal(str(entry["amount"]))
        return [
            {
                "customer_id": customer_id,
                "invoiced_total": _money_text(values["invoiced"]),
                "received_total": _money_text(values["received"]),
                "outstanding_total": _money_text(values["outstanding"]),
                "recognized_revenue_total": _money_text(values["recognized"]),
            }
            for customer_id, values in sorted(grouped.items())
        ]

    def revenue_by_month(self, *, from_date: str, to_date: str) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Decimal]] = {}
        for row in (self._invoice_report_row(invoice) for invoice in self._report_invoices(from_date=from_date, to_date=to_date)):
            month = row["issue_date"][:7]
            bucket = grouped.setdefault(month, {"invoiced": Decimal("0"), "received": Decimal("0"), "outstanding": Decimal("0"), "recognized": Decimal("0")})
            bucket["invoiced"] += Decimal(row["invoiced_total"])
            bucket["received"] += Decimal(row["received_total"])
            bucket["outstanding"] += Decimal(row["outstanding"])
        for entry in self.search_revenue_entries(from_date=from_date, to_date=to_date):
            month = entry["recognition_date"][:7]
            bucket = grouped.setdefault(month, {"invoiced": Decimal("0"), "received": Decimal("0"), "outstanding": Decimal("0"), "recognized": Decimal("0")})
            bucket["recognized"] += Decimal(str(entry["amount"]))
        return [
            {
                "month": month,
                "invoiced_total": _money_text(values["invoiced"]),
                "received_total": _money_text(values["received"]),
                "outstanding_total": _money_text(values["outstanding"]),
                "recognized_revenue_total": _money_text(values["recognized"]),
            }
            for month, values in sorted(grouped.items())
        ]

    def export_accounting_csv(self, *, from_date: str, to_date: str) -> dict[str, Any]:
        output = io.StringIO(newline="")
        writer = csv.DictWriter(
            output,
            fieldnames=["invoice_number", "issue_date", "customer_id", "account", "debit", "credit", "currency"],
            lineterminator="\n",
        )
        writer.writeheader()
        row_count = 0
        for invoice in self._report_invoices(from_date=from_date, to_date=to_date):
            totals = invoice["totals"]
            common = {
                "invoice_number": invoice.get("invoice_number") or "",
                "issue_date": invoice["issue_date"],
                "customer_id": invoice["customer_id"],
                "currency": invoice["currency"],
            }
            writer.writerow({**common, "account": "accounts_receivable", "debit": totals["total"], "credit": "0"})
            writer.writerow({**common, "account": "sales", "debit": "0", "credit": totals["subtotal"]})
            if Decimal(str(totals.get("tax") or "0")):
                writer.writerow({**common, "account": "consumption_tax", "debit": "0", "credit": totals["tax"]})
            row_count += 3 if Decimal(str(totals.get("tax") or "0")) else 2
        return {"from_date": from_date, "to_date": to_date, "row_count": row_count, "csv": output.getvalue()}
