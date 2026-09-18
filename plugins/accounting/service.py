"""Invoice lifecycle, document generation, approval, and payment tracking."""

from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable

from plugins.accounting.channel_selection import select_accounting_channel
from plugins.accounting.gemma import GemmaSuggestionProvider
from plugins.accounting.store import (
    AccountingStore,
    DuplicateInvoiceNumber,
    utc_now_iso,
)


_CENT = Decimal("0.01")
_MAX_TEXT_LENGTH = 1000
_MAX_PAYMENT_CSV_BYTES = 1_000_000
_MAX_PAYMENT_CSV_ROWS = 1000


class AccountingError(ValueError):
    """Raised for invalid accounting input or an invalid lifecycle transition."""


def _money(value: Any, *, field: str) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AccountingError(f"{field} must be a valid amount") from exc
    if not amount.is_finite():
        raise AccountingError(f"{field} must be a finite amount")
    return amount


def _parse_date(value: Any, *, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise AccountingError(f"{field} must use YYYY-MM-DD") from exc


def _today(value: Any = None) -> date:
    return _parse_date(value, field="today") if value else date.today()


def _safe_text(value: Any, *, field: str, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise AccountingError(f"{field} is required")
    if len(text) > _MAX_TEXT_LENGTH:
        raise AccountingError(f"{field} is too long")
    return text


def _safe_filename(value: str) -> str:
    filename = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return filename or "invoice"


def _write_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".invoice-", suffix=".tmp")
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def render_invoice_html(invoice: dict[str, Any], *, company: dict[str, Any] | None = None) -> str:
    """Render a printable invoice without interpolating unescaped user data."""

    company = company or {}
    esc = lambda value: html.escape(str(value or ""), quote=True)
    rows = []
    for item in invoice["items"]:
        rows.append(
            "<tr>"
            f"<td>{esc(item['description'])}</td>"
            f"<td class=number>{esc(item['quantity'])}</td>"
            f"<td class=number>{esc(item['unit_price'])}</td>"
            f"<td class=number>{esc(item['line_total'])}</td>"
            "</tr>"
        )
    company_block = "<br>".join(
        esc(company.get(key))
        for key in ("name", "address", "registration_number")
        if company.get(key)
    )
    return f"""<!doctype html>
<html lang="ja">
<head><meta charset="utf-8"><title>Invoice {esc(invoice['invoice_number'])}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; color: #222; margin: 3rem; }}
h1 {{ margin-bottom: .25rem; }}
.meta {{ color: #555; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 2rem; }}
th, td {{ border-bottom: 1px solid #ddd; padding: .65rem; text-align: left; }}
.number {{ text-align: right; }}
.totals {{ margin: 1.5rem 0 0 auto; width: 20rem; }}
.totals div {{ display: flex; justify-content: space-between; padding: .25rem 0; }}
.grand-total {{ border-top: 2px solid #222; font-weight: bold; }}
</style></head>
<body>
<h1>請求書</h1>
<div class="meta">請求書番号: {esc(invoice['invoice_number'])}</div>
<div class="meta">発行日: {esc(invoice['issue_date'])}　支払期限: {esc(invoice['due_date'])}</div>
<hr>
<p><strong>請求先</strong><br>{esc(invoice['customer']['name'])}<br>{esc(invoice['customer'].get('address'))}</p>
<p><strong>請求元</strong><br>{company_block}</p>
<table><thead><tr><th>内容</th><th class=number>数量</th><th class=number>単価</th><th class=number>金額</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<div class="totals">
<div><span>小計</span><span>{esc(invoice['subtotal'])} {esc(invoice['currency'])}</span></div>
<div><span>消費税</span><span>{esc(invoice['tax'])} {esc(invoice['currency'])}</span></div>
<div class="grand-total"><span>合計</span><span>{esc(invoice['total'])} {esc(invoice['currency'])}</span></div>
</div>
<p>{esc(invoice.get('notes'))}</p>
</body></html>
"""


class AccountingService:
    def __init__(
        self,
        store: AccountingStore,
        *,
        channel_provider: Callable[[], Any] | None = None,
        send_message: Callable[..., Any] | None = None,
        config: dict[str, Any] | None = None,
        suggestion_provider: GemmaSuggestionProvider | None = None,
    ):
        self.store = store
        self.channel_provider = channel_provider or self._default_channel_provider
        self.send_message = send_message or self._default_send_message
        self.config = dict(config or {})
        self.suggestion_provider = suggestion_provider

    @staticmethod
    def _default_channel_provider() -> list[dict[str, Any]]:
        from gateway.channel_directory import load_directory

        return load_directory().get("platforms", {}).get("discord", [])

    @staticmethod
    def _default_send_message(**payload: Any) -> Any:
        from tools.registry import registry

        result = registry.dispatch("send_message", payload)
        if isinstance(result, str):
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                return {"error": result}
        return result

    def _channel(self, hint: str | None = None) -> dict[str, Any] | None:
        channels = self.channel_provider()
        if isinstance(channels, dict):
            channels = channels.get("platforms", {}).get("discord", [])
        if not isinstance(channels, list):
            return None
        return select_accounting_channel(
            channels,
            hint=hint or self.config.get("channel_hint"),
        )

    def _get(self, invoice_id: str) -> dict[str, Any]:
        invoice = self.store.get(invoice_id)
        if invoice is None:
            raise AccountingError(f"Invoice not found: {invoice_id}")
        return invoice

    def create_invoice(
        self,
        *,
        customer: dict[str, Any],
        items: list[dict[str, Any]],
        currency: str,
        due_date: str,
        issue_date: str | None = None,
        invoice_number: str | None = None,
        notes: str | None = None,
        channel_hint: str | None = None,
        recurrence: dict[str, str] | None = None,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(customer, dict):
            raise AccountingError("customer must be an object")
        customer_name = _safe_text(customer.get("name"), field="customer.name", required=True)
        normalized_customer = {"name": customer_name}
        for field in ("email", "address", "tax_id"):
            value = _safe_text(customer.get(field), field=f"customer.{field}")
            if value:
                normalized_customer[field] = value

        if not isinstance(items, list) or not items:
            raise AccountingError("items must contain at least one item")
        if len(items) > 100:
            raise AccountingError("items may contain at most 100 entries")

        normalized_items = []
        subtotal = Decimal("0")
        tax = Decimal("0")
        for index, raw_item in enumerate(items, start=1):
            if not isinstance(raw_item, dict):
                raise AccountingError(f"items[{index}] must be an object")
            description = _safe_text(
                raw_item.get("description"),
                field=f"items[{index}].description",
                required=True,
            )
            quantity = _money(raw_item.get("quantity", 1), field=f"items[{index}].quantity")
            unit_price = _money(raw_item.get("unit_price"), field=f"items[{index}].unit_price")
            tax_rate = _money(raw_item.get("tax_rate", 0), field=f"items[{index}].tax_rate")
            if quantity <= 0 or unit_price < 0 or tax_rate < 0 or tax_rate > 100:
                raise AccountingError(f"items[{index}] contains an invalid quantity, price, or tax rate")
            line_subtotal = (quantity * unit_price).quantize(_CENT, rounding=ROUND_HALF_UP)
            line_tax = (line_subtotal * tax_rate / Decimal("100")).quantize(
                _CENT, rounding=ROUND_HALF_UP
            )
            line_total = line_subtotal + line_tax
            subtotal += line_subtotal
            tax += line_tax
            normalized_items.append(
                {
                    "description": description,
                    "quantity": f"{quantity:.2f}",
                    "unit_price": f"{unit_price:.2f}",
                    "tax_rate": f"{tax_rate:.2f}",
                    "line_subtotal": f"{line_subtotal:.2f}",
                    "line_tax": f"{line_tax:.2f}",
                    "line_total": f"{line_total:.2f}",
                }
            )

        issue = _parse_date(issue_date or date.today().isoformat(), field="issue_date")
        due = _parse_date(due_date, field="due_date")
        if due < issue:
            raise AccountingError("due_date cannot be earlier than issue_date")
        normalized_currency = _safe_text(currency, field="currency", required=True).upper()
        if not re.fullmatch(r"[A-Z]{3}", normalized_currency):
            raise AccountingError("currency must be a 3-letter ISO code")
        number = _safe_text(invoice_number, field="invoice_number") if invoice_number else ""
        if number and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", number):
            raise AccountingError("invoice_number contains invalid characters")
        if not number:
            number = f"INV-{issue:%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"

        channel = self._channel(channel_hint)
        invoice_id = f"inv_{uuid.uuid4().hex}"
        record = {
            "id": invoice_id,
            "invoice_number": number,
            "status": "draft",
            "customer": normalized_customer,
            "items": normalized_items,
            "currency": normalized_currency,
            "issue_date": issue.isoformat(),
            "due_date": due.isoformat(),
            "notes": _safe_text(notes, field="notes"),
            "subtotal": f"{subtotal:.2f}",
            "tax": f"{tax:.2f}",
            "total": f"{(subtotal + tax):.2f}",
            "paid_amount": "0.00",
            "payments": [],
            "channel": channel,
            "document_path": str(
                self.store.documents_dir / f"{_safe_filename(number)}-{invoice_id}.html"
            ),
        }
        if recurrence is not None:
            record["recurrence"] = dict(recurrence)
        try:
            self.store.create(record, actor=actor_id)
        except DuplicateInvoiceNumber as exc:
            raise AccountingError(str(exc)) from exc
        _write_private_text(
            Path(record["document_path"]),
            render_invoice_html(record, company=self.config.get("company")),
        )
        return record

    def create_recurring_invoice(
        self,
        *,
        template_id: str,
        period: str,
        customer: dict[str, Any],
        items: list[dict[str, Any]],
        currency: str,
        issue_day: int = 1,
        due_days: int = 30,
        invoice_number_prefix: str | None = None,
        notes: str | None = None,
        channel_hint: str | None = None,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        """Create one draft invoice for a recurring template and period."""

        template = _safe_text(template_id, field="template_id", required=True)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", template):
            raise AccountingError("template_id contains invalid characters")
        period = _safe_text(period, field="period", required=True)
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", period):
            raise AccountingError("period must use YYYY-MM")
        try:
            month_start = date.fromisoformat(f"{period}-01")
        except ValueError as exc:
            raise AccountingError("period must use a valid YYYY-MM month") from exc

        try:
            issue_day = int(issue_day)
        except (TypeError, ValueError) as exc:
            raise AccountingError("issue_day must be an integer") from exc
        if issue_day < 1 or issue_day > 28:
            raise AccountingError("issue_day must be between 1 and 28")
        try:
            due_days = int(due_days)
        except (TypeError, ValueError) as exc:
            raise AccountingError("due_days must be an integer") from exc
        if due_days < 0 or due_days > 365:
            raise AccountingError("due_days must be between 0 and 365")

        prefix = _safe_text(
            invoice_number_prefix or template,
            field="invoice_number_prefix",
            required=True,
        )
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", prefix):
            raise AccountingError("invoice_number_prefix contains invalid characters")
        invoice_number = f"{prefix}-{period.replace('-', '')}"
        issue_date = date(month_start.year, month_start.month, issue_day)
        due_date = issue_date + timedelta(days=due_days)
        recurrence = {"template_id": template, "period": period}
        try:
            return self.create_invoice(
                customer=customer,
                items=items,
                currency=currency,
                issue_date=issue_date.isoformat(),
                due_date=due_date.isoformat(),
                invoice_number=invoice_number,
                notes=notes,
                channel_hint=channel_hint,
                recurrence=recurrence,
                actor_id=actor_id,
            )
        except AccountingError:
            existing = self.store.find_by_invoice_number(invoice_number)
            if existing and existing.get("recurrence") == recurrence:
                return existing
            raise

    def list_channels(self, *, channel_hint: str | None = None) -> dict[str, Any]:
        channels = self.channel_provider()
        if isinstance(channels, dict):
            channels = channels.get("platforms", {}).get("discord", [])
        if not isinstance(channels, list):
            channels = []
        selected = self._channel(channel_hint)
        return {
            "success": selected is not None,
            "channels": [
                {
                    key: value
                    for key, value in channel.items()
                    if key in {"id", "name", "guild", "type", "topic"}
                }
                for channel in channels
                if isinstance(channel, dict)
            ],
            "selected": selected,
            "error": None if selected else "No accounting-purpose Discord channel found",
        }

    def prepare_invoice(
        self,
        invoice_id: str,
        *,
        channel_hint: str | None = None,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        invoice = self._get(invoice_id)
        if invoice["status"] == "pending_approval":
            return invoice
        if invoice["status"] != "draft":
            raise AccountingError(
                f"Invoice {invoice_id} cannot be prepared from status {invoice['status']}"
            )
        channel = invoice.get("channel") or self._channel(channel_hint)
        if channel is None:
            raise AccountingError(
                "No accounting-purpose Discord channel found; configure channel_hint "
                "or create a channel whose name/topic includes accounting, finance, invoice, or 経理."
            )
        return self.store.update(
            invoice_id,
            {
                "status": "pending_approval",
                "channel": channel,
                "approval_requested_at": utc_now_iso(),
            },
            action="prepare_for_approval",
            actor=actor_id,
        ) or invoice

    def approve_invoice(
        self,
        invoice_id: str,
        *,
        approved_by: str | None = None,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        invoice = self._get(invoice_id)
        if invoice["status"] == "sent":
            return {"success": True, "invoice": invoice, "already_sent": True}
        if invoice["status"] == "approved":
            return self.send_invoice(invoice_id, actor_id=actor_id)
        if invoice["status"] != "pending_approval":
            return {
                "success": False,
                "error": f"Invoice must be pending approval, current status is {invoice['status']}",
            }
        configured_approver = str(self.config.get("approver_user_id") or "").strip()
        if not configured_approver:
            return {
                "success": False,
                "error": "No approver_user_id is configured for accounting approvals",
            }
        authenticated_actor = str(actor_id or "").strip()
        if not authenticated_actor:
            return {
                "success": False,
                "error": "An authenticated approver identity is required",
            }
        if authenticated_actor != configured_approver:
            return {"success": False, "error": "The approver is not authorized for accounting approvals"}
        approved = self.store.update_if_status(
            invoice_id,
            "pending_approval",
            {
                "status": "approved",
                "approved_at": utc_now_iso(),
                "approved_by": authenticated_actor,
            },
            action="approve_send",
            actor=authenticated_actor,
        )
        if approved is None:
            return {
                "success": False,
                "error": "Invoice approval was already handled by another operation",
            }
        return self.send_invoice(invoice_id, actor_id=authenticated_actor)

    def send_invoice(
        self,
        invoice_id: str,
        *,
        force_retry: bool = False,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        invoice = self._get(invoice_id)
        if invoice["status"] != "approved":
            return {
                "success": False,
                "error": "Invoice sending requires an explicit approval first",
            }
        if invoice.get("delivery_uncertain") and not force_retry:
            return {
                "success": False,
                "error": "Delivery status is uncertain; verify Discord history before an explicit retry",
            }
        if force_retry:
            configured_approver = str(self.config.get("approver_user_id") or "").strip()
            authenticated_actor = str(actor_id or "").strip()
            if not configured_approver or not authenticated_actor:
                return {
                    "success": False,
                    "error": "An authenticated approver identity is required for an explicit retry",
                }
            if authenticated_actor != configured_approver:
                return {"success": False, "error": "The approver is not authorized for accounting approvals"}
        channel = invoice.get("channel") or {}
        channel_id = str(channel.get("id") or "").strip()
        document_path = Path(str(invoice.get("document_path") or ""))
        if not channel_id or not document_path.is_file():
            return {"success": False, "error": "Invoice delivery target or document is missing"}
        claimed = self.store.update_if_status(
            invoice_id,
            "approved",
            {"status": "sending", "send_started_at": utc_now_iso()},
            action="begin_send",
            actor=actor_id,
        )
        if claimed is None:
            return {
                "success": False,
                "error": "Invoice delivery is already in progress or was handled by another operation",
            }
        try:
            result = self.send_message(
                target=f"discord:{channel_id}",
                message=(
                    f"請求書 {invoice['invoice_number']} を送付します。"
                    f"\n請求先: {invoice['customer']['name']}"
                    f"\n金額: {invoice['total']} {invoice['currency']}"
                    f"\n支払期限: {invoice['due_date']}"
                    f"\nMEDIA:{document_path}"
                ),
            )
        except Exception as exc:
            self.store.update_if_status(
                invoice_id,
                "sending",
                {"status": "approved", "send_error": type(exc).__name__},
                action="send_failed",
                actor=actor_id,
            )
            return {"success": False, "error": "Discord delivery failed; approval is preserved for retry"}
        if not isinstance(result, dict) or not result.get("success"):
            error = result.get("error", "Discord delivery failed") if isinstance(result, dict) else str(result)
            self.store.update_if_status(
                invoice_id,
                "sending",
                {"status": "approved", "send_error": error},
                action="send_failed",
                actor=actor_id,
            )
            return {"success": False, "error": error, "invoice": invoice}
        updated = self.store.update_if_status(
            invoice_id,
            "sending",
            {
                "status": "sent",
                "sent_at": utc_now_iso(),
                "delivery": result,
                "delivery_uncertain": False,
            },
            action="send_invoice",
            actor=actor_id,
        )
        if updated is None:
            return {
                "success": False,
                "error": "Discord delivery succeeded but invoice state changed; verify Discord history",
                "delivery": result,
            }
        return {"success": True, "invoice": updated, "delivery": result}

    def recover_send(
        self,
        invoice_id: str,
        *,
        max_age_minutes: int = 30,
        now: str | None = None,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        """Move a stale in-flight delivery back to approval for manual review."""

        try:
            max_age_minutes = int(max_age_minutes)
        except (TypeError, ValueError) as exc:
            raise AccountingError("max_age_minutes must be an integer") from exc
        if max_age_minutes < 30 or max_age_minutes > 10_080:
            raise AccountingError("max_age_minutes must be between 30 and 10080")

        configured_approver = str(self.config.get("approver_user_id") or "").strip()
        authenticated_actor = str(actor_id or "").strip()
        if not configured_approver or not authenticated_actor:
            return {
                "success": False,
                "error": "An authenticated approver identity is required for send recovery",
            }
        if authenticated_actor != configured_approver:
            return {"success": False, "error": "The approver is not authorized for accounting approvals"}

        invoice = self._get(invoice_id)
        if invoice["status"] != "sending":
            return {
                "success": False,
                "error": f"Invoice is not sending; current status is {invoice['status']}",
            }
        try:
            started_at = datetime.fromisoformat(str(invoice.get("send_started_at")))
            current = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
        except (TypeError, ValueError) as exc:
            raise AccountingError("send timestamps must be valid ISO datetimes") from exc
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        age_seconds = max((current - started_at).total_seconds(), 0)
        if age_seconds < max_age_minutes * 60:
            return {
                "success": False,
                "error": "Invoice delivery is still within its recovery window",
            }
        recovered = self.store.update_if_status(
            invoice_id,
            "sending",
            {
                "status": "approved",
                "delivery_uncertain": True,
                "send_recovered_at": utc_now_iso(),
                "send_error": "Delivery status uncertain; verify Discord before retry",
            },
            action="recover_send",
            actor=actor_id,
        )
        if recovered is None:
            return {"success": False, "error": "Invoice delivery state changed during recovery"}
        return {
            "success": True,
            "invoice": recovered,
            "requires_delivery_verification": True,
        }

    def check_payment(self, invoice_id: str, *, today: str | None = None) -> dict[str, Any]:
        invoice = self._get(invoice_id)
        total = _money(invoice["total"], field="total")
        paid = _money(invoice.get("paid_amount", "0"), field="paid_amount")
        outstanding = max(total - paid, Decimal("0"))
        due = _parse_date(invoice["due_date"], field="due_date")
        current = _today(today)
        return {
            "success": True,
            "invoice_id": invoice_id,
            "invoice_number": invoice["invoice_number"],
            "status": invoice["status"],
            "total": f"{total:.2f}",
            "paid_amount": f"{paid:.2f}",
            "outstanding": f"{outstanding:.2f}",
            "overdue": outstanding > 0 and due < current,
            "payment_reference": invoice.get("payment_reference"),
            "payments": invoice.get("payments", []),
        }

    def record_payment(
        self,
        invoice_id: str,
        *,
        amount: str,
        payment_date: str,
        reference: str = "",
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        amount_value = _money(amount, field="amount")
        if amount_value <= 0:
            raise AccountingError("amount must be greater than zero")
        payment_day = _parse_date(payment_date, field="payment_date")
        reference = _safe_text(reference, field="reference")
        for _attempt in range(3):
            invoice = self._get(invoice_id)
            if invoice["status"] not in {"sent", "partially_paid"}:
                raise AccountingError("Only sent invoices can receive a payment")
            payments = list(invoice.get("payments") or [])
            if reference and any(item.get("reference") == reference for item in payments):
                raise AccountingError("A payment with this reference is already recorded")
            total = _money(invoice["total"], field="total")
            paid_before = _money(invoice.get("paid_amount", "0"), field="paid_amount")
            if paid_before + amount_value > total:
                raise AccountingError("Payment amount would exceed the invoice total")
            paid = paid_before + amount_value
            status = "paid" if paid >= total else "partially_paid"
            payment = {
                "amount": f"{amount_value:.2f}",
                "payment_date": payment_day.isoformat(),
                "reference": reference or None,
            }
            payments.append(payment)
            updated = self.store.update_if_revision(
                invoice_id,
                int(invoice.get("revision", 0)),
                {
                    "status": status,
                    "paid_amount": f"{paid:.2f}",
                    "payments": payments,
                    "payment_reference": reference or invoice.get("payment_reference"),
                    "paid_at": utc_now_iso() if status == "paid" else invoice.get("paid_at"),
                },
                action="record_payment",
                actor=actor_id,
                detail={"amount": f"{amount_value:.2f}", "reference": reference or None},
            )
            if updated is not None:
                result = self.check_payment(invoice_id)
                result["invoice"] = updated
                return result
        raise AccountingError("Payment record changed concurrently; retry the operation")

    def import_payments_csv(
        self,
        *,
        csv_text: str,
        apply: bool = False,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        """Preview or atomically apply payments from a canonical CSV export."""

        if not isinstance(csv_text, str):
            raise AccountingError("payment CSV must be text")
        if len(csv_text.encode("utf-8")) > _MAX_PAYMENT_CSV_BYTES:
            raise AccountingError("payment CSV is too large")
        if "\x00" in csv_text:
            raise AccountingError("payment CSV contains an invalid character")

        if apply:
            configured_approver = str(self.config.get("approver_user_id") or "").strip()
            authenticated_actor = str(actor_id or "").strip()
            if not configured_approver or not authenticated_actor:
                return {
                    "success": False,
                    "dry_run": False,
                    "applied_count": 0,
                    "rows": [],
                    "errors": [{"error": "An authenticated approver identity is required for CSV import"}],
                }
            if authenticated_actor != configured_approver:
                return {
                    "success": False,
                    "dry_run": False,
                    "applied_count": 0,
                    "rows": [],
                    "errors": [{"error": "The approver is not authorized for CSV import"}],
                }

        try:
            reader = csv.DictReader(io.StringIO(csv_text, newline=""))
            raw_headers = reader.fieldnames or []
        except csv.Error as exc:
            raise AccountingError(f"payment CSV is invalid: {exc}") from exc
        headers = [str(header or "").strip().casefold() for header in raw_headers]
        required_headers = {"invoice_number", "amount", "payment_date", "reference"}
        if len(headers) != len(set(headers)):
            raise AccountingError("payment CSV contains duplicate headers")
        missing_headers = sorted(required_headers - set(headers))
        if missing_headers:
            raise AccountingError(
                "payment CSV is missing required headers: " + ", ".join(missing_headers)
            )

        rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        grouped: dict[str, dict[str, Any]] = {}
        seen_references: dict[str, int] = {}
        data_row_count = 0

        try:
            for raw_row in reader:
                if all(not str(value or "").strip() for value in raw_row.values() if value is not None):
                    continue
                data_row_count += 1
                row_number = reader.line_num
                if data_row_count > _MAX_PAYMENT_CSV_ROWS:
                    errors.append({"row": row_number, "error": "payment CSV has too many rows"})
                    break
                row = {
                    headers[index]: value
                    for index, (_, value) in enumerate(raw_row.items())
                    if index < len(headers)
                }
                try:
                    invoice_number = _safe_text(
                        row.get("invoice_number"),
                        field="invoice_number",
                        required=True,
                    )
                    amount = _money(row.get("amount"), field="amount")
                    if amount <= 0:
                        raise AccountingError("amount must be greater than zero")
                    payment_day = _parse_date(row.get("payment_date"), field="payment_date")
                    reference = _safe_text(
                        row.get("reference"),
                        field="reference",
                        required=True,
                    )
                    invoice = self.store.find_by_invoice_number(invoice_number)
                    if invoice is None:
                        raise AccountingError(f"Invoice not found: {invoice_number}")
                    if invoice["status"] not in {"sent", "partially_paid"}:
                        raise AccountingError(
                            f"Invoice {invoice_number} cannot receive a payment from status "
                            f"{invoice['status']}"
                        )
                    if reference in seen_references:
                        raise AccountingError(
                            f"Payment reference is duplicated in CSV row {seen_references[reference]}"
                        )
                    if any(item.get("reference") == reference for item in invoice.get("payments", [])):
                        raise AccountingError("A payment with this reference is already recorded")
                except AccountingError as exc:
                    errors.append({"row": row_number, "error": str(exc)})
                    continue

                seen_references[reference] = row_number
                payment = {
                    "amount": f"{amount:.2f}",
                    "payment_date": payment_day.isoformat(),
                    "reference": reference,
                }
                row_result = {
                    "row": row_number,
                    "invoice_id": invoice["id"],
                    "invoice_number": invoice_number,
                    "amount": f"{amount:.2f}",
                    "payment_date": payment_day.isoformat(),
                    "reference": reference,
                    "status": "ready",
                }
                rows.append(row_result)
                group = grouped.setdefault(
                    invoice["id"],
                    {"invoice": invoice, "payments": [], "amount": Decimal("0"), "rows": []},
                )
                group["payments"].append(payment)
                group["amount"] += amount
                group["rows"].append(row_result)
        except csv.Error as exc:
            errors.append({"row": reader.line_num, "error": f"payment CSV is invalid: {exc}"})

        if data_row_count == 0:
            errors.append({"error": "payment CSV contains no payment rows"})

        changes: list[dict[str, Any]] = []
        for group in grouped.values():
            invoice = group["invoice"]
            total = _money(invoice["total"], field="total")
            paid_before = _money(invoice.get("paid_amount", "0"), field="paid_amount")
            paid = paid_before + group["amount"]
            if paid > total:
                error = "Payment amount would exceed the invoice total"
                errors.append(
                    {
                        "row": group["rows"][0]["row"],
                        "invoice_number": invoice["invoice_number"],
                        "error": error,
                    }
                )
                for row_result in group["rows"]:
                    row_result["status"] = "error"
                continue
            status = "paid" if paid >= total else "partially_paid"
            changes.append(
                {
                    "invoice_id": invoice["id"],
                    "expected_revision": int(invoice.get("revision", 0)),
                    "updates": {
                        "status": status,
                        "paid_amount": f"{paid:.2f}",
                        "payments": list(invoice.get("payments") or []) + group["payments"],
                        "payment_reference": group["payments"][-1]["reference"],
                        "paid_at": utc_now_iso() if status == "paid" else invoice.get("paid_at"),
                    },
                    "action": "import_payments",
                    "actor": actor_id,
                    "detail": {"source": "payment_csv", "row_count": len(group["rows"])},
                }
            )

        if errors:
            return {
                "success": False,
                "dry_run": not apply,
                "applied_count": 0,
                "rows": rows,
                "errors": errors,
            }
        if not changes:
            return {
                "success": False,
                "dry_run": not apply,
                "applied_count": 0,
                "rows": rows,
                "errors": [{"error": "payment CSV contains no applicable payments"}],
            }
        if not apply:
            return {
                "success": True,
                "dry_run": True,
                "applied_count": 0,
                "rows": rows,
                "errors": [],
            }

        updated = self.store.update_many_if_revisions(changes)
        if updated is None:
            return {
                "success": False,
                "dry_run": False,
                "applied_count": 0,
                "rows": rows,
                "errors": [{"error": "Payment records changed concurrently; preview again"}],
            }
        for row_result in rows:
            row_result["status"] = "applied"
        return {
            "success": True,
            "dry_run": False,
            "applied_count": len(rows),
            "rows": rows,
            "invoices": updated,
            "errors": [],
        }

    def remind_due(
        self,
        *,
        today: str | None = None,
        reminder_days: int = 7,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        current = _today(today)
        try:
            reminder_days = int(reminder_days)
        except (TypeError, ValueError) as exc:
            raise AccountingError("reminder_days must be an integer") from exc
        if reminder_days < 0 or reminder_days > 365:
            raise AccountingError("reminder_days must be between 0 and 365")
        deadline = current + timedelta(days=reminder_days)
        sent_count = 0
        errors: list[dict[str, str]] = []
        for invoice in self.store.list(statuses={"draft", "pending_approval"}):
            due = _parse_date(invoice["due_date"], field="due_date")
            if due > deadline or invoice.get("last_reminder_on") == current.isoformat():
                continue
            channel = invoice.get("channel") or self._channel()
            if channel is None:
                errors.append({"invoice_id": invoice["id"], "error": "No accounting Discord channel"})
                continue
            channel_id = str(channel.get("id") or "").strip()
            if not channel_id:
                errors.append({"invoice_id": invoice["id"], "error": "Reminder channel is missing"})
                continue
            claimed = self.store.claim_reminder(
                invoice["id"],
                current.isoformat(),
                actor=actor_id,
            )
            if claimed is None:
                continue
            try:
                result = self.send_message(
                    target=f"discord:{channel_id}",
                    message=(
                        f"請求書送付リマインダー: {invoice['invoice_number']}"
                        f"\n請求先: {invoice['customer']['name']}"
                        f"\n支払期限: {invoice['due_date']}"
                        "\n内容を確認し、問題なければ請求書を承認してください。"
                    ),
                )
            except Exception as exc:
                result = {"success": False, "error": f"Reminder delivery failed: {type(exc).__name__}"}
            if isinstance(result, dict) and result.get("success"):
                marked = self.store.update_if_revision(
                    invoice["id"],
                    int(claimed.get("revision", 0)),
                    {
                        "last_reminder_on": current.isoformat(),
                        "channel": channel,
                        "reminder_claim_on": None,
                        "reminder_claimed_at": None,
                    },
                    action="send_reminder",
                    actor=actor_id,
                )
                if marked is not None:
                    sent_count += 1
                else:
                    errors.append(
                        {"invoice_id": invoice["id"], "error": "Reminder state update failed after delivery"}
                    )
            else:
                self.store.update_if_revision(
                    invoice["id"],
                    int(claimed.get("revision", 0)),
                    {"reminder_claim_on": None, "reminder_claimed_at": None},
                    action="reminder_failed",
                    actor=actor_id,
                )
                errors.append(
                    {
                        "invoice_id": invoice["id"],
                        "error": result.get("error", "Reminder delivery failed")
                        if isinstance(result, dict)
                        else str(result),
                    }
                )
        return {
            "success": not errors,
            "sent_count": sent_count,
            "errors": errors,
            "today": current.isoformat(),
        }

    def list_invoices(self) -> list[dict[str, Any]]:
        return self.store.list()

    def list_events(self, invoice_id: str) -> dict[str, Any]:
        self._get(invoice_id)
        return {"success": True, "invoice_id": invoice_id, "events": self.store.events(invoice_id)}

    def suggest_billing_run_explanation(self, preview: Any) -> dict[str, Any]:
        """Return an optional advisory explanation without changing accounting state."""

        if self.suggestion_provider is None:
            return {"success": False, "skipped": True, "reason": "disabled"}
        return self.suggestion_provider.explain_billing_run(preview)

    def draft_invoice_reminder(self, invoice_summary: Any) -> dict[str, Any]:
        """Draft reminder prose only; delivery remains a separate approved action."""

        if self.suggestion_provider is None:
            return {"success": False, "skipped": True, "reason": "disabled"}
        return self.suggestion_provider.draft_invoice_reminder(invoice_summary)

    def handle(self, action: str, **args: Any) -> dict[str, Any]:
        """Dispatch the structured accounting tool action."""
        try:
            if action == "list_channels":
                return self.list_channels(channel_hint=args.get("channel_hint"))
            if action == "create_invoice":
                return {
                    "success": True,
                    "invoice": self.create_invoice(
                        customer=args.get("customer"),
                        items=args.get("items"),
                        currency=args.get("currency"),
                        due_date=args.get("due_date"),
                        issue_date=args.get("issue_date"),
                        invoice_number=args.get("invoice_number"),
                        notes=args.get("notes"),
                        channel_hint=args.get("channel_hint"),
                        actor_id=args.get("_actor_id"),
                    ),
                }
            if action == "create_recurring_invoice":
                return {
                    "success": True,
                    "invoice": self.create_recurring_invoice(
                        template_id=args.get("template_id"),
                        period=args.get("period"),
                        customer=args.get("customer"),
                        items=args.get("items"),
                        currency=args.get("currency"),
                        issue_day=args.get("issue_day", 1),
                        due_days=args.get("due_days", 30),
                        invoice_number_prefix=args.get("invoice_number_prefix"),
                        notes=args.get("notes"),
                        channel_hint=args.get("channel_hint"),
                        actor_id=args.get("_actor_id"),
                    ),
                }
            if action == "prepare_invoice":
                return {
                    "success": True,
                    "invoice": self.prepare_invoice(
                        args.get("invoice_id"),
                        channel_hint=args.get("channel_hint"),
                        actor_id=args.get("_actor_id"),
                    ),
                }
            if action == "approve_invoice":
                return self.approve_invoice(
                    args.get("invoice_id"),
                    approved_by=args.get("approved_by"),
                    actor_id=args.get("_actor_id"),
                )
            if action == "send_invoice":
                return self.send_invoice(
                    args.get("invoice_id"),
                    force_retry=bool(args.get("force_retry", False)),
                    actor_id=args.get("_actor_id"),
                )
            if action == "recover_send":
                return self.recover_send(
                    args.get("invoice_id"),
                    max_age_minutes=args.get("max_age_minutes", 30),
                    actor_id=args.get("_actor_id"),
                )
            if action == "check_payment":
                return self.check_payment(args.get("invoice_id"), today=args.get("today"))
            if action == "record_payment":
                return self.record_payment(
                    args.get("invoice_id"),
                    amount=args.get("amount"),
                    payment_date=args.get("payment_date"),
                    reference=args.get("payment_reference", ""),
                    actor_id=args.get("_actor_id"),
                )
            if action == "import_payments":
                return self.import_payments_csv(
                    csv_text=args.get("payment_csv"),
                    apply=bool(args.get("apply", False)),
                    actor_id=args.get("_actor_id"),
                )
            if action == "remind_due":
                return self.remind_due(
                    today=args.get("today"),
                    reminder_days=args.get("reminder_days", 7),
                    actor_id=args.get("_actor_id"),
                )
            if action == "list_invoices":
                return {"success": True, "invoices": self.list_invoices()}
            if action == "list_events":
                return self.list_events(args.get("invoice_id"))
            if action == "suggest_billing_run_explanation":
                return self.suggest_billing_run_explanation(args.get("billing_run_preview"))
            if action == "draft_invoice_reminder":
                return self.draft_invoice_reminder(args.get("invoice_summary"))
            return {"success": False, "error": f"Unknown accounting action: {action}"}
        except (AccountingError, TypeError, KeyError) as exc:
            return {"success": False, "error": str(exc)}
