"""Architecture seams and next Finance Core feature behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.finance_core.test_core import FakePdfRenderer, _line


def _core(tmp_path: Path):
    from finance_core.core import FinanceCore

    return FinanceCore(tmp_path, renderer=FakePdfRenderer())


def _masters(core):
    issuer = core.create_issuer(
        legal_name="Feature Issuer",
        qualified_invoice_issuer=True,
        registration_number="T1234567890123",
        bank_account={"bank_name": "Example Bank", "account_number": "1234567"},
    )
    customer = core.create_customer(
        customer_kind="company",
        legal_name="Feature Customer",
        billing_profile={
            "billing_name": "Feature Customer",
            "billing_address": "Tokyo",
            "email": "customer@example.test",
        },
    )
    return issuer, customer


def _draft(core, issuer_id: str, customer_id: str, *, created_by: str = "creator-1"):
    return core.draft_create(
        issuer_id=issuer_id,
        customer_id=customer_id,
        template_id="invoice-standard-jp",
        template_version="1.0.0",
        issue_date="2026-08-01",
        service_period="2026-08",
        due_date="2026-08-31",
        currency="JPY",
        lines=[_line(unit_price="10000")],
        billing_key=f"feature:{created_by}",
        created_by=created_by,
    )


def test_finance_core_exposes_domain_services(tmp_path: Path):
    from finance_core.domains.approvals import ApprovalService
    from finance_core.domains.delivery import DeliveryService
    from finance_core.domains.integrations import IntegrationService
    from finance_core.domains.invoices import InvoiceService
    from finance_core.domains.masters import MasterService
    from finance_core.domains.payments import PaymentService
    from finance_core.domains.reporting import ReportingService
    from finance_core.domains.revenue import RevenueService
    from finance_core.domains.customers import CustomerService
    from finance_core.domains.schedules import ScheduleService
    from finance_core.domains.work import WorkService

    core = _core(tmp_path)

    assert isinstance(core.approvals, ApprovalService)
    assert isinstance(core.delivery_service, DeliveryService)
    assert callable(core.delivery_service.prepare)
    assert callable(core.delivery_service.update_status)
    assert callable(core.delivery_service.record_download)
    assert isinstance(core.integrations, IntegrationService)
    assert isinstance(core.invoices, InvoiceService)
    assert isinstance(core.customers, CustomerService)
    assert isinstance(core.masters, MasterService)
    assert isinstance(core.payments, PaymentService)
    assert isinstance(core.reporting, ReportingService)
    assert isinstance(core.revenue, RevenueService)
    assert isinstance(core.schedules, ScheduleService)
    assert isinstance(core.work, WorkService)


def test_customer_profile_accepts_common_natural_language_aliases(tmp_path: Path):
    core = _core(tmp_path)

    customer = core.create_customer(
        customer_kind="company",
        legal_name="Natural Language Customer",
        billing_profile={
            "legal_name": "Natural Language Customer",
            "address": "Tokyo",
            "postal_code": "100-0001",
            "fax": "03-0000-0000",
        },
    )

    profile = customer["billing_profile"]
    assert profile["billing_name"] == "Natural Language Customer"
    assert profile["billing_address"] == "Tokyo"
    assert profile["postal_code"] == "100-0001"
    assert profile["fax"] == "03-0000-0000"


def test_approval_policy_enforces_action_amount_and_separation(tmp_path: Path):
    from finance_core.errors import FinanceAuthorizationError

    core = _core(tmp_path)
    issuer, customer = _masters(core)
    draft = _draft(core, issuer["id"], customer["id"])

    core.set_approval_policy(
        actor_id="approver-1",
        role="approver",
        can_approve=True,
        can_issue=False,
        can_send=False,
        max_amount="20000",
        separation_required=True,
        configured_by="admin-1",
    )
    core.set_approval_policy(
        actor_id="issuer-1",
        role="issuer",
        can_approve=False,
        can_issue=True,
        can_send=True,
        max_amount="20000",
        separation_required=True,
        configured_by="admin-1",
    )

    approved = core.approve_invoice(draft["id"], approved_by="approver-1")
    assert approved["approved_by"] == "approver-1"
    with pytest.raises(FinanceAuthorizationError):
        core.issue_invoice(draft["id"], issued_by="approver-1")
    issued = core.issue_invoice(draft["id"], issued_by="issuer-1")
    assert issued["document_status"] == "ISSUED"


def test_live_finance_core_requires_an_active_approval_policy(tmp_path: Path):
    from finance_core.errors import FinanceAuthorizationError
    from finance_core.core import FinanceCore

    core = FinanceCore(tmp_path, renderer=FakePdfRenderer(), require_approval_policy=True)
    issuer, customer = _masters(core)
    draft = _draft(core, issuer["id"], customer["id"])

    with pytest.raises(FinanceAuthorizationError, match="active approval policy"):
        core.approve_invoice(draft["id"], approved_by="owner-1")


def test_period_lock_blocks_new_financial_events_and_can_be_reopened(tmp_path: Path):
    from finance_core.errors import FinanceConflictError

    core = _core(tmp_path)
    issuer, customer = _masters(core)
    core.close_period("2026-08", closed_by="controller-1", reason="Month end")

    with pytest.raises(FinanceConflictError, match="closed"):
        _draft(core, issuer["id"], customer["id"], created_by="period-test")

    assert core.period_status("2026-08")["status"] == "CLOSED"
    core.reopen_period("2026-08", reopened_by="controller-1", reason="Correction")
    draft = _draft(core, issuer["id"], customer["id"], created_by="period-test")
    assert draft["document_status"] == "DRAFT"


def test_delivery_outbox_tracks_attempts_and_retry_without_sending_email(tmp_path: Path):
    core = _core(tmp_path)
    issuer, customer = _masters(core)
    draft = _draft(core, issuer["id"], customer["id"], created_by="creator-delivery")
    core.approve_invoice(draft["id"], approved_by="owner-1")
    issued = core.issue_invoice(draft["id"], issued_by="owner-1")

    queued = core.prepare_delivery(issued["id"], method="email", prepared_by="sender-1")
    assert core.list_delivery_outbox(issued["id"])[0]["status"] == "QUEUED"
    core.update_delivery(queued["id"], status="SENDING", updated_by="sender-1")
    failed = core.update_delivery(
        queued["id"],
        status="FAILED",
        updated_by="sender-1",
        error="temporary SMTP failure",
    )
    assert failed["attempt_count"] == 1
    retried = core.retry_delivery(queued["id"], retried_by="sender-1")
    assert retried["status"] == "QUEUED"
    assert retried["attempt_count"] == 1
    assert retried["last_error"] == "temporary SMTP failure"


def test_payment_alias_dictionary_and_due_date_are_used_for_match_suggestions(tmp_path: Path):
    core = _core(tmp_path)
    issuer, customer = _masters(core)
    draft = core.draft_create(
        issuer_id=issuer["id"],
        customer_id=customer["id"],
        template_id="invoice-standard-jp",
        template_version="1.0.0",
        issue_date="2026-08-01",
        service_period="2026-08",
        due_date="2026-08-20",
        currency="JPY",
        lines=[_line(unit_price="10000")],
        billing_key="payment-alias",
    )
    core.approve_invoice(draft["id"], approved_by="owner-1")
    invoice = core.issue_invoice(draft["id"], issued_by="owner-1")
    core.add_payment_alias(customer["id"], "FEATURE WIRE NAME", added_by="owner-1")
    imported = core.import_payment_csv(
        csv_text=(
            "received_date,value_date,amount,currency,bank_transaction_id,payer_name_raw,remittance_information\n"
            f"2026-08-21,2026-08-21,11000,JPY,BANK-ALIAS,Feature Wire Name,unknown\n"
        ),
        apply=True,
        imported_by="owner-1",
    )

    matches = core.match_payment(imported["payments"][0]["id"])

    assert matches[0]["invoice_id"] == invoice["id"]
    assert matches[0]["matched_reason"] == "payer_alias_and_amount_match"
    assert matches[0]["confidence"] == "medium"
    assert matches[0]["days_from_due_date"] == 1
    assert "Feature Wire Name" not in str(matches)


def test_work_entries_generate_idempotent_invoice_draft_and_are_marked_billed(tmp_path: Path):
    from finance_core.errors import FinanceConflictError

    core = _core(tmp_path)
    issuer, customer = _masters(core)
    contract = core.create_contract(
        customer_id=customer["id"],
        name="Support contract",
        effective_from="2026-01-01",
        default_template_id="invoice-standard-jp",
        payment_due_rule={"days_after_issue": 30},
        created_by="owner-1",
    )
    rule = core.create_billing_rule(
        contract_id=contract["id"],
        rule_type="HOURLY",
        description="Support hours",
        unit="hour",
        unit_price="5500",
        tax_category="STANDARD_10",
        effective_from="2026-01-01",
        created_by="owner-1",
    )
    first = core.record_work_entry(
        customer_id=customer["id"],
        contract_id=contract["id"],
        billing_rule_id=rule["id"],
        service_date="2026-08-05",
        quantity="2",
        source_id="timesheet-001",
        created_by="owner-1",
    )
    second = core.record_work_entry(
        customer_id=customer["id"],
        contract_id=contract["id"],
        billing_rule_id=rule["id"],
        service_date="2026-08-06",
        quantity="3",
        source_id="timesheet-002",
        created_by="owner-1",
    )
    with pytest.raises(FinanceConflictError):
        core.record_work_entry(
            customer_id=customer["id"],
            contract_id=contract["id"],
            billing_rule_id=rule["id"],
            service_date="2026-08-06",
            quantity="1",
            source_id="timesheet-002",
            created_by="owner-1",
        )

    draft = core.draft_from_work_entries(
        issuer_id=issuer["id"],
        billing_rule_id=rule["id"],
        service_period="2026-08",
        issue_date="2026-08-31",
        billing_key="work:2026-08",
        created_by="owner-1",
    )
    repeated = core.draft_from_work_entries(
        issuer_id=issuer["id"],
        billing_rule_id=rule["id"],
        service_period="2026-08",
        issue_date="2026-08-31",
        billing_key="work:2026-08",
        created_by="owner-1",
    )

    assert draft["id"] == repeated["id"]
    assert draft["lines"][0]["quantity"] == "5"
    assert draft["lines"][0]["source_type"] == "work_entry_batch"
    assert {row["status"] for row in core.list_work_entries(unbilled_only=True)} == set()
    assert {row["id"] for row in core.list_work_entries()} == {first["id"], second["id"]}


def test_work_timesheet_csv_supports_preview_then_explicit_apply(tmp_path: Path):
    core = _core(tmp_path)
    _issuer, customer = _masters(core)
    contract = core.create_contract(
        customer_id=customer["id"],
        name="CSV contract",
        effective_from="2026-01-01",
        created_by="owner-1",
    )
    rule = core.create_billing_rule(
        contract_id=contract["id"],
        rule_type="PER_EVENT",
        description="Visits",
        unit="visit",
        unit_price="3000",
        tax_category="STANDARD_10",
        effective_from="2026-01-01",
        created_by="owner-1",
    )
    csv_text = (
        "source_id,customer_id,contract_id,billing_rule_id,service_date,quantity,project_id\n"
        f"csv-001,{customer['id']},{contract['id']},{rule['id']},2026-08-10,2,project-csv\n"
    )

    preview = core.import_work_csv(csv_text=csv_text)
    applied = core.import_work_csv(csv_text=csv_text, apply=True, imported_by="owner-1")

    assert preview["success"] is True
    assert preview["dry_run"] is True
    assert preview["rows"][0]["source_id"] == "csv-001"
    assert applied["success"] is True
    assert applied["applied_count"] == 1


def test_dashboard_separates_receivables_cash_and_revenue_with_aging(tmp_path: Path):
    core = _core(tmp_path)
    issuer, customer = _masters(core)
    draft = _draft(core, issuer["id"], customer["id"], created_by="dashboard-creator")
    core.approve_invoice(draft["id"], approved_by="owner-1")
    invoice = core.issue_invoice(draft["id"], issued_by="owner-1")
    core.record_revenue(
        customer_id=customer["id"],
        recognition_date="2026-08-31",
        service_period="2026-08",
        amount="10000",
        currency="JPY",
        description="August service",
        source_type="service_period",
        source_id="dashboard-service-2026-08",
        invoice_id=invoice["id"],
        project_id="project-dashboard",
        created_by="owner-1",
    )
    payment = core.import_payment_csv(
        csv_text=(
            "received_date,value_date,amount,currency,bank_transaction_id,payer_name_raw,remittance_information\n"
            f"2026-09-05,2026-09-05,5500,JPY,BANK-DASH,Feature Customer,{invoice['invoice_number']}\n"
        ),
        apply=True,
        imported_by="owner-1",
    )
    core.allocate_payment(
        payment["payments"][0]["id"],
        allocations=[{"invoice_id": invoice["id"], "amount": "5500"}],
        approved_by="owner-1",
    )

    dashboard = core.dashboard_summary(
        from_date="2026-08-01",
        to_date="2026-09-30",
        today="2026-09-10",
    )
    aging = core.receivables_aging(today="2026-09-10")

    assert dashboard["invoiced_total"] == "11000"
    assert dashboard["received_total"] == "5500"
    assert dashboard["outstanding_total"] == "5500"
    assert dashboard["recognized_revenue_total"] == "10000"
    assert dashboard["overdue_total"] == "5500"
    assert aging["1_30"]["invoice_count"] == 1
    assert aging["1_30"]["outstanding_total"] == "5500"
    assert "Feature Customer" not in str(dashboard)


def test_accounting_integration_bundle_exports_freee_safe_csv_boundaries(tmp_path: Path):
    core = _core(tmp_path)
    issuer, customer = _masters(core)
    draft = _draft(core, issuer["id"], customer["id"], created_by="integration-creator")
    core.approve_invoice(draft["id"], approved_by="owner-1")
    core.issue_invoice(draft["id"], issued_by="owner-1")

    bundle = core.export_integration_bundle(
        from_date="2026-08-01",
        to_date="2026-08-31",
        target="freee",
    )

    assert bundle["target"] == "freee"
    assert bundle["format_version"] == "freee-journal-v1"
    assert "debit_account" in bundle["invoices_csv"].splitlines()[0]
    assert "Feature Customer" not in str(bundle)
    with pytest.raises(ValueError):
        core.export_integration_bundle(
            from_date="2026-08-01",
            to_date="2026-08-31",
            target="unknown-accounting-software",
        )


def test_freee_invoice_preview_maps_issued_invoice_without_customer_pii(tmp_path: Path):
    core = _core(tmp_path)
    issuer, customer = _masters(core)
    draft = _draft(core, issuer["id"], customer["id"], created_by="freee-preview")
    core.approve_invoice(draft["id"], approved_by="owner-1")
    issued = core.issue_invoice(draft["id"], issued_by="owner-1")

    preview = core.freee_invoice_preview(
        invoice_id=issued["id"],
        company_id=1001,
        partner_id=2002,
    )

    assert preview["target"] == "freee-invoice"
    assert preview["endpoint"] == "https://api.freee.co.jp/iv/invoices"
    assert preview["request"]["company_id"] == 1001
    assert preview["request"]["partner_id"] == 2002
    assert preview["request"]["lines"][0]["tax_rate"] == 10
    assert "Feature Customer" not in str(preview)


def test_explicit_withholding_tax_changes_collectible_total_and_tax_report(tmp_path: Path):
    core = _core(tmp_path)
    issuer, customer = _masters(core)
    draft = core.draft_create(
        issuer_id=issuer["id"],
        customer_id=customer["id"],
        template_id="invoice-standard-jp",
        template_version="1.0.0",
        issue_date="2026-08-01",
        service_period="2026-08",
        due_date="2026-08-31",
        currency="JPY",
        lines=[_line(unit_price="10000")],
        billing_key="withholding-1",
        withholding_tax_rate="10.21",
        withholding_tax_base="subtotal",
    )
    assert draft["totals"]["subtotal"] == "10000"
    assert draft["totals"]["tax"] == "1000"
    assert draft["totals"]["total"] == "11000"
    assert draft["totals"]["withholding_tax"] == "1021"
    assert draft["totals"]["collectible_total"] == "9979"
    core.approve_invoice(draft["id"], approved_by="owner-1")
    issued = core.issue_invoice(draft["id"], issued_by="owner-1")

    check = core.check_payment(issued["id"])
    report = core.tax_report(from_date="2026-08-01", to_date="2026-08-31")

    assert check["invoice_total"] == "11000"
    assert check["collectible_total"] == "9979"
    assert check["outstanding"] == "9979"
    assert report["withholding_tax_total"] == "1021"
    assert report["tax_by_category"]["STANDARD_10"]["tax_amount"] == "1000"


def test_next_priority_mcp_tools_are_exposed(tmp_path: Path):
    pytest.importorskip("mcp.server.fastmcp")
    from finance_core.mcp_server import create_server

    names = set(create_server(home=tmp_path)._tool_manager._tools)

    assert {
        "finance.approval.bootstrap_self",
        "finance.approval.policy_set",
        "finance.approval.policy_list",
        "finance.period.close",
        "finance.period.reopen",
        "finance.period.status",
        "finance.period.list",
        "finance.delivery.outbox",
        "finance.delivery.retry",
        "finance.delivery.dispatch_email",
        "finance.delivery.dispatch_discord",
        "finance.payment.alias_add",
        "finance.payment.alias_list",
        "finance.work.record",
        "finance.work.from_schedule",
        "finance.work.approve",
        "finance.work.reject",
        "finance.work.list",
        "finance.work.import_csv",
        "finance.work.draft_from_entries",
        "finance.invoice.draft_from_schedule",
        "finance.calendar.connection_create",
        "finance.calendar.connection_list",
        "finance.calendar.connection_get",
        "finance.calendar.sync",
        "finance.schedule.create",
        "finance.schedule.list",
        "finance.schedule.get",
        "finance.schedule.link",
        "finance.schedule.complete",
        "finance.schedule.cancel",
        "finance.billing_run.preview",
        "finance.billing_run.finalize",
        "finance.billing_run.get",
        "finance.billing_run.list",
        "finance.invoice.draft_from_billing_run",
        "finance.dashboard.summary",
        "finance.dashboard.receivables_aging",
        "finance.tax.report",
        "finance.accounting.export_bundle",
        "finance.accounting.freee_invoice_preview",
    } <= names


def test_bootstrap_approval_policy_binds_current_session(tmp_path: Path, monkeypatch):
    pytest.importorskip("mcp.server.fastmcp")
    from finance_core.mcp_server import create_server

    server = create_server(home=tmp_path)
    bootstrap = server._tool_manager._tools["finance.approval.bootstrap_self"].fn
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "controller-1")

    policy = bootstrap()

    assert policy["actor_id"] == "controller-1"
    assert policy["role"] == "admin"
    assert policy["can_approve"] is True
    assert policy["can_issue"] is True
    assert policy["can_send"] is True


def test_new_mcp_policy_write_binds_configured_actor(tmp_path: Path, monkeypatch):
    pytest.importorskip("mcp.server.fastmcp")
    from finance_core.errors import FinanceAuthorizationError
    from finance_core.mcp_server import create_server

    server = create_server(home=tmp_path)
    policy_set = server._tool_manager._tools["finance.approval.policy_set"].fn
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "controller-1")

    configured = policy_set(
        "controller-1",
        "admin",
        "controller-1",
        can_approve=True,
        can_issue=True,
        can_send=True,
    )
    assert configured["actor_id"] == "controller-1"
    with pytest.raises(FinanceAuthorizationError):
        policy_set("approver-2", "approver", "spoofed-controller", can_approve=True)

    delegated = policy_set(
        "approver-1",
        "approver",
        "controller-1",
        can_approve=True,
    )
    assert delegated["actor_id"] == "approver-1"


def test_calendar_mcp_write_binds_authenticated_actor_and_named_target(tmp_path: Path, monkeypatch):
    pytest.importorskip("mcp.server.fastmcp")
    from finance_core.errors import FinanceAuthorizationError
    from finance_core.mcp_server import create_server

    server = create_server(home=tmp_path)
    connection_create = server._tool_manager._tools["finance.calendar.connection_create"].fn
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "owner-1")

    connection = connection_create(
        "google_calendar",
        "Asia/Tokyo",
        "owner-1",
        "visit-calendar",
    )

    assert connection["calendar_name"] == "訪問薬剤管理"
    with pytest.raises(FinanceAuthorizationError):
        connection_create("google_calendar", "Asia/Tokyo", "spoofed-owner", "other-calendar")


def test_work_import_mcp_write_binds_authenticated_importer(tmp_path: Path, monkeypatch):
    pytest.importorskip("mcp.server.fastmcp")
    from finance_core.mcp_server import create_server

    server = create_server(home=tmp_path)
    work_import = server._tool_manager._tools["finance.work.import_csv"].fn
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "owner-1")

    result = work_import("source_id\nwork-1\n", True, "spoofed-owner")

    assert result["success"] is False
    assert "authenticated session" in result["errors"][0]["error"] or "does not match" in result["errors"][0]["error"]


def test_email_dispatch_mcp_write_binds_authenticated_sender(tmp_path: Path, monkeypatch):
    pytest.importorskip("mcp.server.fastmcp")
    from finance_core.errors import FinanceAuthorizationError
    from finance_core.mcp_server import create_server

    server = create_server(home=tmp_path)
    dispatch = server._tool_manager._tools["finance.delivery.dispatch_email"].fn
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "owner-1")

    with pytest.raises(FinanceAuthorizationError, match="does not match"):
        dispatch("delivery-1", "spoofed-owner")
