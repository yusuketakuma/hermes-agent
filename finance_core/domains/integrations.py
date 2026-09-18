"""External accounting integration boundary."""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal
from typing import Any, TYPE_CHECKING

from finance_core.errors import FinanceValidationError

if TYPE_CHECKING:
    from finance_core.core import FinanceCore


class IntegrationService:
    """Hold target-specific export adapters outside the finance domain."""

    def __init__(self, core: FinanceCore):
        self.core = core

    _TARGETS = {
        "generic": "generic-journal-v1",
        "freee": "freee-journal-v1",
        "moneyforward": "moneyforward-journal-v1",
        "yayoi": "yayoi-journal-v1",
    }

    FREEE_INVOICE_ENDPOINT = "https://api.freee.co.jp/iv/invoices"
    FREEE_OAUTH_AUTHORIZE_ENDPOINT = "https://accounts.secure.freee.co.jp/public_api/authorize"
    FREEE_OAUTH_TOKEN_ENDPOINT = "https://accounts.secure.freee.co.jp/public_api/token"

    @staticmethod
    def _csv(rows: list[dict[str, Any]], fieldnames: list[str]) -> str:
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue()

    def export_bundle(
        self,
        *,
        from_date: str,
        to_date: str,
        target: str = "generic",
    ) -> dict[str, Any]:
        normalized_target = str(target or "").strip().lower()
        if normalized_target not in self._TARGETS:
            raise ValueError(f"Unsupported accounting integration target: {target}")
        try:
            start = date.fromisoformat(from_date).isoformat()
            end = date.fromisoformat(to_date).isoformat()
        except ValueError as exc:
            raise FinanceValidationError("from_date and to_date must use YYYY-MM-DD") from exc
        if end < start:
            raise FinanceValidationError("to_date cannot be earlier than from_date")

        invoice_rows: list[dict[str, Any]] = []
        for invoice in self.core.revenue._report_invoices(from_date=start, to_date=end):
            totals = invoice["totals"]
            common = {
                "date": invoice["issue_date"],
                "partner_id": invoice["customer_id"],
                "invoice_id": invoice["id"],
                "invoice_number": invoice.get("invoice_number") or "",
                "currency": invoice["currency"],
            }
            invoice_rows.append(
                {
                    **common,
                    "debit_account": "accounts_receivable",
                    "debit_amount": totals["total"],
                    "debit_tax_code": "",
                    "credit_account": "sales",
                    "credit_amount": totals["subtotal"],
                    "credit_tax_code": "taxable",
                    "description": invoice.get("invoice_number") or invoice["id"],
                }
            )
            if Decimal(str(totals.get("tax") or "0")):
                invoice_rows.append(
                    {
                        **common,
                        "debit_account": "accounts_receivable",
                        "debit_amount": "0",
                        "debit_tax_code": "",
                        "credit_account": "consumption_tax",
                        "credit_amount": totals["tax"],
                        "credit_tax_code": "tax",
                        "description": invoice.get("invoice_number") or invoice["id"],
                    }
                )

        payment_rows: list[dict[str, Any]] = []
        for payment in self.core.store.list_payments():
            value_date = str(payment.get("value_date") or "")
            if not start <= value_date <= end:
                continue
            allocations = self.core.store.payment_allocations(payment["id"])
            if not allocations:
                allocations = [{"invoice_id": "", "allocated_amount": payment["amount"]}]
            for allocation in allocations:
                payment_rows.append(
                    {
                        "date": value_date,
                        "partner_id": "",
                        "payment_id": payment["id"],
                        "invoice_id": allocation.get("invoice_id") or "",
                        "currency": payment["currency"],
                        "debit_account": "bank",
                        "debit_amount": allocation["allocated_amount"],
                        "debit_tax_code": "",
                        "credit_account": "accounts_receivable",
                        "credit_amount": allocation["allocated_amount"],
                        "credit_tax_code": "",
                        "description": payment["id"],
                    }
                )

        revenue_rows: list[dict[str, Any]] = []
        for entry in self.core.search_revenue_entries(from_date=start, to_date=end):
            revenue_rows.append(
                {
                    "date": entry["recognition_date"],
                    "partner_id": entry["customer_id"],
                    "payment_id": "",
                    "invoice_id": entry.get("invoice_id") or "",
                    "currency": entry["currency"],
                    "debit_account": "unbilled_revenue",
                    "debit_amount": entry["amount"],
                    "debit_tax_code": "",
                    "credit_account": "sales",
                    "credit_amount": entry["amount"],
                    "credit_tax_code": "recognized",
                    "description": entry["source_id"],
                }
            )
        fields = [
            "date",
            "debit_account",
            "debit_amount",
            "debit_tax_code",
            "credit_account",
            "credit_amount",
            "credit_tax_code",
            "partner_id",
            "invoice_number",
            "invoice_id",
            "payment_id",
            "description",
            "currency",
        ]
        return {
            "target": normalized_target,
            "format_version": self._TARGETS[normalized_target],
            "from_date": start,
            "to_date": end,
            "invoices_csv": self._csv(invoice_rows, fields),
            "payments_csv": self._csv(payment_rows, fields),
            "revenue_csv": self._csv(revenue_rows, fields),
            "invoice_row_count": len(invoice_rows),
            "payment_row_count": len(payment_rows),
            "revenue_row_count": len(revenue_rows),
        }

    def freee_invoice_preview(
        self,
        *,
        invoice_id: str,
        company_id: int,
        partner_id: int,
    ) -> dict[str, Any]:
        """Build the official freee請求書 API payload without network access.

        The endpoint requires freee OAuth and accounting master IDs. This
        method deliberately stops at a deterministic, reviewable payload so a
        caller cannot create an external invoice accidentally.
        """

        try:
            normalized_company_id = int(company_id)
            normalized_partner_id = int(partner_id)
        except (TypeError, ValueError) as exc:
            raise FinanceValidationError("company_id and partner_id must be integers") from exc
        if normalized_company_id <= 0 or normalized_partner_id <= 0:
            raise FinanceValidationError("company_id and partner_id must be positive")

        invoice = self.core._invoice(invoice_id)
        issuer = self.core._issuer(invoice["issuer_id"])
        customer = self.core._customer(invoice["customer_id"])
        profile = customer.get("billing_profile") or {}
        rounding = str(issuer.get("tax_rounding_policy") or "HALF_UP").upper()
        tax_fraction = {
            "DOWN": "omit",
            "UP": "round_up",
            "HALF_UP": "round",
        }.get(rounding)
        if tax_fraction is None:
            raise FinanceValidationError("Issuer tax rounding policy cannot be mapped to freee")

        lines: list[dict[str, Any]] = []
        for line in invoice.get("lines") or []:
            category = str(line.get("tax_category") or "")
            if category == "STANDARD_10":
                tax_rate = 10
            elif category == "REDUCED_8":
                tax_rate = 8
            elif category in {"EXEMPT", "OUT_OF_SCOPE"}:
                tax_rate = 0
            else:
                raise FinanceValidationError(f"Unsupported tax category for freee: {category}")
            quantity = Decimal(str(line.get("quantity") or "0"))
            if not quantity.is_finite() or quantity <= 0:
                raise FinanceValidationError("Invoice line quantity cannot be mapped to freee")
            quantity_value: int | float = (
                int(quantity) if quantity == quantity.to_integral_value() else float(quantity)
            )
            service_date = str(line.get("service_date_or_period") or invoice["issue_date"])
            if len(service_date) == 7:
                service_date = f"{service_date}-01"
            lines.append(
                {
                    "type": "item",
                    "description": str(line.get("description") or "明細"),
                    "sales_date": service_date,
                    "unit": str(line.get("unit") or ""),
                    "quantity": quantity_value,
                    "unit_price": str(line.get("unit_price") or "0"),
                    "tax_rate": tax_rate,
                    "reduced_tax_rate": category == "REDUCED_8",
                    "withholding": Decimal(str(invoice.get("withholding_tax_rate") or "0")) > 0,
                }
            )
        if not lines:
            raise FinanceValidationError("Invoice must contain at least one line for freee")

        honorific = str(profile.get("honorific") or "").strip()
        if honorific not in {"御中", "様"}:
            honorific = "御中" if customer.get("customer_kind") == "company" else "様"
        request: dict[str, Any] = {
            "company_id": normalized_company_id,
            "invoice_number": invoice.get("invoice_number"),
            "billing_date": invoice["issue_date"],
            "issue_date": invoice["issue_date"],
            "payment_date": invoice["due_date"],
            "payment_type": "transfer",
            "subject": str(invoice.get("invoice_number") or "請求書"),
            "tax_entry_method": "out",
            "tax_fraction": tax_fraction,
            "withholding_tax_entry_method": (
                "in" if invoice.get("withholding_tax_base") == "total" else "out"
            ),
            "invoice_note": str(invoice.get("notes") or ""),
            "partner_id": normalized_partner_id,
            "partner_title": honorific,
            "lines": lines,
        }
        return {
            "target": "freee-invoice",
            "format_version": "freee-invoice-api-v1",
            "endpoint": self.FREEE_INVOICE_ENDPOINT,
            "oauth_authorize_endpoint": self.FREEE_OAUTH_AUTHORIZE_ENDPOINT,
            "oauth_token_endpoint": self.FREEE_OAUTH_TOKEN_ENDPOINT,
            "network_requested": False,
            "request": request,
        }
