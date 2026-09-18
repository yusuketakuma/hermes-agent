"""Customer lifecycle, contacts, matching, privacy, and portal services."""

from __future__ import annotations

import difflib
import uuid
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, TYPE_CHECKING

from finance_core.errors import (
    FinanceAuthorizationError,
    FinanceConflictError,
    FinanceNotFoundError,
    FinanceValidationError,
)
from finance_core.tax import TAX_CATEGORIES
from finance_core.domains.customer_common import (
    iso_datetime as _iso_datetime,
    money_text as _money_text,
    normalize as _normalize,
    now as _now,
    text as _text,
)
from finance_core.domains.customer_import import CustomerImportService
from finance_core.domains.customer_portal import CustomerPortalService

if TYPE_CHECKING:
    from finance_core.core import FinanceCore


_LIFECYCLE_STATUSES = frozenset(
    {"LEAD", "PROSPECT", "ACTIVE", "SUSPENDED", "DORMANT", "CLOSED"}
)
_CONTACT_ROLES = frozenset({"billing", "accounting", "contract", "executive", "other"})
_RELATIONSHIP_TYPES = frozenset({"PARENT", "SUBSIDIARY", "AFFILIATE", "BILL_TO", "SHIP_TO"})
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "address",
        "billing_address",
        "email",
        "phone",
        "contact_name",
        "name",
        "bank_account",
        "account_number",
    }
)


