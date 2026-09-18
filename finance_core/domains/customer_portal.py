"""Token-authenticated access to a customer's issued invoices."""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

from finance_core.errors import (
    FinanceAuthorizationError,
    FinanceConflictError,
    FinanceNotFoundError,
    FinanceValidationError,
)
from finance_core.domains.customer_common import iso_datetime, now, text

if TYPE_CHECKING:
    from finance_core.domains.customers import CustomerService


class CustomerPortalService:
    """Own portal-token lifecycle and the intentionally narrow invoice view."""

    def __init__(self, owner: CustomerService):
        self.owner = owner

    def create_token(
        self,
        customer_id: str,
        *,
        expires_at: str,
        created_by: str,
    ) -> dict[str, Any]:
        customer = self.owner._customer(customer_id)
        if not (customer.get("billing_profile") or {}).get("portal_enabled", True):
            raise FinanceConflictError("Customer portal is disabled for this customer")
        actor = self.owner._actor(created_by, field="created_by")
        expiry = iso_datetime(expires_at, field="expires_at")
        if datetime.fromisoformat(expiry) <= datetime.now(timezone.utc):
            raise FinanceValidationError("expires_at must be in the future")
        raw_token = secrets.token_urlsafe(32)
        record = {
            "id": f"cpt_{uuid.uuid4().hex}",
            "customer_id": customer_id,
            "token_hash": hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            "expires_at": expiry,
            "created_by": actor,
        }
        stored = self.owner.core.store.put_customer_portal_token(record)
        return {
            "portal_token_id": stored["id"],
            "customer_id": customer_id,
            "token": raw_token,
            "expires_at": expiry,
        }

    def _customer_for_token(self, token: str) -> tuple[dict[str, Any], dict[str, Any]]:
        raw = text(token, field="token", required=True, limit=500)
        record = self.owner.core.store.get_customer_portal_token(
            hashlib.sha256(raw.encode("utf-8")).hexdigest()
        )
        if record is None or record.get("revoked_at"):
            raise FinanceAuthorizationError("Customer portal token is invalid")
        expiry = datetime.fromisoformat(str(record["expires_at"]))
        if expiry <= datetime.now(timezone.utc):
            raise FinanceAuthorizationError("Customer portal token has expired")
        customer = self.owner._customer(str(record["customer_id"]))
        self.owner.core.store.touch_customer_portal_token(record["id"], used_at=now())
        return customer, record

    def list_invoices(self, token: str) -> list[dict[str, Any]]:
        customer, _record = self._customer_for_token(token)
        rows = self.owner.core.search_invoices(
            customer_id=customer["id"], document_status="ISSUED"
        )
        return [
            {
                "invoice_id": row["id"],
                "invoice_number": row.get("invoice_number"),
                "issue_date": row.get("issue_date"),
                "due_date": row.get("due_date"),
                "currency": row.get("currency"),
                "total": (row.get("totals") or {}).get("total"),
                "collectible_total": (row.get("totals") or {}).get("collectible_total"),
                "settlement_status": row.get("settlement_status"),
            }
            for row in rows
        ]

    def get_invoice(self, token: str, invoice_id: str) -> dict[str, Any]:
        customer, _record = self._customer_for_token(token)
        invoice = self.owner.core.get_invoice(invoice_id)
        if invoice.get("customer_id") != customer["id"] or invoice.get("document_status") != "ISSUED":
            raise FinanceAuthorizationError("Invoice is not available through this portal")
        totals = invoice.get("totals") or {}
        return {
            "invoice_id": invoice["id"],
            "invoice_number": invoice.get("invoice_number"),
            "issue_date": invoice.get("issue_date"),
            "service_period": invoice.get("service_period"),
            "due_date": invoice.get("due_date"),
            "currency": invoice.get("currency"),
            "lines": deepcopy(invoice.get("lines") or []),
            "totals": deepcopy(totals),
            "settlement_status": invoice.get("settlement_status"),
            "document_available": bool((invoice.get("document") or {}).get("pdf_path")),
            "pdf_sha256": (invoice.get("document") or {}).get("pdf_sha256"),
        }

    def download_invoice(self, token: str, invoice_id: str) -> dict[str, Any]:
        details = self.get_invoice(token, invoice_id)
        invoice = self.owner.core.get_invoice(invoice_id)
        path = Path(str((invoice.get("document") or {}).get("pdf_path") or ""))
        if not path.is_file():
            raise FinanceNotFoundError("Issued invoice PDF is missing")
        payload = path.read_bytes()
        return {
            "invoice_id": invoice_id,
            "invoice_number": details.get("invoice_number"),
            "content_type": "application/pdf",
            "filename": f"invoice_{details.get('invoice_number') or invoice_id}.pdf",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "content_base64": base64.b64encode(payload).decode("ascii"),
        }

    def revoke_token(self, token_id: str, *, revoked_by: str) -> dict[str, Any]:
        actor = self.owner._actor(revoked_by, field="revoked_by")
        revoked_at = now()
        self.owner.core.store.revoke_customer_portal_token(token_id, revoked_at=revoked_at)
        return {
            "portal_token_id": token_id,
            "revoked_at": revoked_at,
            "revoked_by": actor,
        }
