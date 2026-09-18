"""Invoice drafting, validation, rendering, and lifecycle services."""

from __future__ import annotations

import copy
import re
import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, TYPE_CHECKING

from finance_core.errors import FinanceConflictError, FinanceValidationError
from finance_core.tax import calculate_totals, calculate_withholding_tax

if TYPE_CHECKING:
    from finance_core.core import FinanceCore


_DOCUMENT_TYPES = frozenset(
    {"invoice", "quote", "delivery_note", "purchase_order", "receipt", "correction_invoice"}
)


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


def _money_text(value: Decimal) -> str:
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


class InvoiceService:
    """Keep invoice lifecycle mutations behind one domain boundary."""

    def __init__(self, core: FinanceCore):
        self.core = core

    def draft_from_billing_rule(
        self,
        *,
        issuer_id: str,
        billing_rule_id: str,
        service_period: str,
        issue_date: str,
        quantity: str = "1",
        due_date: str | None = None,
        template_id: str | None = None,
        template_version: str = "1.0.0",
        notes: str | None = None,
        billing_key: str,
        created_by: str | None = None,
        line_source_type: str = "billing_rule",
        line_source_id: str | None = None,
        generated_by: str = "rule",
        work_entry_ids: list[str] | None = None,
        withholding_tax_rate: str | None = None,
        withholding_tax_base: str = "subtotal",
    ) -> dict[str, Any]:
        rule = self.core.get_billing_rule(billing_rule_id)
        contract = self.core.get_contract(rule["contract_id"])
        customer_id = str(contract["customer_id"])
        self.core._customer(customer_id)
        if rule.get("active_status") != "ACTIVE" or contract.get("active_status") != "ACTIVE":
            raise FinanceConflictError("Only active contracts and billing rules can generate drafts")
        normalized_key = _text(billing_key, field="billing_key", required=True, limit=200)
        existing = self.core.store.find_invoice_by_billing_key(normalized_key)
        if existing is not None:
            return existing
        period = _text(service_period, field="service_period", required=True)
        normalized_issue = _date(issue_date, field="issue_date")
        try:
            amount = Decimal(str(quantity))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise FinanceValidationError("quantity must be a valid decimal") from exc
        if not amount.is_finite() or amount <= 0:
            raise FinanceValidationError("quantity must be greater than zero")
        payment_rule = contract.get("payment_due_rule") or {}
        if due_date:
            normalized_due = _date(due_date, field="due_date")
        else:
            try:
                due_days = int(payment_rule.get("days_after_issue", 30))
            except (TypeError, ValueError) as exc:
                raise FinanceValidationError("payment_due_rule.days_after_issue must be an integer") from exc
            if due_days < 0 or due_days > 3650:
                raise FinanceValidationError("payment_due_rule.days_after_issue must be between 0 and 3650")
            normalized_due = (date.fromisoformat(normalized_issue) + timedelta(days=due_days)).isoformat()
        selected_template = _text(
            template_id or contract.get("default_template_id"),
            field="template_id",
            required=True,
        )
        line = {
            "description": rule["description"],
            "quantity": format(amount, "f"),
            "unit": rule["unit"],
            "unit_price": rule["unit_price"],
            "tax_category": rule["tax_category"],
            "tax_rate": rule["tax_rate"],
            "service_period": period,
            "service_date_or_period": period,
            "source_type": _text(line_source_type, field="line_source_type", required=True, limit=100),
            "source_id": _text(line_source_id or billing_rule_id, field="line_source_id", required=True, limit=300),
            "billing_rule_version_id": billing_rule_id,
            "calculation_expression": f"{format(amount, 'f')} × {rule['unit_price']} {contract['currency']}",
            "generated_by": _text(generated_by, field="generated_by", required=True, limit=100),
        }
        draft = self.draft_create(
            issuer_id=issuer_id,
            customer_id=customer_id,
            template_id=selected_template,
            template_version=template_version,
            issue_date=normalized_issue,
            service_period=period,
            due_date=normalized_due,
            currency=contract["currency"],
            lines=[line],
            notes=notes,
            billing_key=normalized_key,
            created_by=created_by,
            contract_id=contract["id"],
            billing_rule_version_id=billing_rule_id,
            work_entry_ids=work_entry_ids,
            withholding_tax_rate=withholding_tax_rate,
            withholding_tax_base=withholding_tax_base,
        )
        return draft

    def draft_create(
        self,
        *,
        issuer_id: str,
        customer_id: str,
        template_id: str,
        template_version: str,
        issue_date: str,
        service_period: str,
        due_date: str,
        currency: str,
        lines: list[dict[str, Any]],
        notes: str | None = None,
        document_type: str = "invoice",
        billing_key: str | None = None,
        created_by: str | None = None,
        contract_id: str | None = None,
        billing_rule_version_id: str | None = None,
        converted_from: dict[str, Any] | None = None,
        correction_of: str | None = None,
        correction_reason: str | None = None,
        work_entry_ids: list[str] | None = None,
        withholding_tax_rate: str | None = None,
        withholding_tax_base: str = "subtotal",
    ) -> dict[str, Any]:
        issuer = self.core._issuer(issuer_id)
        self.core._customer(customer_id)
        normalized_document_type = _text(document_type, field="document_type", required=True).lower()
        if normalized_document_type not in _DOCUMENT_TYPES:
            raise FinanceValidationError(
                "document_type must be invoice, quote, delivery_note, purchase_order, receipt, or correction_invoice"
            )
        normalized_billing_key = _text(billing_key, field="billing_key", limit=200)
        if normalized_billing_key and self.core.store.find_invoice_by_billing_key(normalized_billing_key):
            raise FinanceConflictError("An invoice with the same billing_key already exists")
        metadata = self.core.templates.get(template_id, template_version)
        normalized_currency = _text(currency, field="currency", required=True).upper()
        if normalized_currency != metadata.get("currency"):
            raise FinanceValidationError(
                f"Template {template_id}@{template_version} only supports {metadata['currency']}"
            )
        issue = _date(issue_date, field="issue_date")
        due = _date(due_date, field="due_date")
        if due < issue:
            raise FinanceValidationError("due_date cannot be earlier than issue_date")
        period = _text(service_period, field="service_period", required=True)
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}(?:-[0-9]{2})?", period):
            raise FinanceValidationError("service_period must use YYYY-MM or YYYY-MM-DD")
        self.core.approvals.assert_period_open(issue[:7], operation="invoice draft creation")
        totals = calculate_totals(
            lines,
            currency=normalized_currency,
            rounding=issuer.get("tax_rounding_policy", "HALF_UP"),
        )
        totals = self._apply_withholding_tax(
            totals,
            currency=normalized_currency,
            rounding=issuer.get("tax_rounding_policy", "HALF_UP"),
            rate=withholding_tax_rate,
            base=withholding_tax_base,
        )
        record = {
            "id": f"finv_{uuid.uuid4().hex}",
            "invoice_number": None,
            "issuer_id": issuer_id,
            "customer_id": customer_id,
            "template_id": template_id,
            "template_version": template_version,
            "document_type": normalized_document_type,
            "billing_key": normalized_billing_key or None,
            "created_by": _text(created_by, field="created_by", limit=200) or None,
            "contract_id": _text(contract_id, field="contract_id", limit=100) or None,
            "billing_rule_version_id": _text(
                billing_rule_version_id, field="billing_rule_version_id", limit=100
            ) or None,
            "converted_from": copy.deepcopy(converted_from) if converted_from else None,
            "correction_of": _text(correction_of, field="correction_of", limit=100) or None,
            "correction_reason": _text(correction_reason, field="correction_reason", limit=1000) or None,
            "work_entry_ids": list(work_entry_ids or []),
            "withholding_tax_rate": totals["withholding_tax_rate"],
            "withholding_tax_base": totals["withholding_tax_base"],
            "issue_date": issue,
            "service_period": period,
            "due_date": due,
            "currency": normalized_currency,
            "lines": totals.pop("lines"),
            "totals": totals,
            "notes": _text(notes, field="notes"),
            "document_status": "DRAFT",
            "delivery_status": "NOT_SENT",
            "settlement_status": "UNPAID",
            "master_snapshot": None,
        }
        return self.core.store.create_invoice(record, actor=created_by)

    def bulk_draft_create(
        self,
        requests: list[dict[str, Any]],
        *,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """Create recurring/bulk drafts while making reruns idempotent."""

        if not isinstance(requests, list) or not requests:
            raise FinanceValidationError("requests must contain at least one draft")
        if len(requests) > 100:
            raise FinanceValidationError("requests may contain at most 100 drafts")
        created: list[dict[str, Any]] = []
        existing: list[dict[str, Any]] = []
        for index, raw_request in enumerate(requests, start=1):
            if not isinstance(raw_request, dict):
                raise FinanceValidationError(f"requests[{index}] must be an object")
            payload = copy.deepcopy(raw_request)
            billing_key = _text(payload.get("billing_key"), field=f"requests[{index}].billing_key", limit=200)
            if not billing_key:
                raise FinanceValidationError(f"requests[{index}].billing_key is required")
            prior = self.core.store.find_invoice_by_billing_key(billing_key)
            if prior is not None:
                existing.append({"billing_key": billing_key, "invoice_id": prior["id"]})
                continue
            payload["billing_key"] = billing_key
            payload.setdefault("created_by", created_by)
            created.append(self.draft_create(**payload))
        return {"created": created, "existing": existing}

    def validate_invoice(self, invoice_id: str) -> dict[str, Any]:
        invoice = self.core._invoice(invoice_id)
        errors: list[str] = []
        try:
            issuer = self.core._issuer(invoice["issuer_id"])
            self.core._customer(invoice["customer_id"])
            metadata = self.core.templates.get(invoice["template_id"], invoice["template_version"])
            if invoice.get("currency") != metadata.get("currency"):
                errors.append("currency is not supported by the template")
            if issuer.get("qualified_invoice_issuer") and not issuer.get("registration_number"):
                errors.append("qualified issuer is missing registration_number")
            calculated = calculate_totals(
                invoice.get("lines") or [],
                currency=invoice["currency"],
                rounding=issuer.get("tax_rounding_policy", "HALF_UP"),
            )
            calculated = self._apply_withholding_tax(
                calculated,
                currency=invoice["currency"],
                rounding=issuer.get("tax_rounding_policy", "HALF_UP"),
                rate=invoice.get("withholding_tax_rate"),
                base=invoice.get("withholding_tax_base") or "subtotal",
            )
            expected = {
                key: invoice.get("totals", {}).get(key)
                for key in (
                    "subtotal",
                    "tax",
                    "total",
                    "withholding_tax_rate",
                    "withholding_tax_base",
                    "withholding_tax",
                    "collectible_total",
                )
            }
            actual = {key: calculated[key] for key in expected}
            if expected != actual:
                errors.append("stored totals do not match deterministic recalculation")
        except FinanceValidationError as exc:
            errors.append(str(exc))
        return {"invoice_id": invoice_id, "valid": not errors, "errors": errors}

    def preview_pdf(self, invoice_id: str) -> dict[str, Any]:
        invoice = self.core._invoice(invoice_id)
        validation = self.validate_invoice(invoice_id)
        if not validation["valid"]:
            raise FinanceValidationError("; ".join(validation["errors"]))
        presentation = self._presentation(invoice)
        html = self.core.templates.render_html(presentation, draft=True)
        pdf = self.core.renderer.render(html)
        document = self.core.documents.write_preview(invoice_id, pdf)
        return {**invoice, **document, "document_status": "DRAFT"}

    def approve_invoice(self, invoice_id: str, *, approved_by: str) -> dict[str, Any]:
        actor = _text(approved_by, field="approved_by", required=True)
        invoice = self.core._invoice(invoice_id)
        self.core.approvals.assert_period_open(
            str(invoice.get("issue_date") or "")[:7], operation="invoice approval"
        )
        self.core.approvals.authorize_invoice_action(invoice, actor_id=actor, action="approve")
        return self.core.store.transition_invoice(
            invoice_id,
            expected_status="DRAFT",
            updates={"approved_at": self.core._now(), "approved_by": actor, "document_status": "APPROVED"},
            action="invoice.approve",
            actor=actor,
        )

    def issue_invoice(self, invoice_id: str, *, issued_by: str) -> dict[str, Any]:
        actor = _text(issued_by, field="issued_by", required=True)
        invoice = self.core._invoice(invoice_id)
        self.core.approvals.assert_period_open(
            str(invoice.get("issue_date") or "")[:7], operation="invoice issue"
        )
        self.core.approvals.authorize_invoice_action(invoice, actor_id=actor, action="issue")
        validation = self.validate_invoice(invoice_id)
        if not validation["valid"]:
            raise FinanceValidationError("; ".join(validation["errors"]))

        def build_document(canonical: dict[str, Any], invoice_number: str) -> dict[str, Any]:
            issuer = self.core._issuer(canonical["issuer_id"])
            customer = self.core._customer(canonical["customer_id"])
            canonical["master_snapshot"] = {
                "issuer": copy.deepcopy(issuer),
                "customer": copy.deepcopy(customer),
            }
            presentation = self._presentation(canonical, issuer=issuer, customer=customer)
            html = self.core.templates.render_html(presentation, draft=False)
            pdf = self.core.renderer.render(html)
            return self.core.documents.write_issued(invoice_number, presentation, pdf)

        return self.core.store.issue_invoice(invoice_id, issued_by=actor, build_document=build_document)

    def convert_document(
        self,
        source_invoice_id: str,
        *,
        target_document_type: str,
        converted_by: str,
        billing_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a new draft document from an existing document."""

        actor = _text(converted_by, field="converted_by", required=True)
        source = self.core._invoice(source_invoice_id)
        target = _text(target_document_type, field="target_document_type", required=True).lower()
        if target not in _DOCUMENT_TYPES:
            raise FinanceValidationError("target_document_type is unsupported")
        if target == source.get("document_type"):
            raise FinanceConflictError("A document cannot be converted to the same type")
        allowed = {
            "quote": {"invoice", "delivery_note"},
            "delivery_note": {"invoice", "receipt"},
            "invoice": {"receipt"},
            "purchase_order": {"delivery_note", "invoice"},
        }
        if target not in allowed.get(source.get("document_type"), set()):
            raise FinanceConflictError(
                f"Cannot convert {source.get('document_type')} to {target}"
            )
        return self.draft_create(
            issuer_id=source["issuer_id"],
            customer_id=source["customer_id"],
            template_id=source["template_id"],
            template_version=source["template_version"],
            issue_date=source["issue_date"],
            service_period=source["service_period"],
            due_date=source["due_date"],
            currency=source["currency"],
            lines=copy.deepcopy(source["lines"]),
            notes=source.get("notes"),
            document_type=target,
            billing_key=billing_key,
            created_by=actor,
            contract_id=source.get("contract_id"),
            billing_rule_version_id=source.get("billing_rule_version_id"),
            withholding_tax_rate=source.get("withholding_tax_rate"),
            withholding_tax_base=source.get("withholding_tax_base") or "subtotal",
            converted_from={
                "id": source["id"],
                "document_type": source.get("document_type"),
                "revision": source.get("revision", 0),
            },
        )

    def correct_invoice(
        self,
        invoice_id: str,
        *,
        lines: list[dict[str, Any]],
        reason: str,
        corrected_by: str,
        correction_date: str | None = None,
        due_date: str | None = None,
        billing_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a correction draft while preserving the issued invoice."""

        actor = _text(corrected_by, field="corrected_by", required=True)
        source = self.core._invoice(invoice_id)
        if source.get("document_status") != "ISSUED":
            raise FinanceConflictError("Only ISSUED invoices can be corrected")
        correction_reason = _text(reason, field="reason", required=True, limit=1000)
        if not isinstance(lines, list) or not lines:
            raise FinanceValidationError("lines must contain at least one correction line")
        issue = _date(correction_date, field="correction_date") if correction_date else self.core._now()[:10]
        due = _date(due_date, field="due_date") if due_date else source["due_date"]
        if due < issue:
            due = issue
        return self.draft_create(
            issuer_id=source["issuer_id"],
            customer_id=source["customer_id"],
            template_id=source["template_id"],
            template_version=source["template_version"],
            issue_date=issue,
            service_period=source["service_period"],
            due_date=due,
            currency=source["currency"],
            lines=copy.deepcopy(lines),
            notes=source.get("notes"),
            document_type="correction_invoice",
            billing_key=billing_key,
            created_by=actor,
            contract_id=source.get("contract_id"),
            billing_rule_version_id=source.get("billing_rule_version_id"),
            withholding_tax_rate=source.get("withholding_tax_rate"),
            withholding_tax_base=source.get("withholding_tax_base") or "subtotal",
            correction_of=invoice_id,
            correction_reason=correction_reason,
        )

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        return self.core._invoice(invoice_id)

    def list_template_versions(self, template_id: str | None = None) -> list[dict[str, Any]]:
        return self.core.templates.list_versions(template_id)

    def list_audit_events(self, invoice_id: str) -> list[dict[str, Any]]:
        self.core._invoice(invoice_id)
        return self.core.store.list_audit_events(invoice_id)

    def _presentation(
        self,
        invoice: dict[str, Any],
        *,
        issuer: dict[str, Any] | None = None,
        customer: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        issuer = issuer or self.core._issuer(invoice["issuer_id"])
        customer = customer or self.core._customer(invoice["customer_id"])
        profile = customer["billing_profile"]
        result = copy.deepcopy(invoice)
        result["issuer"] = copy.deepcopy(issuer)
        result["customer"] = copy.deepcopy(profile)
        return result

    @staticmethod
    def _apply_withholding_tax(
        totals: dict[str, Any],
        *,
        currency: str,
        rounding: str,
        rate: str | Decimal | None,
        base: str,
    ) -> dict[str, Any]:
        normalized_base = str(base or "").strip().lower()
        if normalized_base not in {"subtotal", "total"}:
            raise FinanceValidationError("withholding_tax_base must be subtotal or total")
        try:
            normalized_rate = Decimal(str(rate or "0"))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise FinanceValidationError("withholding_tax_rate must be a valid decimal") from exc
        tax_base = Decimal(str(totals[normalized_base]))
        withholding = calculate_withholding_tax(
            tax_base,
            normalized_rate,
            currency=currency,
            rounding=rounding,
        )
        gross = Decimal(str(totals["total"]))
        net = gross - Decimal(str(withholding))
        result = dict(totals)
        result.update(
            {
                "withholding_tax_rate": _money_text(normalized_rate),
                "withholding_tax_base": normalized_base,
                "withholding_tax": withholding,
                "collectible_total": _money_text(net),
            }
        )
        return result
