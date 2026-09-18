"""Approval policies and accounting-period locks."""

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, TYPE_CHECKING

from finance_core.errors import FinanceAuthorizationError, FinanceConflictError, FinanceValidationError

if TYPE_CHECKING:
    from finance_core.core import FinanceCore


_PERIOD_RE = re.compile(r"^[0-9]{4}-[0-9]{2}$")
_ACTIONS = {"approve", "issue", "send"}


class ApprovalService:
    """Enforce per-actor invoice permissions and accounting-period locks."""

    def __init__(self, core: FinanceCore):
        self.core = core

    def authorize_policy_configuration(self, *, configured_by: str, actor_id: str) -> None:
        """Allow only self-bootstrap or an existing admin to change policies."""

        configured = self._actor(configured_by, "configured_by")
        target = self._actor(actor_id, "actor_id")
        policies = self.core.store.list_approval_policies()
        if not policies:
            if target != configured:
                raise FinanceAuthorizationError(
                    "The first approval policy must configure the authenticated actor"
                )
            return
        admins = [
            policy
            for policy in policies
            if policy.get("actor_id") == configured
            and policy.get("role") == "admin"
            and policy.get("active_status") == "ACTIVE"
        ]
        if not admins:
            raise FinanceAuthorizationError(
                "Only an active admin can configure approval policies"
            )

    @staticmethod
    def _actor(actor_id: str, field: str) -> str:
        actor = str(actor_id or "").strip()
        if not actor or len(actor) > 200:
            raise FinanceValidationError(f"{field} is required and bounded")
        return actor

    @staticmethod
    def _period(period: str) -> str:
        normalized = str(period or "").strip()
        if not _PERIOD_RE.fullmatch(normalized):
            raise FinanceValidationError("period must use YYYY-MM")
        try:
            date.fromisoformat(f"{normalized}-01")
        except ValueError as exc:
            raise FinanceValidationError("period is not a valid calendar month") from exc
        return normalized

    def set_policy(
        self,
        *,
        actor_id: str,
        role: str,
        can_approve: bool = False,
        can_issue: bool = False,
        can_send: bool = False,
        max_amount: str | None = None,
        separation_required: bool = False,
        issuer_id: str | None = None,
        configured_by: str,
    ) -> dict[str, Any]:
        actor = self._actor(actor_id, "actor_id")
        configured = self._actor(configured_by, "configured_by")
        normalized_role = str(role or "").strip().lower()
        if normalized_role not in {"admin", "approver", "issuer", "sender", "operator"}:
            raise FinanceValidationError("role is unsupported")
        normalized_max = None
        if max_amount not in (None, ""):
            try:
                amount = Decimal(str(max_amount))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise FinanceValidationError("max_amount must be a valid decimal") from exc
            if not amount.is_finite() or amount <= 0:
                raise FinanceValidationError("max_amount must be greater than zero")
            normalized_max = format(amount, "f")
        if issuer_id:
            self.core._issuer(issuer_id)
        record = {
            "id": f"pol_{uuid.uuid4().hex}",
            "actor_id": actor,
            "issuer_id": issuer_id or None,
            "role": normalized_role,
            "can_approve": bool(can_approve),
            "can_issue": bool(can_issue),
            "can_send": bool(can_send),
            "max_amount": normalized_max,
            "separation_required": bool(separation_required),
            "active_status": "ACTIVE",
            "configured_by": configured,
        }
        return self.core.store.put_approval_policy(record)

    def list_policies(self, *, actor_id: str | None = None) -> list[dict[str, Any]]:
        return self.core.store.list_approval_policies(actor_id=actor_id)

    def authorize_invoice_action(
        self,
        invoice: dict[str, Any],
        *,
        actor_id: str,
        action: str,
    ) -> None:
        actor = self._actor(actor_id, f"{action}_by")
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in _ACTIONS:
            raise FinanceValidationError("approval action is unsupported")
        policies = self.core.store.list_approval_policies()
        if not policies:
            if getattr(self.core, "require_approval_policy", False):
                raise FinanceAuthorizationError(
                    "No active approval policy is configured"
                )
            return
        matching = [
            policy
            for policy in policies
            if policy.get("actor_id") == actor
            and policy.get("active_status") == "ACTIVE"
            and (
                not policy.get("issuer_id")
                or policy.get("issuer_id") == invoice.get("issuer_id")
            )
        ]
        permission_key = {
            "approve": "can_approve",
            "issue": "can_issue",
            "send": "can_send",
        }[normalized_action]
        permitted = [policy for policy in matching if policy.get(permission_key)]
        if not permitted:
            raise FinanceAuthorizationError(
                f"{actor} is not authorized to {normalized_action} this invoice"
            )
        total = Decimal(str((invoice.get("totals") or {}).get("total") or "0"))
        for policy in permitted:
            maximum = policy.get("max_amount")
            if maximum is not None and total > Decimal(str(maximum)):
                continue
            if policy.get("separation_required"):
                if normalized_action == "approve" and invoice.get("created_by") == actor:
                    raise FinanceAuthorizationError(
                        "The invoice creator cannot approve this invoice"
                    )
                if normalized_action == "issue" and invoice.get("approved_by") == actor:
                    raise FinanceAuthorizationError(
                        "The approver cannot issue this invoice"
                    )
                if normalized_action == "send" and actor in {
                    invoice.get("created_by"),
                    invoice.get("approved_by"),
                }:
                    raise FinanceAuthorizationError(
                        "The creator or approver cannot send this invoice"
                    )
            return
        raise FinanceAuthorizationError(
            f"{actor} exceeds the configured amount limit for {normalized_action}"
        )

    def assert_period_open(self, period: str, *, operation: str) -> None:
        normalized = self._period(period)
        lock = self.core.store.get_period_lock(normalized)
        if lock and lock.get("status") == "CLOSED":
            raise FinanceConflictError(
                f"Accounting period {normalized} is closed for {operation}"
            )

    def close_period(self, period: str, *, closed_by: str, reason: str) -> dict[str, Any]:
        normalized = self._period(period)
        actor = self._actor(closed_by, "closed_by")
        note = str(reason or "").strip()
        if not note or len(note) > 1000:
            raise FinanceValidationError("reason is required and bounded")
        record = self.core.store.close_period(normalized, actor=actor, reason=note)
        self.core.store.record_audit(
            "period.close",
            actor=actor,
            detail={"period": normalized, "reason": note},
        )
        return record

    def reopen_period(self, period: str, *, reopened_by: str, reason: str) -> dict[str, Any]:
        normalized = self._period(period)
        actor = self._actor(reopened_by, "reopened_by")
        note = str(reason or "").strip()
        if not note or len(note) > 1000:
            raise FinanceValidationError("reason is required and bounded")
        record = self.core.store.reopen_period(normalized, actor=actor, reason=note)
        self.core.store.record_audit(
            "period.reopen",
            actor=actor,
            detail={"period": normalized, "reason": note},
        )
        return record

    def period_status(self, period: str) -> dict[str, Any]:
        normalized = self._period(period)
        lock = self.core.store.get_period_lock(normalized)
        return lock or {"period": normalized, "status": "OPEN"}

    def list_periods(self) -> list[dict[str, Any]]:
        return self.core.store.list_period_locks()
