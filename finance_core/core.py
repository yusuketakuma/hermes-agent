"""Domain service for the Finance Core source of truth."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from finance_core.documents import DocumentStore
from finance_core.domains.approvals import ApprovalService
from finance_core.domains.billing_runs import BillingRunService
from finance_core.domains.customers import CustomerService
from finance_core.domains.delivery import (
    DiscordDeliveryTransport,
    DiscordTransport,
    DeliveryService,
    DisabledEmailTransport,
    EmailTransport,
)
from finance_core.domains.integrations import IntegrationService
from finance_core.domains.invoices import InvoiceService
from finance_core.domains.masters import MasterService
from finance_core.domains.payments import PaymentService
from finance_core.domains.revenue import RevenueService
from finance_core.domains.reporting import ReportingService
from finance_core.domains.schedules import ScheduleService
from finance_core.domains.work import WorkService
from finance_core.errors import FinanceNotFoundError
from finance_core.renderer import ChromiumPdfRenderer, PdfRenderer
from finance_core.integrations.google_calendar import GoogleCalendarProvider
from finance_core.store import FinanceStore
from finance_core.templates import TemplateRegistry


class FinanceCore:
    """Independent Finance Core; Hermes talks to it through MCP."""

    def __init__(
        self,
        home: str | Path,
        *,
        store: FinanceStore | None = None,
        renderer: PdfRenderer | None = None,
        templates: TemplateRegistry | None = None,
        email_transport: EmailTransport | None = None,
        discord_transport: DiscordTransport | None = None,
        require_approval_policy: bool = False,
        calendar_provider: Any | None = None,
    ):
        self.home = Path(home)
        self.store = store or FinanceStore(self.home)
        self.documents = DocumentStore(self.home)
        self.renderer = renderer or ChromiumPdfRenderer()
        self.templates = templates or TemplateRegistry()
        self.email_transport = email_transport or DisabledEmailTransport()
        self.discord_transport = discord_transport or DiscordDeliveryTransport()
        self.require_approval_policy = bool(require_approval_policy)
        self.approvals = ApprovalService(self)
        self.delivery_service = DeliveryService(self)
        self.integrations = IntegrationService(self)
        self.invoices = InvoiceService(self)
        self.customers = CustomerService(self)
        self.masters = MasterService(self)
        self.payments = PaymentService(self)
        self.revenue = RevenueService(self)
        self.reporting = ReportingService(self)
        self.work = WorkService(self)
        self.billing_runs = BillingRunService(self)
        self.schedules = ScheduleService(
            self,
            calendar_provider=calendar_provider
            or GoogleCalendarProvider(token_path=self.home.parent / "google_token.json"),
        )

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
        return self.masters.create_issuer(
            legal_name=legal_name,
            qualified_invoice_issuer=qualified_invoice_issuer,
            registration_number=registration_number,
            address=address,
            bank_account=bank_account,
            tax_rounding_policy=tax_rounding_policy,
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
        return self.customers.create_customer(
            customer_kind=customer_kind,
            legal_name=legal_name,
            billing_profile=billing_profile,
            corporate_number=corporate_number,
            external_id=external_id,
            owner_id=owner_id,
            tags=tags,
            lifecycle_status=lifecycle_status,
            contacts=contacts,
        )

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
        return self.customers.search_customers(
            query=query,
            customer_kind=customer_kind,
            active_status=active_status,
            lifecycle_status=lifecycle_status,
            owner_id=owner_id,
            tag=tag,
        )

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
        return self.customers.update_customer(
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
            updated_by=updated_by,
        )

    def archive_customer(self, customer_id: str, *, archived_by: str) -> dict[str, Any]:
        return self.customers.archive_customer(customer_id, archived_by=archived_by)

    def list_customer_revisions(self, customer_id: str) -> list[dict[str, Any]]:
        return self.customers.list_customer_revisions(customer_id)

    def get_customer(
        self,
        customer_id: str,
        *,
        include_sensitive: bool = False,
        accessed_by: str | None = None,
    ) -> dict[str, Any]:
        return self.customers.get_customer(
            customer_id, include_sensitive=include_sensitive, accessed_by=accessed_by
        )

    def update_customer_lifecycle(
        self,
        customer_id: str,
        *,
        status: str,
        reason: str,
        updated_by: str,
    ) -> dict[str, Any]:
        return self.customers.update_lifecycle(
            customer_id, status=status, reason=reason, updated_by=updated_by
        )

    def update_customer_payment_settings(
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
        return self.customers.update_payment_settings(
            customer_id,
            credit_limit=credit_limit,
            credit_currency=credit_currency,
            payment_due_rule=payment_due_rule,
            closing_day=closing_day,
            bank_fee_payer=bank_fee_payer,
            dunning_policy=dunning_policy,
            default_tax_category=default_tax_category,
            updated_by=updated_by,
        )

    def create_customer_contact(self, customer_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.customers.create_contact(customer_id, **kwargs)

    def list_customer_contacts(self, customer_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self.customers.list_contacts(customer_id, **kwargs)

    def update_customer_contact(self, contact_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.customers.update_contact(contact_id, **kwargs)

    def archive_customer_contact(self, contact_id: str, *, archived_by: str) -> dict[str, Any]:
        return self.customers.archive_contact(contact_id, archived_by=archived_by)

    def add_customer_relationship(self, parent_customer_id: str, child_customer_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.customers.add_relationship(parent_customer_id, child_customer_id, **kwargs)

    def list_customer_relationships(self, customer_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self.customers.list_relationships(customer_id, **kwargs)

    def suggest_customer_duplicates(self, customer_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self.customers.suggest_duplicates(customer_id, **kwargs)

    def merge_customers(
        self,
        source_customer_id: str,
        target_customer_id: str,
        *,
        merged_by: str,
        reason: str,
    ) -> dict[str, Any]:
        return self.customers.merge(
            source_customer_id,
            target_customer_id,
            merged_by=merged_by,
            reason=reason,
        )

    def record_customer_activity(self, customer_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.customers.record_activity(customer_id, **kwargs)

    def list_customer_activities(self, customer_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self.customers.list_activities(customer_id, **kwargs)

    def customer_dashboard(self, customer_id: str) -> dict[str, Any]:
        return self.customers.customer_dashboard(customer_id)

    def import_customers_csv(self, **kwargs: Any) -> dict[str, Any]:
        return self.customers.import_csv(**kwargs)

    def create_customer_portal_token(self, customer_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.customers.create_portal_token(customer_id, **kwargs)

    def portal_list_invoices(self, token: str) -> list[dict[str, Any]]:
        return self.customers.portal_list_invoices(token)

    def portal_get_invoice(self, token: str, invoice_id: str) -> dict[str, Any]:
        return self.customers.portal_get_invoice(token, invoice_id)

    def portal_download_invoice(self, token: str, invoice_id: str) -> dict[str, Any]:
        return self.customers.portal_download_invoice(token, invoice_id)

    def revoke_customer_portal_token(self, token_id: str, *, revoked_by: str) -> dict[str, Any]:
        return self.customers.revoke_portal_token(token_id, revoked_by=revoked_by)

    def list_customer_access_events(self, customer_id: str) -> list[dict[str, Any]]:
        self.customers._customer(customer_id)
        return self.store.list_customer_access_events(customer_id)

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
        return self.masters.create_contract(
            customer_id=customer_id,
            name=name,
            effective_from=effective_from,
            effective_to=effective_to,
            currency=currency,
            payment_due_rule=payment_due_rule,
            default_template_id=default_template_id,
            project_id=project_id,
            department_id=department_id,
            created_by=created_by,
        )

    def get_contract(self, contract_id: str) -> dict[str, Any]:
        return self.masters.get_contract(contract_id)

    def list_contracts(self, customer_id: str | None = None) -> list[dict[str, Any]]:
        return self.masters.list_contracts(customer_id)

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
        return self.masters.create_billing_rule(
            contract_id=contract_id,
            rule_type=rule_type,
            description=description,
            unit=unit,
            unit_price=unit_price,
            tax_category=tax_category,
            effective_from=effective_from,
            effective_to=effective_to,
            version=version,
            created_by=created_by,
        )

    def get_billing_rule(self, billing_rule_id: str) -> dict[str, Any]:
        return self.masters.get_billing_rule(billing_rule_id)

    def list_billing_rules(self, contract_id: str | None = None) -> list[dict[str, Any]]:
        return self.masters.list_billing_rules(contract_id)

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
        return self.invoices.draft_from_billing_rule(
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
            created_by=created_by,
            line_source_type=line_source_type,
            line_source_id=line_source_id,
            generated_by=generated_by,
            work_entry_ids=work_entry_ids,
            withholding_tax_rate=withholding_tax_rate,
            withholding_tax_base=withholding_tax_base,
        )

    def record_work_entry(self, **kwargs: Any) -> dict[str, Any]:
        return self.work.record(**kwargs)

    def work_from_schedule(self, schedule_id: str, *, created_by: str) -> dict[str, Any]:
        return self.work.record_from_schedule(schedule_id, created_by=created_by)

    def approve_work_entry(self, entry_id: str, *, approved_by: str) -> dict[str, Any]:
        return self.work.approve(entry_id, approved_by=approved_by)

    def reject_work_entry(self, entry_id: str, *, rejected_by: str, reason: str) -> dict[str, Any]:
        return self.work.reject(entry_id, rejected_by=rejected_by, reason=reason)

    def list_work_entries(self, **filters: Any) -> list[dict[str, Any]]:
        return self.work.list(**filters)

    def import_work_csv(self, **kwargs: Any) -> dict[str, Any]:
        return self.work.import_csv(**kwargs)

    def draft_from_work_entries(self, **kwargs: Any) -> dict[str, Any]:
        return self.work.draft_from_entries(**kwargs)

    def draft_from_schedule_entries(self, **kwargs: Any) -> dict[str, Any]:
        return self.work.draft_from_schedule_entries(**kwargs)

    def preview_billing_run(self, **kwargs: Any) -> dict[str, Any]:
        return self.billing_runs.preview(**kwargs)

    def finalize_billing_run(self, **kwargs: Any) -> dict[str, Any]:
        return self.billing_runs.finalize(**kwargs)

    def get_billing_run(self, run_id: str) -> dict[str, Any]:
        return self.billing_runs.get(run_id)

    def list_billing_runs(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.billing_runs.list(**kwargs)

    def draft_from_billing_run(self, run_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.billing_runs.draft_invoice(run_id, **kwargs)

    def create_calendar_connection(self, **kwargs: Any) -> dict[str, Any]:
        return self.schedules.create_connection(**kwargs)

    def get_calendar_connection(self, connection_id: str) -> dict[str, Any]:
        return self.schedules.get_connection(connection_id)

    def list_calendar_connections(self) -> list[dict[str, Any]]:
        return self.schedules.list_connections()

    def sync_calendar_events(
        self,
        connection_id: str,
        *,
        events: list[dict[str, Any]],
        next_sync_token: str | None = None,
        synced_by: str,
    ) -> dict[str, Any]:
        return self.schedules.sync_events(
            connection_id,
            events=events,
            next_sync_token=next_sync_token,
            synced_by=synced_by,
        )

    def sync_calendar(
        self,
        connection_id: str,
        *,
        synced_by: str,
        time_min: str | None = None,
        time_max: str | None = None,
    ) -> dict[str, Any]:
        return self.schedules.sync_from_provider(
            connection_id,
            synced_by=synced_by,
            time_min=time_min,
            time_max=time_max,
        )

    def create_schedule(self, **kwargs: Any) -> dict[str, Any]:
        return self.schedules.create(**kwargs)

    def get_schedule(self, schedule_id: str) -> dict[str, Any]:
        return self.schedules.get(schedule_id)

    def list_schedules(self, **filters: Any) -> list[dict[str, Any]]:
        return self.schedules.list(**filters)

    def link_schedule(self, schedule_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.schedules.link(schedule_id, **kwargs)

    def complete_schedule(
        self,
        schedule_id: str,
        *,
        actual_starts_at: str,
        actual_ends_at: str,
        completed_by: str,
    ) -> dict[str, Any]:
        return self.schedules.complete(
            schedule_id,
            actual_starts_at=actual_starts_at,
            actual_ends_at=actual_ends_at,
            completed_by=completed_by,
        )

    def cancel_schedule(
        self,
        schedule_id: str,
        *,
        cancelled_by: str,
        reason: str,
    ) -> dict[str, Any]:
        return self.schedules.cancel(schedule_id, cancelled_by=cancelled_by, reason=reason)

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
        return self.invoices.draft_create(
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
            created_by=created_by,
            contract_id=contract_id,
            billing_rule_version_id=billing_rule_version_id,
            converted_from=converted_from,
            correction_of=correction_of,
            correction_reason=correction_reason,
            work_entry_ids=work_entry_ids,
            withholding_tax_rate=withholding_tax_rate,
            withholding_tax_base=withholding_tax_base,
        )

    def bulk_draft_create(
        self,
        requests: list[dict[str, Any]],
        *,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        return self.invoices.bulk_draft_create(requests, created_by=created_by)

    def validate_invoice(self, invoice_id: str) -> dict[str, Any]:
        return self.invoices.validate_invoice(invoice_id)

    def preview_pdf(self, invoice_id: str) -> dict[str, Any]:
        return self.invoices.preview_pdf(invoice_id)
    def approve_invoice(self, invoice_id: str, *, approved_by: str) -> dict[str, Any]:
        return self.invoices.approve_invoice(invoice_id, approved_by=approved_by)

    def issue_invoice(self, invoice_id: str, *, issued_by: str) -> dict[str, Any]:
        return self.invoices.issue_invoice(invoice_id, issued_by=issued_by)

    def convert_document(
        self,
        source_invoice_id: str,
        *,
        target_document_type: str,
        converted_by: str,
        billing_key: str | None = None,
    ) -> dict[str, Any]:
        return self.invoices.convert_document(
            source_invoice_id,
            target_document_type=target_document_type,
            converted_by=converted_by,
            billing_key=billing_key,
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
        return self.invoices.correct_invoice(
            invoice_id,
            lines=lines,
            reason=reason,
            corrected_by=corrected_by,
            correction_date=correction_date,
            due_date=due_date,
            billing_key=billing_key,
        )

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        return self.invoices.get_invoice(invoice_id)

    def list_template_versions(self, template_id: str | None = None) -> list[dict[str, Any]]:
        return self.invoices.list_template_versions(template_id)

    def list_audit_events(self, invoice_id: str) -> list[dict[str, Any]]:
        return self.invoices.list_audit_events(invoice_id)

    def set_approval_policy(self, **kwargs: Any) -> dict[str, Any]:
        return self.approvals.set_policy(**kwargs)

    def list_approval_policies(self, *, actor_id: str | None = None) -> list[dict[str, Any]]:
        return self.approvals.list_policies(actor_id=actor_id)

    def close_period(self, period: str, *, closed_by: str, reason: str) -> dict[str, Any]:
        return self.approvals.close_period(period, closed_by=closed_by, reason=reason)

    def reopen_period(self, period: str, *, reopened_by: str, reason: str) -> dict[str, Any]:
        return self.approvals.reopen_period(period, reopened_by=reopened_by, reason=reason)

    def period_status(self, period: str) -> dict[str, Any]:
        return self.approvals.period_status(period)

    def list_periods(self) -> list[dict[str, Any]]:
        return self.approvals.list_periods()

    def search_invoices(
        self,
        *,
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
        return self.revenue.search_invoices(
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

    def overdue_invoices(self, *, today: str | None = None) -> list[dict[str, Any]]:
        return self.revenue.overdue_invoices(today=today)

    def reminder_candidates(
        self,
        *,
        today: str | None = None,
        reminder_days: int = 0,
    ) -> list[dict[str, Any]]:
        return self.revenue.reminder_candidates(today=today, reminder_days=reminder_days)

    def record_revenue(
        self,
        *,
        customer_id: str,
        recognition_date: str,
        service_period: str,
        amount: str,
        currency: str,
        description: str,
        source_type: str,
        source_id: str,
        invoice_id: str | None = None,
        project_id: str | None = None,
        department_id: str | None = None,
        created_by: str,
    ) -> dict[str, Any]:
        return self.revenue.record_revenue(
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
            created_by=created_by,
        )

    def search_revenue_entries(
        self,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        customer_id: str | None = None,
        project_id: str | None = None,
        department_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.revenue.search_revenue_entries(
            from_date=from_date,
            to_date=to_date,
            customer_id=customer_id,
            project_id=project_id,
            department_id=department_id,
        )

    def revenue_by_project(self, *, from_date: str, to_date: str) -> list[dict[str, Any]]:
        return self.revenue.revenue_by_project(from_date=from_date, to_date=to_date)

    def revenue_summary(self, *, from_date: str, to_date: str) -> dict[str, Any]:
        return self.revenue.revenue_summary(from_date=from_date, to_date=to_date)

    def revenue_by_customer(self, *, from_date: str, to_date: str) -> list[dict[str, Any]]:
        return self.revenue.revenue_by_customer(from_date=from_date, to_date=to_date)

    def revenue_by_month(self, *, from_date: str, to_date: str) -> list[dict[str, Any]]:
        return self.revenue.revenue_by_month(from_date=from_date, to_date=to_date)

    def export_accounting_csv(self, *, from_date: str, to_date: str) -> dict[str, Any]:
        return self.revenue.export_accounting_csv(from_date=from_date, to_date=to_date)

    def dashboard_summary(self, **kwargs: Any) -> dict[str, Any]:
        return self.reporting.dashboard_summary(**kwargs)

    def receivables_aging(self, **kwargs: Any) -> dict[str, dict[str, Any]]:
        return self.reporting.receivables_aging(**kwargs)

    def tax_report(self, **kwargs: Any) -> dict[str, Any]:
        return self.reporting.tax_report(**kwargs)

    def export_integration_bundle(self, **kwargs: Any) -> dict[str, Any]:
        return self.integrations.export_bundle(**kwargs)

    def freee_invoice_preview(self, **kwargs: Any) -> dict[str, Any]:
        return self.integrations.freee_invoice_preview(**kwargs)

    def prepare_delivery(
        self,
        invoice_id: str,
        *,
        method: str,
        prepared_by: str,
        recipient: str | None = None,
    ) -> dict[str, Any]:
        return self.delivery_service.prepare(
            invoice_id,
            method=method,
            prepared_by=prepared_by,
            recipient=recipient,
        )

    def update_delivery(
        self,
        delivery_id: str,
        *,
        status: str,
        updated_by: str,
        external_reference: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return self.delivery_service.update_status(
            delivery_id,
            status=status,
            updated_by=updated_by,
            external_reference=external_reference,
            error=error,
        )

    def list_delivery_outbox(self, invoice_id: str | None = None) -> list[dict[str, Any]]:
        return self.delivery_service.list_outbox(invoice_id)

    def retry_delivery(self, delivery_id: str, *, retried_by: str) -> dict[str, Any]:
        return self.delivery_service.retry(delivery_id, retried_by=retried_by)

    def dispatch_email(self, delivery_id: str, *, sent_by: str) -> dict[str, Any]:
        return self.delivery_service.dispatch_email(delivery_id, sent_by=sent_by)

    def dispatch_discord(self, delivery_id: str, *, sent_by: str) -> dict[str, Any]:
        return self.delivery_service.dispatch_discord(delivery_id, sent_by=sent_by)

    def record_delivery_download(
        self,
        delivery_id: str,
        *,
        downloaded_at: str | None = None,
    ) -> dict[str, Any]:
        return self.delivery_service.record_download(delivery_id, downloaded_at=downloaded_at)

    def list_deliveries(self, invoice_id: str | None = None) -> list[dict[str, Any]]:
        return self.delivery_service.list_deliveries(invoice_id)

    def import_payment_csv(
        self,
        *,
        csv_text: str,
        apply: bool = False,
        imported_by: str | None = None,
    ) -> dict[str, Any]:
        return self.payments.import_payment_csv(
            csv_text=csv_text,
            apply=apply,
            imported_by=imported_by,
        )

    def add_payment_alias(
        self,
        customer_id: str,
        alias: str,
        *,
        added_by: str,
    ) -> dict[str, Any]:
        return self.payments.add_payment_alias(customer_id, alias, added_by=added_by)

    def list_payment_aliases(self, customer_id: str | None = None) -> list[dict[str, Any]]:
        return self.payments.list_payment_aliases(customer_id)

    def match_payment(self, payment_id: str) -> list[dict[str, Any]]:
        return self.payments.match_payment(payment_id)

    def allocate_payment(
        self,
        payment_id: str,
        *,
        allocations: list[dict[str, Any]],
        approved_by: str,
    ) -> dict[str, Any]:
        return self.payments.allocate_payment(
            payment_id,
            allocations=allocations,
            approved_by=approved_by,
        )

    def check_payment(self, invoice_id: str) -> dict[str, Any]:
        return self.payments.check_payment(invoice_id)

    def unallocated_payments(self) -> list[dict[str, Any]]:
        return self.payments.unallocated_payments()

    def _invoice(self, invoice_id: str) -> dict[str, Any]:
        invoice = self.store.get_invoice(invoice_id)
        if invoice is None:
            raise FinanceNotFoundError(f"Invoice not found: {invoice_id}")
        return invoice

    def _issuer(self, issuer_id: str) -> dict[str, Any]:
        issuer = self.store.get_issuer(issuer_id)
        if issuer is None:
            raise FinanceNotFoundError(f"Issuer not found: {issuer_id}")
        return issuer

    def _customer(self, customer_id: str) -> dict[str, Any]:
        customer = self.store.get_customer(customer_id)
        if customer is None:
            raise FinanceNotFoundError(f"Customer not found: {customer_id}")
        return customer

    @staticmethod
    def _now() -> str:
        from finance_core.store import utc_now_iso

        return utc_now_iso()
