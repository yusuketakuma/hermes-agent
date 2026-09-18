"""Delivery outbox, retry, and approved email dispatch operations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import ssl
import uuid
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Protocol, TYPE_CHECKING

from finance_core.errors import FinanceConflictError, FinanceNotFoundError, FinanceValidationError

if TYPE_CHECKING:
    from finance_core.core import FinanceCore


_DELIVERY_METHODS = frozenset({"email", "web", "postal", "discord"})
_DELIVERY_STATUSES = frozenset({"QUEUED", "SENDING", "SENT", "DELIVERED", "FAILED"})


def _text(value: Any, *, field: str, required: bool = False, limit: int = 1000) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise FinanceValidationError(f"{field} is required")
    if len(result) > limit:
        raise FinanceValidationError(f"{field} is too long")
    return result


class EmailTransport(Protocol):
    """Minimal transport contract; providers must not receive the invoice DB."""

    def send(
        self,
        *,
        recipient: str,
        subject: str,
        body: str,
        attachment_path: str,
        attachment_sha256: str,
    ) -> dict[str, Any]: ...


class DiscordTransport(Protocol):
    """Transport contract for a single approved Discord delivery."""

    def send(
        self,
        *,
        recipient: str,
        subject: str,
        body: str,
        attachment_path: str,
        attachment_sha256: str,
    ) -> dict[str, Any]: ...


class DisabledEmailTransport:
    """Safe default used until an email provider is explicitly configured."""

    def send(self, **_: Any) -> dict[str, Any]:
        raise FinanceConflictError("Email delivery adapter is not configured")


def _verified_document(attachment_path: str, attachment_sha256: str) -> Path:
    path = Path(attachment_path)
    if not path.is_file():
        raise FinanceValidationError("Issued invoice PDF is missing")
    if hashlib.sha256(path.read_bytes()).hexdigest() != str(attachment_sha256):
        raise FinanceConflictError("Issued invoice PDF hash does not match the stored artifact")
    return path


class DiscordDeliveryTransport:
    """Send an issued invoice through Hermes' shared Discord transport."""

    def send(
        self,
        *,
        recipient: str,
        subject: str,
        body: str,
        attachment_path: str,
        attachment_sha256: str,
    ) -> dict[str, Any]:
        channel_id = str(recipient or "").strip()
        if not re.fullmatch(r"[0-9]{5,30}", channel_id):
            raise FinanceValidationError("Discord delivery recipient must be a channel ID")
        path = _verified_document(attachment_path, attachment_sha256)
        message = (
            f"{subject}\n{body}\n"
            f"MEDIA:{path}\n[[as_document]]"
        )
        try:
            from tools.send_message_tool import _handle_send

            raw = _handle_send(
                {
                    "action": "send",
                    "target": f"discord:{channel_id}",
                    "message": message,
                }
            )
            result = raw if isinstance(raw, dict) else json.loads(str(raw))
        except FinanceConflictError:
            raise
        except Exception as exc:
            raise FinanceConflictError("Discord delivery adapter failed") from exc
        if not isinstance(result, dict) or result.get("success") is False or result.get("error"):
            raise FinanceConflictError("Discord delivery adapter rejected the message")
        message_id = str(result.get("message_id") or result.get("id") or "").strip()
        if not message_id:
            raise FinanceConflictError("Discord delivery adapter returned no message ID")
        return {"provider": "discord", "message_id": message_id}


