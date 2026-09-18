"""Priority customer, billing-rule, document, and sales-ledger behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.finance_core.test_core import FakePdfRenderer


def _core(tmp_path: Path):
    from finance_core.core import FinanceCore

    return FinanceCore(tmp_path, renderer=FakePdfRenderer())


def _masters(core):
    issuer = core.create_issuer(
        legal_name="Priority Issuer",
        qualified_invoice_issuer=True,
        registration_number="T1234567890123",
    )
    customer = core.create_customer(
        customer_kind="company",
        legal_name="Priority Customer",
        billing_profile={
            "billing_name": "Priority Customer",
            "billing_address": "Tokyo",
            "email": "billing@example.test",
        },
    )
    return issuer, customer


def _line(*, description: str = "Service", unit_price: str = "10000", period: str = "2026-08"):
    return {
        "description": description,
        "quantity": "1",
        "unit": "month",
        "unit_price": unit_price,
        "tax_category": "STANDARD_10",
        "tax_rate": "10",
        "service_period": period,
        "generated_by": "manual",
    }


def _issue(core, issuer_id: str, customer_id: str, *, period: str = "2026-08"):
    draft = core.draft_create(
        issuer_id=issuer_id,
        customer_id=customer_id,
        template_id="invoice-standard-jp",
        template_version="1.0.0",
        issue_date=f"{period}-01",
        service_period=period,
        due_date=f"{period}-31" if period.endswith("08") else f"{period}-28",
        currency="JPY",
        lines=[_line(period=period)],
        billing_key=f"priority:{period}",
    )
    core.approve_invoice(draft["id"], approved_by="owner-1")
    return core.issue_invoice(draft["id"], issued_by="owner-1")


def test_customer_search_update_and_archive_preserve_issued_snapshot(tmp_path: Path):
    core = _core(tmp_path)
    issuer, customer = _masters(core)
    issued = _issue(core, issuer["id"], customer["id"])

    updated = core.update_customer(
        customer["id"],
        billing_profile={"billing_address": "Osaka", "email": "new@example.test"},
        updated_by="owner-1",
    )
    matches = core.search_customers(query="priority", active_status="ACTIVE")
    archived = core.archive_customer(customer["id"], archived_by="owner-1")

    assert updated["revision"] == 1
    assert updated["billing_profile"]["billing_address"] == "Osaka"
    assert matches[0]["id"] == customer["id"]
    assert "billing_profile" not in matches[0]
    assert archived["active_status"] == "INACTIVE"
    assert core.get_invoice(issued["id"])["master_snapshot"]["customer"]["billing_profile"]["billing_address"] == "Tokyo"
    assert core.search_customers(query="priority", active_status="ACTIVE") == []
    revisions = core.list_customer_revisions(customer["id"])
    assert [revision["revision"] for revision in revisions] == [1, 2]
    assert revisions[-1]["data"]["active_status"] == "INACTIVE"


def test_contract_billing_rule_generates_idempotent_deterministic_draft(tmp_path: Path):
    core = _core(tmp_path)
    issuer, customer = _masters(core)
    contract = core.create_contract(
        customer_id=customer["id"],
        name="Monthly support",
        effective_from="2026-01-01",
        currency="JPY",
        default_template_id="invoice-standard-jp",
        payment_due_rule={"days_after_issue": 30},
        project_id="project-support",
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

    first = core.draft_from_billing_rule(
        issuer_id=issuer["id"],
        billing_rule_id=rule["id"],
        service_period="2026-08",
        issue_date="2026-08-01",
        quantity="12",
        billing_key="monthly-support:2026-08",
        created_by="owner-1",
    )
    second = core.draft_from_billing_rule(
        issuer_id=issuer["id"],
        billing_rule_id=rule["id"],
        service_period="2026-08",
        issue_date="2026-08-01",
        quantity="12",
        billing_key="monthly-support:2026-08",
        created_by="owner-1",
    )

    assert first["id"] == second["id"]
    assert first["customer_id"] == customer["id"]
    assert first["totals"]["subtotal"] == "66000"
    assert first["lines"][0]["source_type"] == "billing_rule"
    assert first["lines"][0]["source_id"] == rule["id"]
    assert first["lines"][0]["billing_rule_version_id"] == rule["id"]
    assert "12" in first["lines"][0]["calculation_expression"]
    assert first["due_date"] == "2026-08-31"


def test_document_conversion_and_correction_never_overwrite_original(tmp_path: Path):
    core = _core(tmp_path)
    issuer, customer = _masters(core)
    quote = core.draft_create(
        issuer_id=issuer["id"],
        customer_id=customer["id"],
        template_id="invoice-standard-jp",
        template_version="1.0.0",
        issue_date="2026-08-01",
        service_period="2026-08",
        due_date="2026-08-31",
        currency="JPY",
        lines=[_line(description="Quote")],
        document_type="quote",
        billing_key="quote:1",
    )
    invoice = core.convert_document(
        quote["id"], target_document_type="invoice", converted_by="owner-1", billing_key="invoice:1"
    )
    core.approve_invoice(invoice["id"], approved_by="owner-1")
    issued = core.issue_invoice(invoice["id"], issued_by="owner-1")

    correction = core.correct_invoice(
        issued["id"],
        lines=[_line(description="Corrected", unit_price="12000")],
        reason="Unit price correction",
        corrected_by="owner-1",
    )

    assert invoice["document_type"] == "invoice"
    assert invoice["converted_from"]["id"] == quote["id"]
    assert issued["document_status"] == "ISSUED"
    assert core.get_invoice(issued["id"])["totals"]["subtotal"] == "10000"
    assert correction["document_status"] == "DRAFT"
    assert correction["correction_of"] == issued["id"]
    assert correction["correction_reason"] == "Unit price correction"
    assert correction["totals"]["subtotal"] == "12000"


def test_sales_subledger_separates_recognition_from_invoice_and_is_pii_free(tmp_path: Path):
    core = _core(tmp_path)
    issuer, customer = _masters(core)
    issued = _issue(core, issuer["id"], customer["id"], period="2026-08")

    entry = core.record_revenue(
        customer_id=customer["id"],
        recognition_date="2026-07-31",
        service_period="2026-07",
        amount="10000",
        currency="JPY",
        description="July delivery",
        source_type="service_period",
        source_id="work:2026-07",
        invoice_id=issued["id"],
        project_id="project-support",
        created_by="owner-1",
    )
    summary = core.revenue_summary(from_date="2026-07-01", to_date="2026-07-31")
    by_project = core.revenue_by_project(from_date="2026-07-01", to_date="2026-07-31")
    found = core.search_revenue_entries(customer_id=customer["id"])

    assert entry["id"].startswith("rev_")
    assert summary["invoice_count"] == 0
    assert summary["recognized_revenue_total"] == "10000"
    assert by_project == [{"project_id": "project-support", "recognized_revenue_total": "10000"}]
    assert found[0]["id"] == entry["id"]
    assert "billing@example.test" not in str(found)


def test_new_mcp_tools_are_exposed(tmp_path: Path):
    pytest.importorskip("mcp.server.fastmcp")
    from finance_core.mcp_server import create_server

    names = set(create_server(home=tmp_path)._tool_manager._tools)

    assert {
        "finance.customer.search",
        "finance.customer.update",
        "finance.customer.archive",
        "finance.customer.revisions",
        "finance.contract.create",
        "finance.contract.get",
        "finance.contract.list",
        "finance.billing_rule.create",
        "finance.billing_rule.get",
        "finance.billing_rule.list",
        "finance.invoice.draft_from_rule",
        "finance.invoice.convert",
        "finance.invoice.correct",
        "finance.sales.record",
        "finance.sales.search",
        "finance.revenue.by_project",
    } <= names


def test_new_mcp_customer_writes_bind_actor_and_redact_revision_data(
    tmp_path: Path, monkeypatch
):
    pytest.importorskip("mcp.server.fastmcp")
    from finance_core.errors import FinanceAuthorizationError
    from finance_core.mcp_server import create_server

    server = create_server(home=tmp_path)
    create = server._tool_manager._tools["finance.customer.create"].fn
    update = server._tool_manager._tools["finance.customer.update"].fn
    revisions = server._tool_manager._tools["finance.customer.revisions"].fn
    customer = create(
        "company",
        "Private Customer",
        {"billing_name": "Private Customer", "billing_address": "Tokyo"},
    )
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "owner-1")

    updated = update(
        customer["id"],
        "owner-1",
        billing_profile={"billing_address": "Osaka"},
    )

    assert updated["id"] == customer["id"]
    revision_rows = revisions(customer["id"])
    assert revision_rows[0]["actor"] == "owner-1"
    assert "data" not in revision_rows[0]
    with pytest.raises(FinanceAuthorizationError):
        update(customer["id"], "spoofed", billing_profile={"billing_address": "Kyoto"})
