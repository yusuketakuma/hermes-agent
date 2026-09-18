"""Reporting service boundary for dashboards and aging views."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from finance_core.core import FinanceCore


def _amount(value: Decimal) -> str:
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


class ReportingService:
    """A small orchestration boundary; calculations remain deterministic."""

    def __init__(self, core: FinanceCore):
        self.core = core

    def dashboard_summary(
        self,
        *,
        from_date: str,
        to_date: str,
        today: str | None = None,
        reminder_days: int = 7,
    ) -> dict[str, Any]:
        summary = self.core.revenue_summary(from_date=from_date, to_date=to_date)
        reference_date = today or date.today().isoformat()
        overdue = self.core.overdue_invoices(today=reference_date)
        reminders = self.core.reminder_candidates(
            today=reference_date,
            reminder_days=reminder_days,
        )
        overdue_total = sum(
            (Decimal(str(row["outstanding"])) for row in overdue), Decimal("0")
        )
        due_soon_total = sum(
            (
                Decimal(str(row["outstanding"]))
                for row in reminders
                if row.get("reminder_type") == "due_soon"
            ),
            Decimal("0"),
        )
        unallocated = self.core.store.list_unallocated_payments()
        unallocated_total = sum(
            (Decimal(str(row["amount"])) for row in unallocated), Decimal("0")
        )
        return {
            **summary,
            "as_of": reference_date,
            "overdue_count": len(overdue),
            "overdue_total": _amount(overdue_total),
            "due_soon_count": sum(
                1 for row in reminders if row.get("reminder_type") == "due_soon"
            ),
            "due_soon_total": _amount(due_soon_total),
            "unallocated_payment_count": len(unallocated),
            "unallocated_payment_total": _amount(unallocated_total),
        }

    def receivables_aging(self, *, today: str | None = None) -> dict[str, dict[str, Any]]:
        reference = date.fromisoformat(today) if today else date.today()
        buckets = {
            "current": {"invoice_count": 0, "outstanding_total": Decimal("0")},
            "1_30": {"invoice_count": 0, "outstanding_total": Decimal("0")},
            "31_60": {"invoice_count": 0, "outstanding_total": Decimal("0")},
            "61_90": {"invoice_count": 0, "outstanding_total": Decimal("0")},
            "91_plus": {"invoice_count": 0, "outstanding_total": Decimal("0")},
        }
        for invoice in self.core.store.list_issued_invoices():
            check = self.core.check_payment(invoice["id"])
            outstanding = Decimal(str(check["outstanding"]))
            if outstanding <= 0:
                continue
            due = date.fromisoformat(str(invoice["due_date"]))
            days = (reference - due).days
            if days <= 0:
                bucket = "current"
            elif days <= 30:
                bucket = "1_30"
            elif days <= 60:
                bucket = "31_60"
            elif days <= 90:
                bucket = "61_90"
            else:
                bucket = "91_plus"
            buckets[bucket]["invoice_count"] += 1
            buckets[bucket]["outstanding_total"] += outstanding
        return {
            name: {
                "invoice_count": values["invoice_count"],
                "outstanding_total": _amount(values["outstanding_total"]),
            }
            for name, values in buckets.items()
        }

    def tax_report(self, *, from_date: str, to_date: str) -> dict[str, Any]:
        tax_by_category: dict[str, dict[str, Decimal]] = {}
        withholding_total = Decimal("0")
        invoice_count = 0
        for invoice in self.core.revenue._report_invoices(from_date=from_date, to_date=to_date):
            invoice_count += 1
            totals = invoice.get("totals") or {}
            withholding_total += Decimal(str(totals.get("withholding_tax") or "0"))
            for item in totals.get("tax_breakdown") or []:
                category = str(item["tax_category"])
                bucket = tax_by_category.setdefault(
                    category,
                    {"taxable_amount": Decimal("0"), "tax_amount": Decimal("0")},
                )
                bucket["taxable_amount"] += Decimal(str(item["taxable_amount"]))
                bucket["tax_amount"] += Decimal(str(item["tax_amount"]))
        return {
            "from_date": from_date,
            "to_date": to_date,
            "invoice_count": invoice_count,
            "withholding_tax_total": _amount(withholding_total),
            "tax_by_category": {
                category: {
                    "taxable_amount": _amount(values["taxable_amount"]),
                    "tax_amount": _amount(values["tax_amount"]),
                }
                for category, values in sorted(tax_by_category.items())
            },
        }