class SmtpEmailTransport:
    """SMTP adapter using the existing Hermes email secret variables."""

    def __init__(
        self,
        *,
        sender: str,
        password: str,
        host: str,
        port: int = 587,
        timeout: float = 30.0,
    ):
        self.sender = str(sender).strip()
        self.password = str(password)
        self.host = str(host).strip()
        self.port = int(port)
        self.timeout = float(timeout)
        if not self.sender or not self.password or not self.host:
            raise FinanceValidationError("SMTP sender, password, and host are required")
        if any(marker in self.sender or marker in self.host for marker in ("\r", "\n")):
            raise FinanceValidationError("SMTP sender and host contain invalid header characters")
        if not 1 <= self.port <= 65535:
            raise FinanceValidationError("SMTP port must be between 1 and 65535")

    @classmethod
    def from_environment(cls) -> EmailTransport:
        sender = os.environ.get("EMAIL_ADDRESS", "").strip()
        password = os.environ.get("EMAIL_PASSWORD", "")
        host = os.environ.get("EMAIL_SMTP_HOST", "").strip()
        if not sender or not password or not host:
            return DisabledEmailTransport()
        try:
            port = int(os.environ.get("EMAIL_SMTP_PORT", "587") or "587")
        except ValueError:
            return DisabledEmailTransport()
        return cls(sender=sender, password=password, host=host, port=port)

    def send(
        self,
        *,
        recipient: str,
        subject: str,
        body: str,
        attachment_path: str,
        attachment_sha256: str,
    ) -> dict[str, Any]:
        path = Path(attachment_path)
        if not path.is_file():
            raise FinanceValidationError("Issued invoice PDF is missing")
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != str(attachment_sha256):
            raise FinanceConflictError("Issued invoice PDF hash does not match the stored artifact")
        normalized_recipient = str(recipient).strip()
        if not normalized_recipient or any(marker in normalized_recipient for marker in ("\r", "\n")):
            raise FinanceValidationError("Email recipient contains invalid header characters")
        message = MIMEMultipart()
        message["From"] = self.sender
        message["To"] = normalized_recipient
        message["Subject"] = str(subject)
        message.attach(MIMEText(str(body), "plain", "utf-8"))
        attachment = MIMEApplication(payload, _subtype="pdf")
        attachment.add_header("Content-Disposition", "attachment", filename=path.name)
        message.attach(attachment)
        context = ssl.create_default_context()
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as server:
            server.starttls(context=context)
            server.login(self.sender, self.password)
            server.send_message(message)
        return {"provider": "smtp", "message_id": f"smtp-{attachment_sha256[:12]}"}