class CustomerService:
    """Own customer-specific state while keeping PII out of agent responses."""

    def __init__(self, core: FinanceCore):
        self.core = core
        self.importer = CustomerImportService(self)
        self.portal = CustomerPortalService(self)

    @staticmethod
    def _active_status(lifecycle_status: str) -> str:
        return "ACTIVE" if lifecycle_status in {"LEAD", "PROSPECT", "ACTIVE"} else "INACTIVE"

    @staticmethod
    def _validate_kind(value: Any) -> str:
        kind = str(value or "").strip().lower()
        if kind not in {"company", "individual"}:
            raise FinanceValidationError("customer_kind must be company or individual")
        return kind

    @staticmethod
    def _validate_lifecycle(value: Any) -> str:
        status = str(value or "").strip().upper()
        if status not in _LIFECYCLE_STATUSES:
            raise FinanceValidationError(
                f"lifecycle_status must be one of {', '.join(sorted(_LIFECYCLE_STATUSES))}"
            )
        return status

    @staticmethod
    def _validate_email(value: Any, *, field: str = "email") -> str:
        email = _text(value, field=field, limit=320)
        if email and ("@" not in email or email.startswith("@") or email.endswith("@")):
            raise FinanceValidationError(f"{field} must be a valid email address")
        return email

    @staticmethod
    def _validate_corporate_number(value: Any) -> str | None:
        number = _text(value, field="corporate_number", limit=13)
        if number and (len(number) != 13 or not number.isdigit()):
            raise FinanceValidationError("corporate_number must contain 13 digits")
        return number or None

    @staticmethod
    def _validate_tags(value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 100:
            raise FinanceValidationError("tags must be a list of at most 100 values")
        tags = [_text(item, field="tags", required=True, limit=100) for item in value]
        if len(tags) != len(set(tags)):
            raise FinanceValidationError("tags must not contain duplicates")
        return tags

    @classmethod
    def _profile(
        cls,
        billing_profile: dict[str, Any],
        *,
        kind: str,
        legal_name: str,
        current: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(billing_profile, dict):
            raise FinanceValidationError("billing_profile must be an object")
        profile = deepcopy((current or {}).get("billing_profile") or {})
        normalized_input = deepcopy(billing_profile)
        for alias, canonical in (
            ("legal_name", "billing_name"),
            ("address", "billing_address"),
        ):
            if alias not in normalized_input:
                continue
            alias_value = normalized_input.pop(alias)
            if canonical in normalized_input and normalized_input[canonical] != alias_value:
                raise FinanceValidationError(
                    f"billing_profile fields {alias} and {canonical} disagree"
                )
            normalized_input.setdefault(canonical, alias_value)
        allowed = {
            "billing_name",
            "honorific",
            "billing_address",
            "postal_code",
            "department",
            "contact_name",
            "email",
            "phone",
            "fax",
            "preferred_delivery_method",
            "closing_day",
            "payment_due_rule",
            "default_template_id",
            "default_currency",
            "bank_fee_payer",
            "credit_limit",
            "credit_currency",
            "dunning_policy",
            "default_tax_category",
            "portal_enabled",
        }
        unknown = sorted(set(normalized_input) - allowed)
        if unknown:
            raise FinanceValidationError(
                f"billing_profile contains unsupported fields: {', '.join(unknown)}"
            )
        profile.update(normalized_input)
        profile["billing_name"] = _text(
            profile.get("billing_name") or legal_name,
            field="billing_profile.billing_name",
            required=True,
        )
        profile["billing_address"] = _text(
            profile.get("billing_address"),
            field="billing_profile.billing_address",
            required=True,
        )
        profile["postal_code"] = _text(
            profile.get("postal_code"), field="postal_code", limit=20
        ) or None
        profile["department"] = _text(profile.get("department"), field="department")
        profile["contact_name"] = _text(profile.get("contact_name"), field="contact_name")
        profile["fax"] = _text(profile.get("fax"), field="fax", limit=50) or None
        profile["preferred_delivery_method"] = _text(
            profile.get("preferred_delivery_method"),
            field="preferred_delivery_method",
        )
        profile["closing_day"] = profile.get("closing_day")
        profile["payment_due_rule"] = deepcopy(profile.get("payment_due_rule"))
        profile["default_template_id"] = _text(
            profile.get("default_template_id"), field="default_template_id"
        ) or None
        profile["bank_fee_payer"] = _text(profile.get("bank_fee_payer"), field="bank_fee_payer") or None
        expected_honorific = "御中" if kind == "company" else "様"
        supplied_honorific = _text(profile.get("honorific"), field="honorific")
        if supplied_honorific and supplied_honorific != expected_honorific:
            raise FinanceValidationError(
                f"honorific for {kind} customers must be {expected_honorific}"
            )
        profile["honorific"] = expected_honorific
        profile["email"] = cls._validate_email(profile.get("email"))
        profile["phone"] = _text(profile.get("phone"), field="phone", limit=50)
        profile["default_currency"] = _text(
            profile.get("default_currency") or "JPY", field="default_currency", required=True
        ).upper()
        if len(profile["default_currency"]) != 3 or not profile["default_currency"].isalpha():
            raise FinanceValidationError("default_currency must be a three-letter code")
        profile["credit_currency"] = _text(
            profile.get("credit_currency") or profile["default_currency"],
            field="credit_currency",
            required=True,
        ).upper()
        if len(profile["credit_currency"]) != 3 or not profile["credit_currency"].isalpha():
            raise FinanceValidationError("credit_currency must be a three-letter code")
        if profile.get("credit_limit") not in (None, ""):
            try:
                credit_limit = Decimal(str(profile["credit_limit"]))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise FinanceValidationError("credit_limit must be a valid decimal") from exc
            if not credit_limit.is_finite() or credit_limit < 0:
                raise FinanceValidationError("credit_limit must be non-negative")
            profile["credit_limit"] = _money_text(credit_limit)
        else:
            profile["credit_limit"] = None
        if profile.get("payment_due_rule") is not None and not isinstance(
            profile["payment_due_rule"], dict
        ):
            raise FinanceValidationError("payment_due_rule must be an object")
        if profile.get("dunning_policy") is not None and not isinstance(
            profile["dunning_policy"], dict
        ):
            raise FinanceValidationError("dunning_policy must be an object")
        tax_category = str(profile.get("default_tax_category") or "").upper()
        if tax_category and tax_category not in TAX_CATEGORIES:
            raise FinanceValidationError("default_tax_category is unsupported")
        profile["default_tax_category"] = tax_category or None
        profile["portal_enabled"] = bool(profile.get("portal_enabled", True))
        return profile

    @classmethod
    def _build_customer_record(
        cls,
        *,
        customer_kind: str,
        legal_name: str,
        billing_profile: dict[str, Any],
        customer_id: str | None = None,
        corporate_number: str | None = None,
        external_id: str | None = None,
        owner_id: str | None = None,
        tags: list[str] | None = None,
        lifecycle_status: str = "ACTIVE",
        revision: int = 0,
        current: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kind = cls._validate_kind(customer_kind)
        name = _text(legal_name, field="legal_name", required=True, limit=500)
        lifecycle = cls._validate_lifecycle(lifecycle_status)
        record = deepcopy(current) if current else {}
        record.update(
            {
                "id": customer_id or f"cus_{uuid.uuid4().hex}",
                "customer_kind": kind,
                "legal_name": name,
                "corporate_number": cls._validate_corporate_number(corporate_number),
                "external_id": _text(external_id, field="external_id", limit=200) or None,
                "owner_id": _text(owner_id, field="owner_id", limit=200) or None,
                "tags": cls._validate_tags(tags),
                "lifecycle_status": lifecycle,
                "active_status": cls._active_status(lifecycle),
                "revision": int(revision),
            }
        )
        record["billing_profile"] = cls._profile(
            billing_profile,
            kind=kind,
            legal_name=name,
            current=current,
        )
        record.setdefault("aliases", list((current or {}).get("aliases") or []))
        return record

    @staticmethod
    def _summary(customer: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "id",
            "customer_kind",
            "legal_name",
            "corporate_number",
            "external_id",
            "owner_id",
            "tags",
            "active_status",
            "lifecycle_status",
            "revision",
            "merged_into",
            "merged_customer_ids",
        )
        return {key: deepcopy(customer[key]) for key in keys if key in customer}

    @staticmethod
    def _safe_contact(contact: dict[str, Any], *, include_sensitive: bool) -> dict[str, Any]:
        if include_sensitive:
            return deepcopy(contact)
        return {
            key: contact[key]
            for key in ("id", "customer_id", "role", "is_primary", "active_status")
            if key in contact
        }

    @staticmethod
    def _safe_activity(activity: dict[str, Any]) -> dict[str, Any]:
        return {
            key: deepcopy(activity[key])
            for key in ("id", "customer_id", "activity_type", "summary", "actor", "occurred_at")
            if key in activity
        }

    def _customer(self, customer_id: str) -> dict[str, Any]:
        customer = self.core.store.get_customer(customer_id)
        if customer is None:
            raise FinanceNotFoundError(f"Customer not found: {customer_id}")
        return customer

    @staticmethod
    def _actor(actor: str | None, *, field: str) -> str:
        return _text(actor, field=field, required=True, limit=200)

    def _assert_sensitive_access(self, actor: str) -> str:
        normalized = self._actor(actor, field="accessed_by")
        policies = self.core.store.list_approval_policies(actor_id=normalized)
        if policies and not any(
            policy.get("role") in {"admin", "operator"} or policy.get("can_view_pii")
            for policy in policies
        ):
            raise FinanceAuthorizationError("Actor is not authorized to view customer PII")
        return normalized

    def _record_access(self, customer_id: str, *, actor: str, action: str) -> None:
        self.core.store.record_customer_access(
            {
                "id": f"caccess_{uuid.uuid4().hex}",
                "customer_id": customer_id,
                "action": action,
                "actor": actor,
            }
        )

    def create_customer(
        self,
        *,
        customer_kind: str,
        legal_name: str,
        billing_profile: dict[str, Any],
        corporate_number: str | None = None,
        external_id: str | None = None,
        owner_id: str | None = None,
        tags: list[str] | None = None,
        lifecycle_status: str = "ACTIVE",
        contacts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        record = self._build_customer_record(
            customer_kind=customer_kind,
            legal_name=legal_name,
            billing_profile=billing_profile,
            corporate_number=corporate_number,
            external_id=external_id,
            owner_id=owner_id,
            tags=tags,
            lifecycle_status=lifecycle_status,
        )
        stored = self.core.store.put_customer(record)
        for contact in contacts or []:
            self.create_contact(
                stored["id"],
                name=contact.get("name"),
                email=contact.get("email"),
                phone=contact.get("phone"),
                department=contact.get("department"),
                role=contact.get("role", "other"),
                added_by=contact.get("added_by") or "system",
                is_primary=bool(contact.get("is_primary", False)),
            )
        return stored

    def get_customer(
        self,
        customer_id: str,
        *,
        include_sensitive: bool = False,
        accessed_by: str | None = None,
    ) -> dict[str, Any]:
        customer = self._customer(customer_id)
        if not include_sensitive:
            return self._summary(customer)
        actor = self._assert_sensitive_access(accessed_by or "")
        self._record_access(customer_id, actor=actor, action="customer.pii.view")
        detailed = deepcopy(customer)
        detailed["contacts"] = [
            self._safe_contact(row, include_sensitive=True)
            for row in self.core.store.list_customer_contacts(customer_id)
        ]
        return detailed

    def search_customers(
        self,
        *,
        query: str | None = None,
        customer_kind: str | None = None,
        active_status: str | None = None,
        lifecycle_status: str | None = None,
        owner_id: str | None = None,
        tag: str | None = None,
    ) -> list[dict[str, Any]]:
        needle = _normalize(query)
        kind = self._validate_kind(customer_kind) if customer_kind else None
        status = str(active_status or "").strip().upper()
        if status and status not in {"ACTIVE", "INACTIVE"}:
            raise FinanceValidationError("active_status must be ACTIVE or INACTIVE")
        lifecycle = self._validate_lifecycle(lifecycle_status) if lifecycle_status else None
        rows: list[dict[str, Any]] = []
        for customer in self.core.store.list_customers():
            if kind and customer.get("customer_kind") != kind:
                continue
            if status and customer.get("active_status") != status:
                continue
            if lifecycle and customer.get("lifecycle_status", "ACTIVE") != lifecycle:
                continue
            if owner_id and customer.get("owner_id") != owner_id:
                continue
            if tag and tag not in (customer.get("tags") or []):
                continue
            searchable = _normalize(
                " ".join(
                    [
                        str(customer.get("legal_name") or ""),
                        *(str(alias) for alias in customer.get("aliases") or []),
                        str((customer.get("billing_profile") or {}).get("billing_name") or ""),
                    ]
                )
            )
            if needle and needle not in searchable:
                continue
            rows.append(self._summary(customer))
        return rows

    def update_customer(
        self,
        customer_id: str,
        *,
        legal_name: str | None = None,
        customer_kind: str | None = None,
        billing_profile: dict[str, Any] | None = None,
        aliases: list[str] | None = None,
        corporate_number: str | None = None,
        external_id: str | None = None,
        owner_id: str | None = None,
        tags: list[str] | None = None,
        lifecycle_status: str | None = None,
        updated_by: str,
    ) -> dict[str, Any]:
        actor = self._actor(updated_by, field="updated_by")
        current = self._customer(customer_id)
        kind = self._validate_kind(customer_kind or current.get("customer_kind"))
        name = _text(legal_name or current.get("legal_name"), field="legal_name", required=True)
        profile = self._profile(
            billing_profile or {},
            kind=kind,
            legal_name=name,
            current=current,
        )
        status = self._validate_lifecycle(
            lifecycle_status or current.get("lifecycle_status") or "ACTIVE"
        )
        updated = deepcopy(current)
        updated.update(
            {
                "legal_name": name,
                "customer_kind": kind,
                "billing_profile": profile,
                "corporate_number": (
                    self._validate_corporate_number(corporate_number)
                    if corporate_number is not None
                    else current.get("corporate_number")
                ),
                "external_id": (
                    _text(external_id, field="external_id", limit=200) or None
                    if external_id is not None
                    else current.get("external_id")
                ),
                "owner_id": (
                    _text(owner_id, field="owner_id", limit=200) or None
                    if owner_id is not None
                    else current.get("owner_id")
                ),
                "tags": self._validate_tags(tags) if tags is not None else list(current.get("tags") or []),
                "lifecycle_status": status,
                "active_status": self._active_status(status),
                "revision": int(current.get("revision", 0)) + 1,
            }
        )
        if aliases is not None:
            if not isinstance(aliases, list) or len(aliases) > 100:
                raise FinanceValidationError("aliases must be a list of at most 100 values")
            updated["aliases"] = [
                _text(alias, field="aliases", required=True, limit=200) for alias in aliases
            ]
        stored = self.core.store.update_customer(updated, actor=actor)
        self.record_activity(
            customer_id,
            activity_type="customer.update",
            summary="Customer master updated",
            recorded_by=actor,
        )
        return stored

    def update_lifecycle(
        self,
        customer_id: str,
        *,
        status: str,
        reason: str,
        updated_by: str,
    ) -> dict[str, Any]:
        actor = self._actor(updated_by, field="updated_by")
        note = _text(reason, field="reason", required=True, limit=1000)
        updated = self.update_customer(
            customer_id,
            lifecycle_status=status,
            updated_by=actor,
        )
        self.record_activity(
            customer_id,
            activity_type="lifecycle.change",
            summary=note,
            recorded_by=actor,
        )
        return updated

    def archive_customer(self, customer_id: str, *, archived_by: str) -> dict[str, Any]:
        return self.update_lifecycle(
            customer_id,
            status="CLOSED",
            reason="Customer archived",
            updated_by=archived_by,
        )

    def list_customer_revisions(self, customer_id: str) -> list[dict[str, Any]]:
        self._customer(customer_id)
        return self.core.store.list_customer_revisions(customer_id)

    def update_payment_settings(
        self,
        customer_id: str,
        *,
        credit_limit: str | None = None,
        credit_currency: str | None = None,
        payment_due_rule: dict[str, Any] | None = None,
        closing_day: int | str | None = None,
        bank_fee_payer: str | None = None,
        dunning_policy: dict[str, Any] | None = None,
        default_tax_category: str | None = None,
        updated_by: str,
    ) -> dict[str, Any]:
        current = self._customer(customer_id)
        existing = deepcopy(current.get("billing_profile") or {})
        changes = {
            key: value
            for key, value in {
                "credit_limit": credit_limit,
                "credit_currency": credit_currency,
                "payment_due_rule": payment_due_rule,
                "closing_day": closing_day,
                "bank_fee_payer": bank_fee_payer,
                "dunning_policy": dunning_policy,
                "default_tax_category": default_tax_category,
            }.items()
            if value is not None
        }
        existing.update(changes)
        return self.update_customer(customer_id, billing_profile=existing, updated_by=updated_by)

    def create_contact(
        self,
        customer_id: str,
        *,
        name: str,
        email: str | None = None,
        phone: str | None = None,
        department: str | None = None,
        role: str = "other",
        added_by: str,
        is_primary: bool = False,
    ) -> dict[str, Any]:
        self._customer(customer_id)
        actor = self._actor(added_by, field="added_by")
        normalized_role = str(role or "").strip().lower()
        if normalized_role not in _CONTACT_ROLES:
            raise FinanceValidationError("contact role is unsupported")
        record = {
            "id": f"cct_{uuid.uuid4().hex}",
            "customer_id": customer_id,
            "name": _text(name, field="name", required=True, limit=300),
            "email": self._validate_email(email),
            "phone": _text(phone, field="phone", limit=50),
            "department": _text(department, field="department", limit=200),
            "role": normalized_role,
            "is_primary": bool(is_primary),
            "active_status": "ACTIVE",
            "created_by": actor,
        }
        if is_primary:
            for old in self.core.store.list_customer_contacts(customer_id):
                if old.get("role") == normalized_role and old.get("is_primary"):
                    old["is_primary"] = False
                    self.core.store.update_customer_contact(old)
        return self.core.store.put_customer_contact(record)

    def list_contacts(
        self,
        customer_id: str,
        *,
        include_sensitive: bool = False,
        accessed_by: str | None = None,
    ) -> list[dict[str, Any]]:
        self._customer(customer_id)
        actor = None
        if include_sensitive:
            actor = self._assert_sensitive_access(accessed_by or "")
            self._record_access(customer_id, actor=actor, action="customer.contacts.view")
        return [
            self._safe_contact(row, include_sensitive=include_sensitive)
            for row in self.core.store.list_customer_contacts(customer_id)
        ]

    def update_contact(
        self,
        contact_id: str,
        *,
        name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        department: str | None = None,
        role: str | None = None,
        is_primary: bool | None = None,
        updated_by: str,
    ) -> dict[str, Any]:
        actor = self._actor(updated_by, field="updated_by")
        current = self.core.store.get_customer_contact(contact_id)
        if current is None:
            raise FinanceNotFoundError(f"Customer contact not found: {contact_id}")
        updated = deepcopy(current)
        if name is not None:
            updated["name"] = _text(name, field="name", required=True, limit=300)
        if email is not None:
            updated["email"] = self._validate_email(email)
        if phone is not None:
            updated["phone"] = _text(phone, field="phone", limit=50)
        if department is not None:
            updated["department"] = _text(department, field="department", limit=200)
        if role is not None:
            normalized_role = str(role).strip().lower()
            if normalized_role not in _CONTACT_ROLES:
                raise FinanceValidationError("contact role is unsupported")
            updated["role"] = normalized_role
        if is_primary is not None:
            updated["is_primary"] = bool(is_primary)
        updated["updated_by"] = actor
        if updated.get("is_primary"):
            for old in self.core.store.list_customer_contacts(current["customer_id"]):
                if old["id"] != contact_id and old.get("role") == updated.get("role") and old.get("is_primary"):
                    old["is_primary"] = False
                    self.core.store.update_customer_contact(old)
        return self.core.store.update_customer_contact(updated)

    def archive_contact(self, contact_id: str, *, archived_by: str) -> dict[str, Any]:
        actor = self._actor(archived_by, field="archived_by")
        current = self.core.store.get_customer_contact(contact_id)
        if current is None:
            raise FinanceNotFoundError(f"Customer contact not found: {contact_id}")
        current["active_status"] = "INACTIVE"
        current["archived_by"] = actor
        return self.core.store.update_customer_contact(current)

    def add_relationship(
        self,
        parent_customer_id: str,
        child_customer_id: str,
        *,
        relationship_type: str,
        added_by: str,
    ) -> dict[str, Any]:
        self._customer(parent_customer_id)
        self._customer(child_customer_id)
        if parent_customer_id == child_customer_id:
            raise FinanceConflictError("A customer cannot relate to itself")
        actor = self._actor(added_by, field="added_by")
        normalized = str(relationship_type or "").strip().upper()
        if normalized not in _RELATIONSHIP_TYPES:
            raise FinanceValidationError("relationship_type is unsupported")
        record = {
            "id": f"crel_{uuid.uuid4().hex}",
            "parent_customer_id": parent_customer_id,
            "child_customer_id": child_customer_id,
            "relationship_type": normalized,
            "active_status": "ACTIVE",
            "added_by": actor,
        }
        return self.core.store.put_customer_relationship(record)

    def list_relationships(
        self,
        customer_id: str,
        *,
        relationship_type: str | None = None,
    ) -> list[dict[str, Any]]:
        self._customer(customer_id)
        normalized = str(relationship_type or "").strip().upper() or None
        if normalized and normalized not in _RELATIONSHIP_TYPES:
            raise FinanceValidationError("relationship_type is unsupported")
        return self.core.store.list_customer_relationships(
            customer_id, relationship_type=normalized
        )

    def suggest_duplicates(
        self,
        customer_id: str,
        *,
        minimum_score: int = 40,
    ) -> list[dict[str, Any]]:
        source = self._customer(customer_id)
        if minimum_score < 0 or minimum_score > 100:
            raise FinanceValidationError("minimum_score must be between 0 and 100")
        source_profile = source.get("billing_profile") or {}
        source_name = _normalize(source.get("legal_name"))
        source_billing = _normalize(source_profile.get("billing_name"))
        source_email = _normalize(source_profile.get("email"))
        source_address = _normalize(source_profile.get("billing_address"))
        source_corporate = str(source.get("corporate_number") or "")
        candidates: list[dict[str, Any]] = []
        for candidate in self.core.store.list_customers():
            if candidate["id"] == customer_id or candidate.get("active_status") != "ACTIVE":
                continue
            profile = candidate.get("billing_profile") or {}
            score = 0
            reasons: list[str] = []
            if source_corporate and source_corporate == str(candidate.get("corporate_number") or ""):
                score += 70
                reasons.append("corporate_number")
            candidate_name = _normalize(candidate.get("legal_name"))
            if source_name and source_name == candidate_name:
                score += 35
                reasons.append("legal_name")
            elif source_name and candidate_name:
                similarity = int(difflib.SequenceMatcher(None, source_name, candidate_name).ratio() * 30)
                if similarity >= 18:
                    score += similarity
                    reasons.append("legal_name_similarity")
            if source_billing and source_billing == _normalize(profile.get("billing_name")):
                score += 20
                reasons.append("billing_name")
            if source_email and source_email == _normalize(profile.get("email")):
                score += 20
                reasons.append("email")
            if source_address and source_address == _normalize(profile.get("billing_address")):
                score += 15
                reasons.append("billing_address")
            if score < minimum_score:
                continue
            candidates.append(
                {
                    "customer_id": candidate["id"],
                    "score": min(score, 100),
                    "reasons": reasons,
                    "customer": self._summary(candidate),
                }
            )
        candidates.sort(key=lambda row: (-row["score"], row["customer_id"]))
        return candidates

    def merge(
        self,
        source_customer_id: str,
        target_customer_id: str,
        *,
        merged_by: str,
        reason: str,
    ) -> dict[str, Any]:
        actor = self._actor(merged_by, field="merged_by")
        note = _text(reason, field="reason", required=True, limit=1000)
        result = self.core.store.merge_customers(
            source_customer_id,
            target_customer_id,
            merged_by=actor,
            reason=note,
        )
        return {
            "source_customer_id": result["source_customer_id"],
            "target_customer_id": result["target_customer_id"],
            "migrated": result["migrated"],
        }

    def record_activity(
        self,
        customer_id: str,
        *,
        activity_type: str,
        summary: str,
        recorded_by: str,
        occurred_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._customer(customer_id)
        actor = self._actor(recorded_by, field="recorded_by")
        normalized_type = _text(activity_type, field="activity_type", required=True, limit=100)
        note = _text(summary, field="summary", required=True, limit=1000)
        if metadata is not None:
            if not isinstance(metadata, dict):
                raise FinanceValidationError("metadata must be an object")
            sensitive = {
                key for key in metadata if key.casefold() in _SENSITIVE_FIELD_NAMES
            }
            if sensitive:
                raise FinanceValidationError("activity metadata cannot contain customer PII")
        record = {
            "id": f"cact_{uuid.uuid4().hex}",
            "customer_id": customer_id,
            "activity_type": normalized_type,
            "summary": note,
            "metadata": deepcopy(metadata or {}),
            "actor": actor,
            "occurred_at": _iso_datetime(occurred_at, field="occurred_at") if occurred_at else _now(),
        }
        stored = self.core.store.put_customer_activity(record)
        return self._safe_activity(stored) | {"metadata": deepcopy(stored.get("metadata") or {})}

    def list_activities(
        self,
        customer_id: str,
        *,
        activity_type: str | None = None,
    ) -> list[dict[str, Any]]:
        self._customer(customer_id)
        return [
            self._safe_activity(row) | {"metadata": deepcopy(row.get("metadata") or {})}
            for row in self.core.store.list_customer_activities(
                customer_id, activity_type=activity_type
            )
        ]

    def customer_dashboard(self, customer_id: str) -> dict[str, Any]:
        customer = self._customer(customer_id)
        contracts = self.core.list_contracts(customer_id)
        invoices = self.core.search_invoices(customer_id=customer_id)
        issued = [row for row in invoices if row.get("document_status") == "ISSUED"]
        outstanding = Decimal("0")
        for invoice in issued:
            outstanding += Decimal(str(self.core.check_payment(invoice["id"])["outstanding"]))
        projects = sorted(
            {
                str(contract["project_id"])
                for contract in contracts
                if contract.get("project_id")
            }
        )
        return {
            "customer_id": customer_id,
            "lifecycle_status": customer.get("lifecycle_status", "ACTIVE"),
            "contract_count": len(contracts),
            "project_ids": projects,
            "invoice_count": len(invoices),
            "issued_invoice_count": len(issued),
            "outstanding_total": _money_text(outstanding),
            "relationship_count": len(self.list_relationships(customer_id)),
            "contact_count": len(self.core.store.list_customer_contacts(customer_id)),
            "activity_count": len(self.core.store.list_customer_activities(customer_id)),
        }

    def import_csv(
        self,
        *,
        csv_text: str,
        apply: bool = False,
        upsert: bool = False,
        imported_by: str | None = None,
    ) -> dict[str, Any]:
        return self.importer.import_csv(
            csv_text=csv_text,
            apply=apply,
            upsert=upsert,
            imported_by=imported_by,
        )

    def create_portal_token(
        self,
        customer_id: str,
        *,
        expires_at: str,
        created_by: str,
    ) -> dict[str, Any]:
        return self.portal.create_token(
            customer_id, expires_at=expires_at, created_by=created_by
        )

    def portal_list_invoices(self, token: str) -> list[dict[str, Any]]:
        return self.portal.list_invoices(token)

    def portal_get_invoice(self, token: str, invoice_id: str) -> dict[str, Any]:
        return self.portal.get_invoice(token, invoice_id)

    def portal_download_invoice(self, token: str, invoice_id: str) -> dict[str, Any]:
        return self.portal.download_invoice(token, invoice_id)

    def revoke_portal_token(self, token_id: str, *, revoked_by: str) -> dict[str, Any]:
        return self.portal.revoke_token(token_id, revoked_by=revoked_by)
