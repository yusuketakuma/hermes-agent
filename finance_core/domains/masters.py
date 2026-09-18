"""Issuer, contract, and billing-rule master services."""

from __future__ import annotations

import copy
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, TYPE_CHECKING

from finance_core.errors import FinanceConflictError, FinanceNotFoundError, FinanceValidationError
from finance_core.tax import TAX_CATEGORIES

if TYPE_CHECKING:
    from finance_core.core import FinanceCore


_BILLING_RULE_TYPES = frozenset(
    {
        "RECURRING_FIXED",
        "HOURLY",
        "PER_EVENT",
        "EXPENSE",
        "MILESTONE",
        "DISCOUNT",
        "ADJUSTMENT",
        "MANUAL",
    }
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


class MasterService:
    """Keep master-data validation and persistence outside the facade."""

    def __init__(self, core: FinanceCore):
        self.core = core

    def create_issuer(
        self,
        *,
        legal_name: str,
        qualified_invoice_issuer: bool = False,
        registration_number: str | None = None,
        address: str | None = None,
        bank_account: dict[str, Any] | None = None,
        tax_rounding_policy: str = "HALF_UP",
    ) -> dict[str, Any]:
        legal_name = _text(legal_name, field="legal_name", required=True)
        rounding = str(tax_rounding_policy).upper()
        if rounding not in {"DOWN", "HALF_UP", "UP"}:
            raise FinanceValidationError("tax_rounding_policy must be DOWN, HALF_UP, or UP")
        registration = _text(registration_number, field="registration_number")
        if qualified_invoice_issuer and not registration:
            raise FinanceValidationError(
                "registration_number is required for a qualified invoice issuer"
            )
        record = {
            "id": f"iss_{uuid.uuid4().hex}",
            "legal_name": legal_name,
            "qualified_invoice_issuer": bool(qualified_invoice_issuer),
            "registration_number": registration or None,
            "address": _text(address, field="address"),
            "bank_account": copy.deepcopy(bank_account) if bank_account else None,
            "tax_rounding_policy": rounding,
            "active_status": "ACTIVE",
        }
        return self.core.store.put_issuer(record)

    def create_contract(
        self,
        *,
        customer_id: str,
        name: str,
        effective_from: str,
        effective_to: str | None = None,
        currency: str = "JPY",
        payment_due_rule: dict[str, Any] | None = None,
        default_template_id: str | None = None,
        project_id: str | None = None,
        department_id: str | None = None,
        created_by: str,
    ) -> dict[str, Any]:
        actor = _text(created_by, field="created_by", required=True)
        customer = self.core._customer(customer_id)
        if customer.get("active_status") != "ACTIVE":
            raise FinanceConflictError("Inactive customers cannot receive new contracts")
        start = _date(effective_from, field="effective_from")
        end = _date(effective_to, field="effective_to") if effective_to else None
        if end and end < start:
            raise FinanceValidationError("effective_to cannot be earlier than effective_from")
        normalized_currency = _text(currency, field="currency", required=True).upper()
        if len(normalized_currency) != 3 or not normalized_currency.isalpha():
            raise FinanceValidationError("currency must be a three-letter code")
        if payment_due_rule is not None and not isinstance(payment_due_rule, dict):
            raise FinanceValidationError("payment_due_rule must be an object")
        record = {
            "id": f"ctr_{uuid.uuid4().hex}",
            "customer_id": customer_id,
            "name": _text(name, field="name", required=True, limit=300),
            "effective_from": start,
            "effective_to": end,
            "currency": normalized_currency,
            "payment_due_rule": copy.deepcopy(payment_due_rule or {}),
            "default_template_id": _text(default_template_id, field="default_template_id", limit=200) or None,
            "project_id": _text(project_id, field="project_id", limit=200) or None,
            "department_id": _text(department_id, field="department_id", limit=200) or None,
            "active_status": "ACTIVE",
        }
        stored = self.core.store.put_contract(record)
        self.core.store.record_audit(
            "contract.create",
            actor=actor,
            detail={"contract_id": stored["id"], "customer_id": customer_id},
        )
        return stored

    def get_contract(self, contract_id: str) -> dict[str, Any]:
        contract = self.core.store.get_contract(contract_id)
        if contract is None:
            raise FinanceNotFoundError(f"Contract not found: {contract_id}")
        return contract

    def list_contracts(self, customer_id: str | None = None) -> list[dict[str, Any]]:
        if customer_id:
            self.core._customer(customer_id)
        return self.core.store.list_contracts(customer_id)

    def create_billing_rule(
        self,
        *,
        contract_id: str,
        rule_type: str,
        description: str,
        unit: str,
        unit_price: str,
        tax_category: str,
        effective_from: str,
        effective_to: str | None = None,
        version: int = 1,
        created_by: str,
    ) -> dict[str, Any]:
        actor = _text(created_by, field="created_by", required=True)
        contract = self.get_contract(contract_id)
        if contract.get("active_status") != "ACTIVE":
            raise FinanceConflictError("Inactive contracts cannot receive billing rules")
        normalized_type = _text(rule_type, field="rule_type", required=True).upper()
        if normalized_type not in _BILLING_RULE_TYPES:
            raise FinanceValidationError(
                f"rule_type must be one of {', '.join(sorted(_BILLING_RULE_TYPES))}"
            )
        category = _text(tax_category, field="tax_category", required=True).upper()
        if category not in TAX_CATEGORIES:
            raise FinanceValidationError(f"Unsupported tax_category: {category}")
        try:
            price = Decimal(str(unit_price))
            rule_version = int(version)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise FinanceValidationError("unit_price and version must be valid") from exc
        if not price.is_finite() or price < 0:
            raise FinanceValidationError("unit_price must be non-negative")
        if rule_version < 1:
            raise FinanceValidationError("version must be at least 1")
        start = _date(effective_from, field="effective_from")
        end = _date(effective_to, field="effective_to") if effective_to else None
        if end and end < start:
            raise FinanceValidationError("effective_to cannot be earlier than effective_from")
        record = {
            "id": f"brv_{uuid.uuid4().hex}",
            "contract_id": contract_id,
            "version": rule_version,
            "rule_type": normalized_type,
            "description": _text(description, field="description", required=True, limit=1000),
            "unit": _text(unit, field="unit", required=True, limit=100),
            "unit_price": _money_text(price),
            "tax_category": category,
            "tax_rate": _money_text(TAX_CATEGORIES[category]),
            "effective_from": start,
            "effective_to": end,
            "active_status": "ACTIVE",
        }
        stored = self.core.store.put_billing_rule(record)
        self.core.store.record_audit(
            "billing_rule.create",
            actor=actor,
            detail={"billing_rule_id": stored["id"], "contract_id": contract_id},
        )
        return stored

    def get_billing_rule(self, billing_rule_id: str) -> dict[str, Any]:
        rule = self.core.store.get_billing_rule(billing_rule_id)
        if rule is None:
            raise FinanceNotFoundError(f"Billing rule not found: {billing_rule_id}")
        return rule

    def list_billing_rules(self, contract_id: str | None = None) -> list[dict[str, Any]]:
        if contract_id:
            self.get_contract(contract_id)
        return self.core.store.list_billing_rules(contract_id)