class DeliveryService:
    """Keep external delivery asynchronous and idempotent."""

    def __init__(self, core: FinanceCore):
        self.core = core

    def prepare(
        self,
        invoice_id: str,
        *,
        method: str,
        prepared_by: str,
        recipient: str | None = None,
    ) -> dict[str, Any]:
        actor = _text(prepared_by, field="prepared_by", required=True)
        normalized_method = _text(method, field="method", required=True).lower()
        if normalized_method not in _DELIVERY_METHODS:
            raise FinanceValidationError("method must be email, web, postal, or discord")
        invoice = self.core._invoice(invoice_id)
        if invoice["document_status"] != "ISSUED":
            raise FinanceConflictError("Only ISSUED invoices can be prepared for delivery")
        self.core.approvals.authorize_invoice_action(invoice, actor_id=actor, action="send")
        customer = self.core._customer(invoice["customer_id"])
        billing_profile = customer.get("billing_profile") or {}
        recipient_channel_id = None
        if normalized_method == "discord":
            recipient_channel_id = _text(recipient, field="recipient", required=True, limit=30)
            if not re.fullmatch(r"[0-9]{5,30}", recipient_channel_id):
                raise FinanceValidationError("Discord delivery recipient must be a channel ID")
        record = {
            "id": f"del_{uuid.uuid4().hex}",
            "invoice_id": invoice_id,
            "method": normalized_method,
            "status": "QUEUED",
            "approved_by": actor,
            "external_reference": None,
            "downloaded_at": None,
            "recipient_email": billing_profile.get("email") if normalized_method == "email" else None,
            "recipient_channel_id": recipient_channel_id,
        }
        return self.core.store.create_delivery(record, actor=actor)

    def update_status(
        self,
        delivery_id: str,
        *,
        status: str,
        updated_by: str,
        external_reference: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        actor = _text(updated_by, field="updated_by", required=True)
        normalized_status = _text(status, field="status", required=True).upper()
        if normalized_status not in _DELIVERY_STATUSES:
            raise FinanceValidationError("status must be QUEUED, SENDING, SENT, DELIVERED, or FAILED")
        current = self.core.store.get_delivery(delivery_id)
        if current is None:
            raise FinanceNotFoundError(f"Delivery not found: {delivery_id}")
        transitions = {
            "QUEUED": {"SENDING", "SENT", "FAILED"},
            "SENDING": {"SENT", "FAILED"},
            "SENT": {"DELIVERED", "FAILED"},
            "DELIVERED": set(),
            "FAILED": {"QUEUED"},
        }
        if (
            normalized_status != current.get("status")
            and normalized_status not in transitions.get(current.get("status"), set())
        ):
            raise FinanceConflictError(
                f"Delivery cannot transition from {current.get('status')} to {normalized_status}"
            )
        invoice = self.core._invoice(current["invoice_id"])
        self.core.approvals.authorize_invoice_action(invoice, actor_id=actor, action="send")
        updates: dict[str, Any] = {"status": normalized_status}
        if external_reference is not None:
            updates["external_reference"] = _text(
                external_reference, field="external_reference", limit=300
            )
        if normalized_status == "SENT":
            updates["sent_at"] = self.core._now()
        if normalized_status == "DELIVERED":
            updates["delivered_at"] = self.core._now()
        if error is not None:
            updates["last_error"] = _text(error, field="error", limit=1000) or None
        return self.core.store.update_delivery(
            delivery_id,
            expected_status=current.get("status"),
            updates=updates,
            action="delivery.status_update",
            actor=actor,
        )

    def record_download(
        self,
        delivery_id: str,
        *,
        downloaded_at: str | None = None,
    ) -> dict[str, Any]:
        current = self.core.store.get_delivery(delivery_id)
        if current is None:
            raise FinanceNotFoundError(f"Delivery not found: {delivery_id}")
        timestamp = _text(
            downloaded_at or self.core._now(),
            field="downloaded_at",
            required=True,
            limit=100,
        )
        return self.core.store.update_delivery(
            delivery_id,
            expected_status=None,
            updates={"downloaded_at": timestamp},
            action="delivery.downloaded",
            actor="external-recipient",
        )

    def list_deliveries(self, invoice_id: str | None = None) -> list[dict[str, Any]]:
        if invoice_id:
            self.core._invoice(invoice_id)
        return self.core.store.list_deliveries(invoice_id)

    def list_outbox(self, invoice_id: str | None = None) -> list[dict[str, Any]]:
        return self.core.store.list_delivery_outbox(invoice_id=invoice_id)

    def retry(self, delivery_id: str, *, retried_by: str) -> dict[str, Any]:
        actor = str(retried_by or "").strip()
        if not actor:
            raise FinanceValidationError("retried_by is required")
        current = self.core.store.get_delivery(delivery_id)
        if current is None:
            raise FinanceNotFoundError(f"Delivery not found: {delivery_id}")
        invoice = self.core._invoice(current["invoice_id"])
        self.core.approvals.authorize_invoice_action(invoice, actor_id=actor, action="send")
        return self.core.store.retry_delivery(delivery_id, actor=actor)

    def dispatch_email(self, delivery_id: str, *, sent_by: str) -> dict[str, Any]:
        """Send one approved email outbox item and return a redacted result."""

        actor = str(sent_by or "").strip()
        if not actor:
            raise FinanceValidationError("sent_by is required")
        current = self.core.store.get_delivery(delivery_id)
        if current is None:
            raise FinanceNotFoundError(f"Delivery not found: {delivery_id}")
        if current.get("method") != "email":
            raise FinanceConflictError("Only email deliveries can use the email adapter")
        if current.get("status") != "QUEUED":
            raise FinanceConflictError("Only QUEUED email deliveries can be dispatched")
        invoice = self.core._invoice(current["invoice_id"])
        self.core.approvals.authorize_invoice_action(invoice, actor_id=actor, action="send")
        recipient = str(current.get("recipient_email") or "").strip()
        document = invoice.get("document") or {}
        attachment_path = str(document.get("pdf_path") or "")
        attachment_sha256 = str(document.get("pdf_sha256") or "")
        if not recipient:
            raise FinanceValidationError("Email recipient is missing")
        if not attachment_path or not attachment_sha256:
            raise FinanceConflictError("Issued invoice PDF is missing")

        try:
            self.core.update_delivery(delivery_id, status="SENDING", updated_by=actor)
        except Exception as exc:
            return {
                "success": False,
                "delivery_id": delivery_id,
                "delivery_status": current.get("status"),
                "error": "Email delivery could not be claimed",
                "error_type": type(exc).__name__,
            }

        try:
            result = self.core.email_transport.send(
                recipient=recipient,
                subject=f"請求書 {invoice.get('invoice_number') or invoice['id']}",
                body=(
                    "請求書を送付します。\n"
                    f"請求書番号: {invoice.get('invoice_number') or invoice['id']}\n"
                    f"請求金額: {invoice['totals'].get('collectible_total', invoice['totals']['total'])} {invoice['currency']}\n"
                    f"支払期限: {invoice['due_date']}\n"
                ),
                attachment_path=attachment_path,
                attachment_sha256=attachment_sha256,
            )
            if not isinstance(result, dict) or result.get("success") is False:
                raise FinanceConflictError("Email transport rejected the message")
        except Exception as exc:
            try:
                failed = self.core.update_delivery(
                    delivery_id,
                    status="FAILED",
                    updated_by=actor,
                    error=type(exc).__name__,
                )
                attempt_count = failed.get("attempt_count", 0)
            except Exception:
                attempt_count = None
            return {
                "success": False,
                "delivery_id": delivery_id,
                "invoice_id": invoice["id"],
                "delivery_status": "FAILED",
                "attempt_count": attempt_count,
                "error": "Email delivery failed; retry requires explicit approval",
            }
        reference = str(result.get("message_id") or result.get("provider") or "email")[:300]
        try:
            sent = self.core.update_delivery(
                delivery_id,
                status="SENT",
                updated_by=actor,
                external_reference=reference,
            )
        except Exception:
            return {
                "success": False,
                "delivery_id": delivery_id,
                "invoice_id": invoice["id"],
                "delivery_status": "SENDING",
                "error": "Email delivery outcome is uncertain; verify the provider before retry",
            }
        return {
            "success": True,
            "delivery_id": delivery_id,
            "invoice_id": invoice["id"],
            "status": sent["status"],
            "attempt_count": sent.get("attempt_count", 0),
            "external_reference": sent.get("external_reference"),
        }

    def dispatch_discord(self, delivery_id: str, *, sent_by: str) -> dict[str, Any]:
        """Send one queued Discord delivery with the issued PDF attached."""

        actor = str(sent_by or "").strip()
        if not actor:
            raise FinanceValidationError("sent_by is required")
        current = self.core.store.get_delivery(delivery_id)
        if current is None:
            raise FinanceNotFoundError(f"Delivery not found: {delivery_id}")
        if current.get("method") != "discord":
            raise FinanceConflictError("Only Discord deliveries can use the Discord adapter")
        if current.get("status") != "QUEUED":
            raise FinanceConflictError("Only QUEUED Discord deliveries can be dispatched")
        invoice = self.core._invoice(current["invoice_id"])
        self.core.approvals.authorize_invoice_action(invoice, actor_id=actor, action="send")
        recipient = str(current.get("recipient_channel_id") or "").strip()
        document = invoice.get("document") or {}
        attachment_path = str(document.get("pdf_path") or "")
        attachment_sha256 = str(document.get("pdf_sha256") or "")
        if not recipient:
            raise FinanceValidationError("Discord delivery channel is missing")
        if not attachment_path or not attachment_sha256:
            raise FinanceConflictError("Issued invoice PDF is missing")

        try:
            self.core.update_delivery(delivery_id, status="SENDING", updated_by=actor)
        except Exception as exc:
            return {
                "success": False,
                "delivery_id": delivery_id,
                "delivery_status": current.get("status"),
                "error": "Discord delivery could not be claimed",
                "error_type": type(exc).__name__,
            }

        try:
            result = self.core.discord_transport.send(
                recipient=recipient,
                subject=f"請求書 {invoice.get('invoice_number') or invoice['id']}",
                body=(
                    "請求書を送付します。\n"
                    f"請求書番号: {invoice.get('invoice_number') or invoice['id']}\n"
                    f"請求金額: {invoice['totals'].get('collectible_total', invoice['totals']['total'])} {invoice['currency']}\n"
                    f"支払期限: {invoice['due_date']}\n"
                ),
                attachment_path=attachment_path,
                attachment_sha256=attachment_sha256,
            )
            if not isinstance(result, dict) or result.get("success") is False:
                raise FinanceConflictError("Discord transport rejected the message")
        except Exception:
            try:
                failed = self.core.update_delivery(
                    delivery_id,
                    status="FAILED",
                    updated_by=actor,
                    error="Discord delivery failed",
                )
                attempt_count = failed.get("attempt_count", 0)
            except Exception:
                attempt_count = None
            return {
                "success": False,
                "delivery_id": delivery_id,
                "invoice_id": invoice["id"],
                "delivery_status": "FAILED",
                "attempt_count": attempt_count,
                "error": "Discord delivery failed; retry requires explicit approval",
            }
        reference = str(result.get("message_id") or result.get("provider") or "discord")[:300]
        try:
            sent = self.core.update_delivery(
                delivery_id,
                status="SENT",
                updated_by=actor,
                external_reference=reference,
            )
        except Exception:
            return {
                "success": False,
                "delivery_id": delivery_id,
                "invoice_id": invoice["id"],
                "delivery_status": "SENDING",
                "error": "Discord delivery outcome is uncertain; verify Discord before retry",
            }
        return {
            "success": True,
            "delivery_id": delivery_id,
            "invoice_id": invoice["id"],
            "status": sent["status"],
            "attempt_count": sent.get("attempt_count", 0),
            "external_reference": sent.get("external_reference"),
        }
