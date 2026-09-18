"""MCP adapter for the standalone Finance Core."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

try:
    from mcp.server.fastmcp import Context, FastMCP
except ImportError:  # pragma: no cover - exercised only on lean installs
    FastMCP = None  # type: ignore[assignment,misc]
    Context = Any  # type: ignore[assignment,misc]

from finance_core.core import FinanceCore
from finance_core.domains.delivery import SmtpEmailTransport
from finance_core.domains.schedules import INVOICE_CALENDAR_NAME
from finance_core.errors import FinanceAuthorizationError, FinanceNotFoundError


def _default_home() -> Path:
    raw_home = os.environ.get("HERMES_HOME")
    if raw_home:
        return Path(raw_home) / "finance-core"
    return Path.home() / ".hermes" / "finance-core"


def _project_fields(record: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """Project a Finance Core record without exposing unspecified fields."""

    return {key: record[key] for key in fields if key in record}


def _public_master(record: dict[str, Any]) -> dict[str, Any]:
    """Keep sensitive master fields inside Finance Core by default."""

    return _project_fields(
        record,
        (
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
            "qualified_invoice_issuer",
        ),
    )


def _public_customer_contact(record: dict[str, Any], *, include_sensitive: bool = False) -> dict[str, Any]:
    if include_sensitive:
        return _project_fields(
            record,
            (
                "id",
                "customer_id",
                "name",
                "email",
                "phone",
                "department",
                "role",
                "is_primary",
                "active_status",
                "created_by",
                "updated_by",
            ),
        )
    return _project_fields(record, ("id", "customer_id", "role", "is_primary", "active_status"))


def _public_customer_relationship(record: dict[str, Any]) -> dict[str, Any]:
    return _project_fields(
        record,
        (
            "id",
            "parent_customer_id",
            "child_customer_id",
            "relationship_type",
            "active_status",
            "added_by",
        ),
    )


def _public_customer_activity(record: dict[str, Any]) -> dict[str, Any]:
    return _project_fields(
        record,
        ("id", "customer_id", "activity_type", "summary", "metadata", "actor", "occurred_at"),
    )


def _public_contract(record: dict[str, Any]) -> dict[str, Any]:
    return _project_fields(
        record,
        (
            "id",
            "customer_id",
            "name",
            "effective_from",
            "effective_to",
            "currency",
            "payment_due_rule",
            "default_template_id",
            "project_id",
            "department_id",
            "active_status",
        ),
    )


def _public_billing_rule(record: dict[str, Any]) -> dict[str, Any]:
    return _project_fields(
        record,
        (
            "id",
            "contract_id",
            "version",
            "rule_type",
            "description",
            "unit",
            "unit_price",
            "tax_category",
            "tax_rate",
            "effective_from",
            "effective_to",
            "active_status",
        ),
    )


def _public_revenue_entry(record: dict[str, Any]) -> dict[str, Any]:
    """Return ledger dimensions without descriptions or customer billing PII."""

    return _project_fields(
        record,
        (
            "id",
            "customer_id",
            "recognition_date",
            "service_period",
            "amount",
            "currency",
            "source_type",
            "source_id",
            "invoice_id",
            "project_id",
            "department_id",
            "status",
        ),
    )


def _public_policy(record: dict[str, Any]) -> dict[str, Any]:
    return _project_fields(
        record,
        (
            "id",
            "actor_id",
            "issuer_id",
            "role",
            "can_approve",
            "can_issue",
            "can_send",
            "max_amount",
            "separation_required",
            "active_status",
        ),
    )


def _public_period(record: dict[str, Any]) -> dict[str, Any]:
    return _project_fields(
        record,
        ("period", "status", "closed_by", "closed_at", "reopened_by", "reopened_at", "reason"),
    )


def _public_outbox(record: dict[str, Any]) -> dict[str, Any]:
    return _project_fields(
        record,
        (
            "id",
            "delivery_id",
            "invoice_id",
            "idempotency_key",
            "status",
            "attempts",
            "available_at",
            "last_error",
            "created_at",
            "updated_at",
        ),
    )


def _public_work_entry(record: dict[str, Any]) -> dict[str, Any]:
    return _project_fields(
        record,
        (
            "id",
            "source_id",
            "customer_id",
            "contract_id",
            "billing_rule_id",
            "service_date",
            "service_period",
            "quantity",
            "unit",
            "project_id",
            "status",
            "billed_invoice_id",
        ),
    )


def _public_calendar_connection(record: dict[str, Any]) -> dict[str, Any]:
    return _project_fields(
        record,
        (
            "id",
            "provider",
            "calendar_id",
            "calendar_name",
            "timezone",
            "status",
            "last_synced_at",
            "created_at",
            "updated_at",
        ),
    )


def _public_schedule(record: dict[str, Any]) -> dict[str, Any]:
    """Expose operational schedule facts without calendar free-text details."""

    return _project_fields(
        record,
        (
            "id",
            "customer_id",
            "contract_id",
            "billing_rule_id",
            "project_id",
            "starts_at",
            "ends_at",
            "timezone",
            "planned_quantity",
            "actual_quantity",
            "actual_starts_at",
            "actual_ends_at",
            "actual_duration_seconds",
            "status",
            "mapping_status",
            "billable_status",
            "source_type",
            "source_id",
            "calendar_connection_id",
            "external_event_id",
            "occurrence_start",
            "work_entry_id",
        ),
    )


def _public_billing_run(record: dict[str, Any]) -> dict[str, Any]:
    """Expose billing-run state without returning schedule free text."""

    return _project_fields(
        record,
        (
            "id",
            "calendar_connection_id",
            "billing_rule_id",
            "service_period",
            "billing_key",
            "status",
            "schedule_ids",
            "work_entry_ids",
            "invoice_id",
            "finalized_by",
            "finalized_at",
            "draft_created_by",
            "draft_created_at",
            "created_at",
            "updated_at",
        ),
    )


def _public_invoice(record: dict[str, Any]) -> dict[str, Any]:
    """Return invoice facts without replaying master PII into the agent context."""

    return _project_fields(
        record,
        (
            "id",
            "invoice_number",
            "issuer_id",
            "customer_id",
            "template_id",
            "template_version",
            "document_type",
            "created_by",
            "contract_id",
            "billing_rule_version_id",
            "work_entry_ids",
            "withholding_tax_rate",
            "withholding_tax_base",
            "converted_from",
            "correction_of",
            "correction_reason",
            "issue_date",
            "service_period",
            "due_date",
            "currency",
            "lines",
            "totals",
            "notes",
            "document_status",
            "delivery_status",
            "settlement_status",
            "approved_at",
            "approved_by",
            "issued_at",
            "issued_by",
            "path",
            "sha256",
            "document",
            "revision",
        ),
    )


def _public_invoice_summary(record: dict[str, Any]) -> dict[str, Any]:
    """Return searchable invoice facts without rendering master PII."""

    return _project_fields(
        record,
        (
            "id",
            "invoice_number",
            "issuer_id",
            "customer_id",
            "document_type",
            "contract_id",
            "billing_rule_version_id",
            "converted_from",
            "correction_of",
            "issue_date",
            "service_period",
            "due_date",
            "currency",
            "totals",
            "document_status",
            "delivery_status",
            "settlement_status",
        ),
    )


def _public_delivery(record: dict[str, Any]) -> dict[str, Any]:
    return _project_fields(
        record,
        (
            "id",
            "invoice_id",
            "method",
            "status",
            "approved_by",
            "external_reference",
            "downloaded_at",
            "queued_at",
            "sent_at",
            "delivered_at",
            "created_at",
            "updated_at",
            "attempt_count",
            "max_attempts",
            "next_attempt_at",
            "last_error",
        ),
    )


_SESSION_IDENTITY_META_KEY = "hermes_session_user_id"


def _request_actor(context: Context | None) -> str:
    """Read the per-call actor metadata supplied by the Hermes MCP client."""

    if context is None:
        return ""
    try:
        meta = context.request_context.meta
    except (AttributeError, ValueError):
        return ""
    extras = getattr(meta, "model_extra", None) or {}
    return str(extras.get(_SESSION_IDENTITY_META_KEY, "") or "").strip()


def _authenticated_session_actor(context: Context | None = None) -> str:
    """Return the actor bound to this MCP call, never a model-supplied label."""

    # A live MCP request must carry the actor in request metadata.  The
    # environment fallback is retained only for direct embedded/unit callers
    # that do not have an MCP request context.
    session_actor = _request_actor(context)
    if context is None:
        session_actor = os.environ.get("HERMES_SESSION_USER_ID", "").strip()
    if not session_actor:
        raise FinanceAuthorizationError(
            "an authenticated HERMES_SESSION_USER_ID session is required"
        )
    return session_actor


def _authenticated_actor(
    requested: str | None,
    *,
    field: str,
    context: Context | None = None,
) -> str:
    """Bind approval/write labels to the Hermes session identity."""

    session_actor = _authenticated_session_actor(context)
    if str(requested or "").strip() != session_actor:
        raise FinanceAuthorizationError(f"{field} does not match the authenticated session")
    return session_actor


def create_server(*, home: str | Path | None = None, core: FinanceCore | None = None):
    if FastMCP is None:
        raise ImportError(
            "Finance Core MCP requires the optional mcp package; install the Hermes MCP extra"
        )
    service = core or FinanceCore(
        home or _default_home(),
        email_transport=SmtpEmailTransport.from_environment(),
        require_approval_policy=True,
    )
    server = FastMCP(
        "hermes-finance-core",
        instructions=(
            "Finance Core is the source of truth for customer, invoice, tax, "
            "document, and audit data. Never store financial PII in Hermes Memory. "
            "Invoice issue and external delivery are separate operations. "
            "Invoice drafts sourced from schedules must use the 「訪問薬剤管理」 "
            "connection and require work-entry approval."
        ),
    )

    @server.tool(name="finance.calendar.connection_create")
    def calendar_connection_create(
        provider: str,
        timezone: str,
        created_by: str,
        calendar_id: str | None = None,
        calendar_name: str = INVOICE_CALENDAR_NAME,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(created_by, field="created_by", context=ctx)
        return _public_calendar_connection(
            service.create_calendar_connection(
                provider=provider,
                calendar_id=calendar_id,
                calendar_name=calendar_name,
                timezone=timezone,
                created_by=actor,
            )
        )

    @server.tool(name="finance.calendar.connection_list")
    def calendar_connection_list() -> list[dict[str, Any]]:
        return [_public_calendar_connection(record) for record in service.list_calendar_connections()]

    @server.tool(name="finance.calendar.connection_get")
    def calendar_connection_get(connection_id: str) -> dict[str, Any]:
        return _public_calendar_connection(service.get_calendar_connection(connection_id))

    @server.tool(name="finance.calendar.sync")
    def calendar_sync(
        connection_id: str,
        synced_by: str,
        time_min: str | None = None,
        time_max: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(synced_by, field="synced_by", context=ctx)
        return service.sync_calendar(
            connection_id,
            synced_by=actor,
            time_min=time_min,
            time_max=time_max,
        )

    @server.tool(name="finance.schedule.create")
    def schedule_create(
        title: str,
        starts_at: str,
        ends_at: str,
        timezone: str,
        created_by: str,
        customer_id: str | None = None,
        contract_id: str | None = None,
        billing_rule_id: str | None = None,
        project_id: str | None = None,
        planned_quantity: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(created_by, field="created_by", context=ctx)
        return _public_schedule(
            service.create_schedule(
                title=title,
                starts_at=starts_at,
                ends_at=ends_at,
                timezone=timezone,
                created_by=actor,
                customer_id=customer_id,
                contract_id=contract_id,
                billing_rule_id=billing_rule_id,
                project_id=project_id,
                planned_quantity=planned_quantity,
            )
        )

    @server.tool(name="finance.schedule.list")
    def schedule_list(
        customer_id: str | None = None,
        status: str | None = None,
        from_time: str | None = None,
        to_time: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            _public_schedule(
                record
            )
            for record in service.list_schedules(
                customer_id=customer_id,
                status=status,
                from_time=from_time,
                to_time=to_time,
            )
        ]

    @server.tool(name="finance.schedule.get")
    def schedule_get(schedule_id: str) -> dict[str, Any]:
        return _public_schedule(service.get_schedule(schedule_id))

    @server.tool(name="finance.schedule.link")
    def schedule_link(
        schedule_id: str,
        customer_id: str,
        contract_id: str,
        billing_rule_id: str,
        linked_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(linked_by, field="linked_by", context=ctx)
        return _public_schedule(
            service.link_schedule(
                schedule_id,
                customer_id=customer_id,
                contract_id=contract_id,
                billing_rule_id=billing_rule_id,
                linked_by=actor,
            )
        )

    @server.tool(name="finance.schedule.complete")
    def schedule_complete(
        schedule_id: str,
        actual_starts_at: str,
        actual_ends_at: str,
        completed_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(completed_by, field="completed_by", context=ctx)
        return _public_schedule(
            service.complete_schedule(
                schedule_id,
                actual_starts_at=actual_starts_at,
                actual_ends_at=actual_ends_at,
                completed_by=actor,
            )
        )

    @server.tool(name="finance.schedule.cancel")
    def schedule_cancel(
        schedule_id: str,
        reason: str,
        cancelled_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(cancelled_by, field="cancelled_by", context=ctx)
        return _public_schedule(
            service.cancel_schedule(schedule_id, cancelled_by=actor, reason=reason)
        )

    @server.tool(name="finance.billing_run.preview")
    def billing_run_preview(
        calendar_connection_id: str,
        billing_rule_id: str,
        service_period: str,
    ) -> dict[str, Any]:
        return service.preview_billing_run(
            calendar_connection_id=calendar_connection_id,
            billing_rule_id=billing_rule_id,
            service_period=service_period,
        )

    @server.tool(name="finance.billing_run.finalize")
    def billing_run_finalize(
        calendar_connection_id: str,
        billing_rule_id: str,
        service_period: str,
        schedule_ids: list[str],
        finalized_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(finalized_by, field="finalized_by", context=ctx)
        return _public_billing_run(
            service.finalize_billing_run(
                calendar_connection_id=calendar_connection_id,
                billing_rule_id=billing_rule_id,
                service_period=service_period,
                schedule_ids=schedule_ids,
                finalized_by=actor,
            )
        )

    @server.tool(name="finance.billing_run.get")
    def billing_run_get(run_id: str) -> dict[str, Any]:
        return _public_billing_run(service.get_billing_run(run_id))

    @server.tool(name="finance.billing_run.list")
    def billing_run_list(
        calendar_connection_id: str | None = None,
        service_period: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            _public_billing_run(record)
            for record in service.list_billing_runs(
                calendar_connection_id=calendar_connection_id,
                service_period=service_period,
            )
        ]

    @server.tool(name="finance.customer.create")
    def customer_create(
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
        """Create a customer using Finance Core billing-profile field names.

        Common natural-language aliases ``legal_name`` and ``address`` inside
        ``billing_profile`` are normalized to ``billing_name`` and
        ``billing_address``. Postal code and fax are stored in Finance Core;
        unsupported keys are rejected before persistence.
        """
        return _public_master(service.create_customer(
            customer_kind=customer_kind,
            legal_name=legal_name,
            billing_profile=billing_profile,
            corporate_number=corporate_number,
            external_id=external_id,
            owner_id=owner_id,
            tags=tags,
            lifecycle_status=lifecycle_status,
            contacts=contacts,
        ))

    @server.tool(name="finance.customer.get")
    def customer_get(customer_id: str) -> dict[str, Any]:
        try:
            return _public_master(service.get_customer(customer_id))
        except FinanceNotFoundError:
            return {"error": "customer not found"}

    @server.tool(name="finance.customer.search")
    def customer_search(
        query: str | None = None,
        customer_kind: str | None = None,
        active_status: str | None = None,
        lifecycle_status: str | None = None,
        owner_id: str | None = None,
        tag: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            _public_master(record)
            for record in service.search_customers(
                query=query,
                customer_kind=customer_kind,
                active_status=active_status,
                lifecycle_status=lifecycle_status,
                owner_id=owner_id,
                tag=tag,
            )
        ]

    @server.tool(name="finance.customer.update")
    def customer_update(
        customer_id: str,
        updated_by: str,
        legal_name: str | None = None,
        customer_kind: str | None = None,
        billing_profile: dict[str, Any] | None = None,
        aliases: list[str] | None = None,
        corporate_number: str | None = None,
        external_id: str | None = None,
        owner_id: str | None = None,
        tags: list[str] | None = None,
        lifecycle_status: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(updated_by, field="updated_by", context=ctx)
        return _public_master(
            service.update_customer(
                customer_id,
                legal_name=legal_name,
                customer_kind=customer_kind,
                billing_profile=billing_profile,
                aliases=aliases,
                corporate_number=corporate_number,
                external_id=external_id,
                owner_id=owner_id,
                tags=tags,
                lifecycle_status=lifecycle_status,
                updated_by=actor,
            )
        )

    @server.tool(name="finance.customer.archive")
    def customer_archive(
        customer_id: str,
        archived_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(archived_by, field="archived_by", context=ctx)
        return _public_master(service.archive_customer(customer_id, archived_by=actor))

    @server.tool(name="finance.customer.revisions")
    def customer_revisions(customer_id: str) -> list[dict[str, Any]]:
        # Revision payloads contain billing addresses and contacts.  Expose
        # audit metadata only; the full snapshot remains inside Finance Core.
        return [
            {
                key: revision[key]
                for key in ("customer_id", "revision", "actor", "created_at")
                if key in revision
            }
            for revision in service.list_customer_revisions(customer_id)
        ]

    @server.tool(name="finance.customer.get_sensitive")
    def customer_get_sensitive(
        customer_id: str,
        accessed_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(accessed_by, field="accessed_by", context=ctx)
        return service.get_customer(customer_id, include_sensitive=True, accessed_by=actor)

    @server.tool(name="finance.customer.contact_create")
    def customer_contact_create(
        customer_id: str,
        name: str,
        added_by: str,
        email: str | None = None,
        phone: str | None = None,
        department: str | None = None,
        role: str = "other",
        is_primary: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(added_by, field="added_by", context=ctx)
        return _public_customer_contact(
            service.create_customer_contact(
                customer_id,
                name=name,
                email=email,
                phone=phone,
                department=department,
                role=role,
                is_primary=is_primary,
                added_by=actor,
            )
        )

    @server.tool(name="finance.customer.contact_list")
    def customer_contact_list(
        customer_id: str,
        include_sensitive: bool = False,
        accessed_by: str | None = None,
        ctx: Context | None = None,
    ) -> list[dict[str, Any]]:
        actor = None
        if include_sensitive:
            actor = _authenticated_actor(accessed_by, field="accessed_by", context=ctx)
        return [
            _public_customer_contact(row, include_sensitive=include_sensitive)
            for row in service.list_customer_contacts(
                customer_id,
                include_sensitive=include_sensitive,
                accessed_by=actor,
            )
        ]

    @server.tool(name="finance.customer.contact_update")
    def customer_contact_update(
        contact_id: str,
        updated_by: str,
        name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        department: str | None = None,
        role: str | None = None,
        is_primary: bool | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(updated_by, field="updated_by", context=ctx)
        return _public_customer_contact(
            service.update_customer_contact(
                contact_id,
                name=name,
                email=email,
                phone=phone,
                department=department,
                role=role,
                is_primary=is_primary,
                updated_by=actor,
            )
        )

    @server.tool(name="finance.customer.contact_archive")
    def customer_contact_archive(
        contact_id: str,
        archived_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(archived_by, field="archived_by", context=ctx)
        return _public_customer_contact(
            service.archive_customer_contact(contact_id, archived_by=actor)
        )

    @server.tool(name="finance.customer.lifecycle_update")
    def customer_lifecycle_update(
        customer_id: str,
        status: str,
        reason: str,
        updated_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(updated_by, field="updated_by", context=ctx)
        return _public_master(
            service.update_customer_lifecycle(
                customer_id, status=status, reason=reason, updated_by=actor
            )
        )

    @server.tool(name="finance.customer.payment_settings_update")
    def customer_payment_settings_update(
        customer_id: str,
        updated_by: str,
        credit_limit: str | None = None,
        credit_currency: str | None = None,
        payment_due_rule: dict[str, Any] | None = None,
        closing_day: int | str | None = None,
        bank_fee_payer: str | None = None,
        dunning_policy: dict[str, Any] | None = None,
        default_tax_category: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(updated_by, field="updated_by", context=ctx)
        updated = service.update_customer_payment_settings(
            customer_id,
            credit_limit=credit_limit,
            credit_currency=credit_currency,
            payment_due_rule=payment_due_rule,
            closing_day=closing_day,
            bank_fee_payer=bank_fee_payer,
            dunning_policy=dunning_policy,
            default_tax_category=default_tax_category,
            updated_by=actor,
        )
        return _public_master(updated)

    @server.tool(name="finance.customer.relationship_add")
    def customer_relationship_add(
        parent_customer_id: str,
        child_customer_id: str,
        relationship_type: str,
        added_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(added_by, field="added_by", context=ctx)
        return _public_customer_relationship(
            service.add_customer_relationship(
                parent_customer_id,
                child_customer_id,
                relationship_type=relationship_type,
                added_by=actor,
            )
        )

    @server.tool(name="finance.customer.relationship_list")
    def customer_relationship_list(
        customer_id: str,
        relationship_type: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            _public_customer_relationship(row)
            for row in service.list_customer_relationships(
                customer_id, relationship_type=relationship_type
            )
        ]

    @server.tool(name="finance.customer.duplicate_suggest")
    def customer_duplicate_suggest(
        customer_id: str,
        minimum_score: int = 40,
    ) -> list[dict[str, Any]]:
        return service.suggest_customer_duplicates(
            customer_id, minimum_score=minimum_score
        )

    @server.tool(name="finance.customer.merge")
    def customer_merge(
        source_customer_id: str,
        target_customer_id: str,
        reason: str,
        merged_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(merged_by, field="merged_by", context=ctx)
        return service.merge_customers(
            source_customer_id,
            target_customer_id,
            merged_by=actor,
            reason=reason,
        )

    @server.tool(name="finance.customer.activity_record")
    def customer_activity_record(
        customer_id: str,
        activity_type: str,
        summary: str,
        recorded_by: str,
        occurred_at: str | None = None,
        metadata: dict[str, Any] | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(recorded_by, field="recorded_by", context=ctx)
        return _public_customer_activity(
            service.record_customer_activity(
                customer_id,
                activity_type=activity_type,
                summary=summary,
                recorded_by=actor,
                occurred_at=occurred_at,
                metadata=metadata,
            )
        )

    @server.tool(name="finance.customer.activity_list")
    def customer_activity_list(
        customer_id: str,
        activity_type: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            _public_customer_activity(row)
            for row in service.list_customer_activities(
                customer_id, activity_type=activity_type
            )
        ]

    @server.tool(name="finance.customer.dashboard")
    def customer_dashboard(customer_id: str) -> dict[str, Any]:
        return service.customer_dashboard(customer_id)

    @server.tool(name="finance.customer.import_csv")
    def customer_import_csv(
        csv_text: str,
        apply: bool = False,
        upsert: bool = False,
        imported_by: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = None
        if apply:
            actor = _authenticated_actor(imported_by, field="imported_by", context=ctx)
        return service.import_customers_csv(
            csv_text=csv_text,
            apply=apply,
            upsert=upsert,
            imported_by=actor,
        )

    @server.tool(name="finance.customer.portal_token_create")
    def customer_portal_token_create(
        customer_id: str,
        expires_at: str,
        created_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(created_by, field="created_by", context=ctx)
        return service.create_customer_portal_token(
            customer_id, expires_at=expires_at, created_by=actor
        )

    @server.tool(name="finance.customer.portal_invoice_list")
    def customer_portal_invoice_list(token: str) -> list[dict[str, Any]]:
        return service.portal_list_invoices(token)

    @server.tool(name="finance.customer.portal_invoice_get")
    def customer_portal_invoice_get(token: str, invoice_id: str) -> dict[str, Any]:
        return service.portal_get_invoice(token, invoice_id)

    @server.tool(name="finance.customer.portal_invoice_download")
    def customer_portal_invoice_download(token: str, invoice_id: str) -> dict[str, Any]:
        return service.portal_download_invoice(token, invoice_id)

    @server.tool(name="finance.customer.portal_token_revoke")
    def customer_portal_token_revoke(
        token_id: str,
        revoked_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(revoked_by, field="revoked_by", context=ctx)
        return service.revoke_customer_portal_token(token_id, revoked_by=actor)

    @server.tool(name="finance.customer.access_events")
    def customer_access_events(customer_id: str) -> list[dict[str, Any]]:
        return service.list_customer_access_events(customer_id)

    @server.tool(name="finance.approval.bootstrap_self")
    def approval_bootstrap_self(ctx: Context | None = None) -> dict[str, Any]:
        """Bootstrap the current authenticated session as the first admin."""

        actor = _authenticated_session_actor(ctx)
        existing = service.list_approval_policies(actor_id=actor)
        for policy in existing:
            if (
                policy.get("role") == "admin"
                and policy.get("active_status") == "ACTIVE"
                and policy.get("can_approve")
                and policy.get("can_issue")
                and policy.get("can_send")
            ):
                return _public_policy(policy)
        service.approvals.authorize_policy_configuration(
            configured_by=actor,
            actor_id=actor,
        )
        return _public_policy(
            service.set_approval_policy(
                actor_id=actor,
                role="admin",
                can_approve=True,
                can_issue=True,
                can_send=True,
                configured_by=actor,
            )
        )

    @server.tool(name="finance.approval.policy_set")
    def approval_policy_set(
        actor_id: str,
        role: str,
        configured_by: str,
        can_approve: bool = False,
        can_issue: bool = False,
        can_send: bool = False,
        max_amount: str | None = None,
        separation_required: bool = False,
        issuer_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(configured_by, field="configured_by", context=ctx)
        service.approvals.authorize_policy_configuration(
            configured_by=actor,
            actor_id=actor_id,
        )
        return _public_policy(
            service.set_approval_policy(
                actor_id=actor_id,
                role=role,
                can_approve=can_approve,
                can_issue=can_issue,
                can_send=can_send,
                max_amount=max_amount,
                separation_required=separation_required,
                issuer_id=issuer_id,
                configured_by=actor,
            )
        )

    @server.tool(name="finance.approval.policy_list")
    def approval_policy_list(actor_id: str | None = None) -> list[dict[str, Any]]:
        return [_public_policy(record) for record in service.list_approval_policies(actor_id=actor_id)]

    @server.tool(name="finance.period.close")
    def period_close(
        period: str,
        closed_by: str,
        reason: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(closed_by, field="closed_by", context=ctx)
        return _public_period(service.close_period(period, closed_by=actor, reason=reason))

    @server.tool(name="finance.period.reopen")
    def period_reopen(
        period: str,
        reopened_by: str,
        reason: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(reopened_by, field="reopened_by", context=ctx)
        return _public_period(service.reopen_period(period, reopened_by=actor, reason=reason))

    @server.tool(name="finance.period.status")
    def period_status(period: str) -> dict[str, Any]:
        return _public_period(service.period_status(period))

    @server.tool(name="finance.period.list")
    def period_list() -> list[dict[str, Any]]:
        return [_public_period(record) for record in service.list_periods()]

    @server.tool(name="finance.contract.create")
    def contract_create(
        customer_id: str,
        name: str,
        effective_from: str,
        created_by: str,
        effective_to: str | None = None,
        currency: str = "JPY",
        payment_due_rule: dict[str, Any] | None = None,
        default_template_id: str | None = None,
        project_id: str | None = None,
        department_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(created_by, field="created_by", context=ctx)
        return _public_contract(
            service.create_contract(
                customer_id=customer_id,
                name=name,
                effective_from=effective_from,
                effective_to=effective_to,
                currency=currency,
                payment_due_rule=payment_due_rule,
                default_template_id=default_template_id,
                project_id=project_id,
                department_id=department_id,
                created_by=actor,
            )
        )

    @server.tool(name="finance.contract.get")
    def contract_get(contract_id: str) -> dict[str, Any]:
        return _public_contract(service.get_contract(contract_id))

    @server.tool(name="finance.contract.list")
    def contract_list(customer_id: str | None = None) -> list[dict[str, Any]]:
        return [_public_contract(record) for record in service.list_contracts(customer_id)]

    @server.tool(name="finance.billing_rule.create")
    def billing_rule_create(
        contract_id: str,
        rule_type: str,
        description: str,
        unit: str,
        unit_price: str,
        tax_category: str,
        effective_from: str,
        created_by: str,
        effective_to: str | None = None,
        version: int = 1,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(created_by, field="created_by", context=ctx)
        return _public_billing_rule(
            service.create_billing_rule(
                contract_id=contract_id,
                rule_type=rule_type,
                description=description,
                unit=unit,
                unit_price=unit_price,
                tax_category=tax_category,
                effective_from=effective_from,
                effective_to=effective_to,
                version=version,
                created_by=actor,
            )
        )

    @server.tool(name="finance.billing_rule.get")
    def billing_rule_get(billing_rule_id: str) -> dict[str, Any]:
        return _public_billing_rule(service.get_billing_rule(billing_rule_id))

    @server.tool(name="finance.billing_rule.list")
    def billing_rule_list(contract_id: str | None = None) -> list[dict[str, Any]]:
        return [_public_billing_rule(record) for record in service.list_billing_rules(contract_id)]

    @server.tool(name="finance.issuer.create")
    def issuer_create(
        legal_name: str,
        qualified_invoice_issuer: bool = False,
        registration_number: str | None = None,
        address: str | None = None,
        bank_account: dict[str, Any] | None = None,
        tax_rounding_policy: str = "HALF_UP",
    ) -> dict[str, Any]:
        return _public_master(service.create_issuer(
            legal_name=legal_name,
            qualified_invoice_issuer=qualified_invoice_issuer,
            registration_number=registration_number,
            address=address,
            bank_account=bank_account,
            tax_rounding_policy=tax_rounding_policy,
        ))

    @server.tool(name="finance.template.list_versions")
    def template_list_versions(template_id: str | None = None) -> list[dict[str, Any]]:
        return service.list_template_versions(template_id)

    @server.tool(name="finance.invoice.draft_create")
    def invoice_draft_create(
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
        withholding_tax_rate: str | None = None,
        withholding_tax_base: str = "subtotal",
    ) -> dict[str, Any]:
        return _public_invoice(service.draft_create(
            issuer_id=issuer_id,
            customer_id=customer_id,
            template_id=template_id,
            template_version=template_version,
            issue_date=issue_date,
            service_period=service_period,
            due_date=due_date,
            currency=currency,
            lines=lines,
            notes=notes,
            document_type=document_type,
            billing_key=billing_key,
            withholding_tax_rate=withholding_tax_rate,
            withholding_tax_base=withholding_tax_base,
        ))

    @server.tool(name="finance.invoice.draft_from_rule")
    def invoice_draft_from_rule(
        issuer_id: str,
        billing_rule_id: str,
        service_period: str,
        issue_date: str,
        billing_key: str,
        created_by: str,
        quantity: str = "1",
        due_date: str | None = None,
        template_id: str | None = None,
        template_version: str = "1.0.0",
        notes: str | None = None,
        withholding_tax_rate: str | None = None,
        withholding_tax_base: str = "subtotal",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(created_by, field="created_by", context=ctx)
        return _public_invoice(
            service.draft_from_billing_rule(
                issuer_id=issuer_id,
                billing_rule_id=billing_rule_id,
                service_period=service_period,
                issue_date=issue_date,
                quantity=quantity,
                due_date=due_date,
                template_id=template_id,
                template_version=template_version,
                notes=notes,
                billing_key=billing_key,
                created_by=actor,
                withholding_tax_rate=withholding_tax_rate,
                withholding_tax_base=withholding_tax_base,
            )
        )

    @server.tool(name="finance.invoice.convert")
    def invoice_convert(
        source_invoice_id: str,
        target_document_type: str,
        converted_by: str,
        billing_key: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(converted_by, field="converted_by", context=ctx)
        return _public_invoice(
            service.convert_document(
                source_invoice_id,
                target_document_type=target_document_type,
                converted_by=actor,
                billing_key=billing_key,
            )
        )

    @server.tool(name="finance.invoice.correct")
    def invoice_correct(
        invoice_id: str,
        lines: list[dict[str, Any]],
        reason: str,
        corrected_by: str,
        correction_date: str | None = None,
        due_date: str | None = None,
        billing_key: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(corrected_by, field="corrected_by", context=ctx)
        return _public_invoice(
            service.correct_invoice(
                invoice_id,
                lines=lines,
                reason=reason,
                corrected_by=actor,
                correction_date=correction_date,
                due_date=due_date,
                billing_key=billing_key,
            )
        )

    @server.tool(name="finance.work.record")
    def work_record(
        customer_id: str,
        contract_id: str,
        billing_rule_id: str,
        service_date: str,
        quantity: str,
        source_id: str,
        created_by: str,
        description: str | None = None,
        project_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(created_by, field="created_by", context=ctx)
        return _public_work_entry(
            service.record_work_entry(
                customer_id=customer_id,
                contract_id=contract_id,
                billing_rule_id=billing_rule_id,
                service_date=service_date,
                quantity=quantity,
                source_id=source_id,
                created_by=actor,
                description=description,
                project_id=project_id,
            )
        )

    @server.tool(name="finance.work.from_schedule")
    def work_from_schedule(
        schedule_id: str,
        created_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(created_by, field="created_by", context=ctx)
        return _public_work_entry(service.work_from_schedule(schedule_id, created_by=actor))

    @server.tool(name="finance.work.approve")
    def work_approve(
        entry_id: str,
        approved_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(approved_by, field="approved_by", context=ctx)
        return _public_work_entry(service.approve_work_entry(entry_id, approved_by=actor))

    @server.tool(name="finance.work.reject")
    def work_reject(
        entry_id: str,
        reason: str,
        rejected_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(rejected_by, field="rejected_by", context=ctx)
        return _public_work_entry(
            service.reject_work_entry(entry_id, rejected_by=actor, reason=reason)
        )

    @server.tool(name="finance.work.list")
    def work_list(
        customer_id: str | None = None,
        contract_id: str | None = None,
        billing_rule_id: str | None = None,
        service_period: str | None = None,
        unbilled_only: bool = False,
    ) -> list[dict[str, Any]]:
        return [
            _public_work_entry(record)
            for record in service.list_work_entries(
                customer_id=customer_id,
                contract_id=contract_id,
                billing_rule_id=billing_rule_id,
                service_period=service_period,
                unbilled_only=unbilled_only,
            )
        ]

    @server.tool(name="finance.work.import_csv")
    def work_import_csv(
        csv_text: str,
        apply: bool = False,
        imported_by: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Preview or explicitly apply a work-timesheet CSV."""

        actor = imported_by
        if apply:
            try:
                actor = _authenticated_actor(imported_by, field="imported_by", context=ctx)
            except FinanceAuthorizationError as exc:
                return {"success": False, "errors": [{"error": str(exc)}]}
        return service.import_work_csv(
            csv_text=csv_text,
            apply=apply,
            imported_by=actor,
        )

    @server.tool(name="finance.work.draft_from_entries")
    def work_draft_from_entries(
        issuer_id: str,
        billing_rule_id: str,
        service_period: str,
        issue_date: str,
        billing_key: str,
        created_by: str,
        due_date: str | None = None,
        template_id: str | None = None,
        template_version: str = "1.0.0",
        notes: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(created_by, field="created_by", context=ctx)
        return _public_invoice(
            service.draft_from_work_entries(
                issuer_id=issuer_id,
                billing_rule_id=billing_rule_id,
                service_period=service_period,
                issue_date=issue_date,
                billing_key=billing_key,
                created_by=actor,
                due_date=due_date,
                template_id=template_id,
                template_version=template_version,
                notes=notes,
            )
        )

    @server.tool(name="finance.invoice.draft_from_schedule")
    def invoice_draft_from_schedule(
        issuer_id: str,
        calendar_connection_id: str,
        billing_rule_id: str,
        service_period: str,
        issue_date: str,
        billing_key: str,
        created_by: str,
        due_date: str | None = None,
        template_id: str | None = None,
        template_version: str = "1.0.0",
        notes: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(created_by, field="created_by", context=ctx)
        return _public_invoice(
            service.draft_from_schedule_entries(
                issuer_id=issuer_id,
                calendar_connection_id=calendar_connection_id,
                billing_rule_id=billing_rule_id,
                service_period=service_period,
                issue_date=issue_date,
                billing_key=billing_key,
                created_by=actor,
                due_date=due_date,
                template_id=template_id,
                template_version=template_version,
                notes=notes,
            )
        )

    @server.tool(name="finance.invoice.draft_from_billing_run")
    def invoice_draft_from_billing_run(
        run_id: str,
        issuer_id: str,
        issue_date: str,
        created_by: str,
        due_date: str | None = None,
        template_id: str | None = None,
        template_version: str = "1.0.0",
        notes: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(created_by, field="created_by", context=ctx)
        return _public_invoice(
            service.draft_from_billing_run(
                run_id,
                issuer_id=issuer_id,
                issue_date=issue_date,
                created_by=actor,
                due_date=due_date,
                template_id=template_id,
                template_version=template_version,
                notes=notes,
            )
        )

    @server.tool(name="finance.invoice.validate")
    def invoice_validate(invoice_id: str) -> dict[str, Any]:
        return service.validate_invoice(invoice_id)

    @server.tool(name="finance.invoice.preview_pdf")
    def invoice_preview_pdf(invoice_id: str) -> dict[str, Any]:
        return _public_invoice(service.preview_pdf(invoice_id))

    @server.tool(name="finance.invoice.approve")
    def invoice_approve(
        invoice_id: str,
        approved_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(approved_by, field="approved_by", context=ctx)
        return _public_invoice(service.approve_invoice(invoice_id, approved_by=actor))

    @server.tool(name="finance.invoice.issue")
    def invoice_issue(
        invoice_id: str,
        issued_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(issued_by, field="issued_by", context=ctx)
        return _public_invoice(service.issue_invoice(invoice_id, issued_by=actor))

    @server.tool(name="finance.invoice.get")
    def invoice_get(invoice_id: str) -> dict[str, Any]:
        return _public_invoice(service.get_invoice(invoice_id))

    @server.tool(name="finance.invoice.audit_events")
    def invoice_audit_events(invoice_id: str) -> list[dict[str, Any]]:
        return service.list_audit_events(invoice_id)

    @server.tool(name="finance.invoice.bulk_draft_create")
    def invoice_bulk_draft_create(requests: list[dict[str, Any]]) -> dict[str, Any]:
        result = service.bulk_draft_create(requests)
        return {
            "created": [_public_invoice(record) for record in result["created"]],
            "existing": result["existing"],
        }

    @server.tool(name="finance.invoice.search")
    def invoice_search(
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
        return [
            _public_invoice_summary(record)
            for record in service.search_invoices(
                invoice_number=invoice_number,
                customer_id=customer_id,
                document_status=document_status,
                settlement_status=settlement_status,
                issue_date_from=issue_date_from,
                issue_date_to=issue_date_to,
                due_date_from=due_date_from,
                due_date_to=due_date_to,
                amount_min=amount_min,
                amount_max=amount_max,
            )
        ]

    @server.tool(name="finance.receivables.overdue_list")
    def receivables_overdue_list(today: str | None = None) -> list[dict[str, Any]]:
        return service.overdue_invoices(today=today)

    @server.tool(name="finance.receivables.reminder_candidates")
    def receivables_reminder_candidates(
        today: str | None = None,
        reminder_days: int = 0,
    ) -> list[dict[str, Any]]:
        return service.reminder_candidates(today=today, reminder_days=reminder_days)

    @server.tool(name="finance.revenue.summary")
    def revenue_summary(from_date: str, to_date: str) -> dict[str, Any]:
        return service.revenue_summary(from_date=from_date, to_date=to_date)

    @server.tool(name="finance.revenue.by_customer")
    def revenue_by_customer(from_date: str, to_date: str) -> list[dict[str, Any]]:
        return service.revenue_by_customer(from_date=from_date, to_date=to_date)

    @server.tool(name="finance.revenue.by_month")
    def revenue_by_month(from_date: str, to_date: str) -> list[dict[str, Any]]:
        return service.revenue_by_month(from_date=from_date, to_date=to_date)

    @server.tool(name="finance.sales.record")
    def sales_record(
        customer_id: str,
        recognition_date: str,
        service_period: str,
        amount: str,
        currency: str,
        description: str,
        source_type: str,
        source_id: str,
        created_by: str,
        invoice_id: str | None = None,
        project_id: str | None = None,
        department_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(created_by, field="created_by", context=ctx)
        return _public_revenue_entry(
            service.record_revenue(
                customer_id=customer_id,
                recognition_date=recognition_date,
                service_period=service_period,
                amount=amount,
                currency=currency,
                description=description,
                source_type=source_type,
                source_id=source_id,
                invoice_id=invoice_id,
                project_id=project_id,
                department_id=department_id,
                created_by=actor,
            )
        )

    @server.tool(name="finance.sales.search")
    def sales_search(
        from_date: str | None = None,
        to_date: str | None = None,
        customer_id: str | None = None,
        project_id: str | None = None,
        department_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            _public_revenue_entry(record)
            for record in service.search_revenue_entries(
                from_date=from_date,
                to_date=to_date,
                customer_id=customer_id,
                project_id=project_id,
                department_id=department_id,
            )
        ]

    @server.tool(name="finance.revenue.by_project")
    def revenue_by_project(from_date: str, to_date: str) -> list[dict[str, Any]]:
        return service.revenue_by_project(from_date=from_date, to_date=to_date)

    @server.tool(name="finance.dashboard.summary")
    def dashboard_summary(
        from_date: str,
        to_date: str,
        today: str | None = None,
        reminder_days: int = 7,
    ) -> dict[str, Any]:
        return service.dashboard_summary(
            from_date=from_date,
            to_date=to_date,
            today=today,
            reminder_days=reminder_days,
        )

    @server.tool(name="finance.dashboard.receivables_aging")
    def dashboard_receivables_aging(today: str | None = None) -> dict[str, dict[str, Any]]:
        return service.receivables_aging(today=today)

    @server.tool(name="finance.tax.report")
    def tax_report(from_date: str, to_date: str) -> dict[str, Any]:
        return service.tax_report(from_date=from_date, to_date=to_date)

    @server.tool(name="finance.delivery.prepare")
    def delivery_prepare(
        invoice_id: str,
        method: str,
        prepared_by: str,
        recipient: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(prepared_by, field="prepared_by", context=ctx)
        return _public_delivery(
            service.prepare_delivery(
                invoice_id,
                method=method,
                prepared_by=actor,
                recipient=recipient,
            )
        )

    @server.tool(name="finance.delivery.update_status")
    def delivery_update_status(
        delivery_id: str,
        status: str,
        updated_by: str,
        external_reference: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(updated_by, field="updated_by", context=ctx)
        return _public_delivery(
            service.update_delivery(
                delivery_id,
                status=status,
                updated_by=actor,
                external_reference=external_reference,
            )
        )

    @server.tool(name="finance.delivery.list")
    def delivery_list(invoice_id: str | None = None) -> list[dict[str, Any]]:
        return [_public_delivery(record) for record in service.list_deliveries(invoice_id)]

    @server.tool(name="finance.delivery.outbox")
    def delivery_outbox(invoice_id: str | None = None) -> list[dict[str, Any]]:
        return [_public_outbox(record) for record in service.list_delivery_outbox(invoice_id)]

    @server.tool(name="finance.delivery.retry")
    def delivery_retry(
        delivery_id: str,
        retried_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(retried_by, field="retried_by", context=ctx)
        return _public_delivery(service.retry_delivery(delivery_id, retried_by=actor))

    @server.tool(name="finance.delivery.dispatch_email")
    def delivery_dispatch_email(
        delivery_id: str,
        sent_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(sent_by, field="sent_by", context=ctx)
        return service.dispatch_email(delivery_id, sent_by=actor)

    @server.tool(name="finance.delivery.dispatch_discord")
    def delivery_dispatch_discord(
        delivery_id: str,
        sent_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(sent_by, field="sent_by", context=ctx)
        return service.dispatch_discord(delivery_id, sent_by=actor)

    @server.tool(name="finance.delivery.record_download")
    def delivery_record_download(
        delivery_id: str,
        downloaded_at: str | None = None,
    ) -> dict[str, Any]:
        return _public_delivery(
            service.record_delivery_download(delivery_id, downloaded_at=downloaded_at)
        )

    @server.tool(name="finance.accounting.export_csv")
    def accounting_export_csv(from_date: str, to_date: str) -> dict[str, Any]:
        return service.export_accounting_csv(from_date=from_date, to_date=to_date)

    @server.tool(name="finance.accounting.export_bundle")
    def accounting_export_bundle(
        from_date: str,
        to_date: str,
        target: str = "generic",
    ) -> dict[str, Any]:
        return service.export_integration_bundle(
            from_date=from_date,
            to_date=to_date,
            target=target,
        )

    @server.tool(name="finance.accounting.freee_invoice_preview")
    def accounting_freee_invoice_preview(
        invoice_id: str,
        company_id: int,
        partner_id: int,
    ) -> dict[str, Any]:
        """Build a reviewable freee請求書 API payload without network access."""

        return service.freee_invoice_preview(
            invoice_id=invoice_id,
            company_id=company_id,
            partner_id=partner_id,
        )

    @server.tool(name="finance.payment.import_csv")
    def payment_import_csv(
        csv_text: str,
        apply: bool = False,
        imported_by: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Preview or explicitly apply a bank CSV; raw payer fields stay private."""

        actor = imported_by
        if apply:
            try:
                actor = _authenticated_actor(imported_by, field="imported_by", context=ctx)
            except FinanceAuthorizationError as exc:
                return {"success": False, "errors": [{"error": str(exc)}]}
        return service.import_payment_csv(
            csv_text=csv_text,
            apply=apply,
            imported_by=actor,
        )

    @server.tool(name="finance.payment.match_suggest")
    def payment_match_suggest(payment_id: str) -> list[dict[str, Any]]:
        return service.match_payment(payment_id)

    @server.tool(name="finance.payment.allocate")
    def payment_allocate(
        payment_id: str,
        allocations: list[dict[str, Any]],
        approved_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(approved_by, field="approved_by", context=ctx)
        return service.allocate_payment(
            payment_id,
            allocations=allocations,
            approved_by=actor,
        )

    @server.tool(name="finance.payment.unallocated_list")
    def payment_unallocated_list() -> list[dict[str, Any]]:
        return service.unallocated_payments()

    @server.tool(name="finance.payment.check")
    def payment_check(invoice_id: str) -> dict[str, Any]:
        return service.check_payment(invoice_id)

    @server.tool(name="finance.payment.alias_add")
    def payment_alias_add(
        customer_id: str,
        alias: str,
        added_by: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        actor = _authenticated_actor(added_by, field="added_by", context=ctx)
        record = service.add_payment_alias(customer_id, alias, added_by=actor)
        return _project_fields(record, ("id", "customer_id", "active_status"))

    @server.tool(name="finance.payment.alias_list")
    def payment_alias_list(customer_id: str | None = None) -> list[dict[str, Any]]:
        return [
            _project_fields(
                record,
                ("id", "customer_id", "active_status", "created_at", "updated_at"),
            )
            for record in service.list_payment_aliases(customer_id)
        ]

    return server


def main() -> None:
    if FastMCP is None:
        print(
            "Finance Core MCP requires the optional mcp package.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
