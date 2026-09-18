"""Customer contacts, lifecycle, matching, portal, and privacy behavior."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from tests.finance_core.test_core import FakePdfRenderer, _line


def _core(tmp_path: Path):
    from finance_core.core import FinanceCore

    return FinanceCore(tmp_path, renderer=FakePdfRenderer())


def _customer(core, name: str, *, corporate_number: str, external_id: str):
    return core.create_customer(
        customer_kind="company",
        legal_name=name,
        corporate_number=corporate_number,
        external_id=external_id,
        owner_id="owner-1",
        tags=["priority", "tokyo"],
        billing_profile={
            "billing_name": name,
            "billing_address": "Tokyo",
            "email": f"{external_id}@example.test",
        },
    )


def test_customer_contacts_and_sensitive_access_are_separated_and_audited(tmp_path: Path):
    core = _core(tmp_path)
    customer = _customer(
        core,
        "Contact Company",
        corporate_number="1234567890123",
        external_id="contact-1",
    )

    contact = core.create_customer_contact(
        customer["id"],
        name="Accounting Person",
        email="accounting@example.test",
        role="accounting",
        added_by="owner-1",
        is_primary=True,
    )
    safe = core.list_customer_contacts(customer["id"])
    sensitive = core.list_customer_contacts(
        customer["id"], include_sensitive=True, accessed_by="owner-1"
    )

    assert safe[0]["id"] == contact["id"]
    assert "accounting@example.test" not in json.dumps(safe)
    assert sensitive[0]["email"] == "accounting@example.test"
    assert core.get_customer(customer["id"])["id"] == customer["id"]
    detailed = core.get_customer(
        customer["id"], include_sensitive=True, accessed_by="owner-1"
    )
    assert detailed["billing_profile"]["email"] == "contact-1@example.test"
    assert core.list_customer_access_events(customer["id"])[-1]["actor"] == "owner-1"


def test_customer_lifecycle_payment_settings_relationships_and_dashboard(tmp_path: Path):
    core = _core(tmp_path)
    parent = _customer(
        core,
        "Parent Company",
        corporate_number="1234567890123",
        external_id="parent-1",
    )
    child = _customer(
        core,
        "Child Company",
        corporate_number="9876543210987",
        external_id="child-1",
    )

    relationship = core.add_customer_relationship(
        parent["id"], child["id"], relationship_type="SUBSIDIARY", added_by="owner-1"
    )
    updated = core.update_customer_payment_settings(
        parent["id"],
        credit_limit="500000",
        credit_currency="JPY",
        dunning_policy={"reminder_days": [7, 3], "channel": "email"},
        updated_by="owner-1",
    )
    lifecycle = core.update_customer_lifecycle(
        parent["id"], status="SUSPENDED", reason="credit review", updated_by="owner-1"
    )

    assert relationship["relationship_type"] == "SUBSIDIARY"
    assert core.list_customer_relationships(parent["id"])[0]["child_customer_id"] == child["id"]
    assert updated["billing_profile"]["credit_limit"] == "500000"
    assert lifecycle["lifecycle_status"] == "SUSPENDED"
    assert lifecycle["active_status"] == "INACTIVE"
    dashboard = core.customer_dashboard(parent["id"])
    assert dashboard["customer_id"] == parent["id"]
    assert dashboard["relationship_count"] == 1


def test_duplicate_suggestions_and_atomic_customer_merge_preserve_history(tmp_path: Path):
    core = _core(tmp_path)
    target = _customer(
        core,
        "Merge Company",
        corporate_number="1111111111111",
        external_id="merge-target",
    )
    source = _customer(
        core,
        "Merge Company Ltd.",
        corporate_number="1111111111111",
        external_id="merge-source",
    )
    contract = core.create_contract(
        customer_id=source["id"],
        name="Source contract",
        effective_from="2026-01-01",
        created_by="owner-1",
    )

    suggestions = core.suggest_customer_duplicates(source["id"])
    merged = core.merge_customers(
        source["id"], target["id"], merged_by="owner-1", reason="same corporate number"
    )

    assert suggestions[0]["customer_id"] == target["id"]
    assert "corporate_number" in suggestions[0]["reasons"]
    assert merged["target_customer_id"] == target["id"]
    assert core.get_customer(source["id"])["lifecycle_status"] == "CLOSED"
    assert core.list_contracts(target["id"])[0]["id"] == contract["id"]
    assert core.get_customer(target["id"])["merged_customer_ids"] == [source["id"]]


def test_customer_csv_preview_upsert_activity_and_portal_download(tmp_path: Path):
    core = _core(tmp_path)
    issuer = core.create_issuer(legal_name="Portal Issuer")
    csv_text = (
        "external_id,customer_kind,legal_name,billing_address,email,corporate_number,tags\n"
        "csv-1,company,CSV Company,Tokyo,csv@example.test,2222222222222,imported|priority\n"
    )

    preview = core.import_customers_csv(csv_text=csv_text)
    applied = core.import_customers_csv(
        csv_text=csv_text, apply=True, imported_by="owner-1"
    )
    customer_id = applied["customers"][0]["customer_id"]
    core.record_customer_activity(
        customer_id,
        activity_type="note",
        summary="Monthly review completed",
        recorded_by="owner-1",
    )
    draft = core.draft_create(
        issuer_id=issuer["id"],
        customer_id=customer_id,
        template_id="invoice-standard-jp",
        template_version="1.0.0",
        issue_date="2026-08-01",
        service_period="2026-08",
        due_date="2026-08-31",
        currency="JPY",
        lines=[_line(unit_price="10000")],
        billing_key="portal-customer-1",
        created_by="owner-1",
    )
    core.approve_invoice(draft["id"], approved_by="owner-1")
    issued = core.issue_invoice(draft["id"], issued_by="owner-1")
    token = core.create_customer_portal_token(
        customer_id, expires_at="2030-01-01T00:00:00+00:00", created_by="owner-1"
    )
    portal_rows = core.portal_list_invoices(token["token"])
    document = core.portal_download_invoice(token["token"], issued["id"])

    assert preview["rows"][0]["status"] == "ready"
    assert "csv@example.test" not in json.dumps(preview)
    assert applied["applied_count"] == 1
    assert core.list_customer_activities(customer_id)[0]["summary"] == "Monthly review completed"
    assert portal_rows[0]["invoice_id"] == issued["id"]
    assert base64.b64decode(document["content_base64"]).startswith(b"%PDF-")


def test_customer_mcp_surface_redacts_pii_and_binds_customer_writes(
    tmp_path: Path, monkeypatch
):
    pytest.importorskip("mcp.server.fastmcp")
    from finance_core.errors import FinanceAuthorizationError
    from finance_core.mcp_server import create_server

    server = create_server(home=tmp_path)
    names = set(server._tool_manager._tools)
    assert {
        "finance.customer.get_sensitive",
        "finance.customer.contact_create",
        "finance.customer.contact_list",
        "finance.customer.contact_update",
        "finance.customer.contact_archive",
        "finance.customer.lifecycle_update",
        "finance.customer.payment_settings_update",
        "finance.customer.relationship_add",
        "finance.customer.relationship_list",
        "finance.customer.duplicate_suggest",
        "finance.customer.merge",
        "finance.customer.activity_record",
        "finance.customer.activity_list",
        "finance.customer.dashboard",
        "finance.customer.import_csv",
        "finance.customer.portal_token_create",
        "finance.customer.portal_invoice_list",
        "finance.customer.portal_invoice_get",
        "finance.customer.portal_invoice_download",
        "finance.customer.portal_token_revoke",
        "finance.customer.access_events",
    } <= names

    monkeypatch.setenv("HERMES_SESSION_USER_ID", "owner-1")
    create = server._tool_manager._tools["finance.customer.create"].fn
    contact_create = server._tool_manager._tools["finance.customer.contact_create"].fn
    contact_list = server._tool_manager._tools["finance.customer.contact_list"].fn
    sensitive_get = server._tool_manager._tools["finance.customer.get_sensitive"].fn
    customer = create(
        "company",
        "MCP Customer",
        {"billing_name": "MCP Customer", "billing_address": "Tokyo", "email": "pii@example.test"},
    )
    contact = contact_create(
        customer["id"],
        "Billing Contact",
        "owner-1",
        email="contact@example.test",
        role="billing",
    )

    assert "contact@example.test" not in json.dumps(contact)
    assert "contact@example.test" not in json.dumps(contact_list(customer["id"]))
    assert contact_list(customer["id"], True, "owner-1")[0]["email"] == "contact@example.test"
    assert sensitive_get(customer["id"], "owner-1")["billing_profile"]["email"] == "pii@example.test"
    with pytest.raises(FinanceAuthorizationError):
        contact_create(customer["id"], "Spoofed", "spoofed", email="bad@example.test")


def test_customer_csv_mixed_create_and_upsert_returns_stable_ids(tmp_path: Path):
    core = _core(tmp_path)
    existing = _customer(
        core,
        "Existing Company",
        corporate_number="3333333333333",
        external_id="existing-1",
    )
    csv_text = (
        "external_id,customer_kind,legal_name,billing_address,corporate_number,tags\n"
        "existing-1,company,Existing Company Updated,Osaka,3333333333333,updated\n"
        "new-1,company,New Company,Kyoto,4444444444444,new\n"
    )

    applied = core.import_customers_csv(
        csv_text=csv_text, apply=True, upsert=True, imported_by="owner-1"
    )
    ids = {row["row_number"]: row["customer_id"] for row in applied["customers"]}

    assert ids[2] == existing["id"]
    assert ids[3] != existing["id"]
    assert core.search_customers(query="Existing Company Updated")[0]["id"] == existing["id"]


def test_customer_merge_moves_drafts_and_contacts_but_preserves_issued_history(tmp_path: Path):
    core = _core(tmp_path)
    issuer = core.create_issuer(legal_name="Merge Issuer")
    target = _customer(
        core,
        "Merge Target",
        corporate_number="5555555555555",
        external_id="merge-target-2",
    )
    source = _customer(
        core,
        "Merge Source",
        corporate_number="6666666666666",
        external_id="merge-source-2",
    )
    core.create_customer_contact(
        source["id"],
        name="Source Contact",
        email="source-contact@example.test",
        added_by="owner-1",
    )

    def draft(key: str):
        return core.draft_create(
            issuer_id=issuer["id"],
            customer_id=source["id"],
            template_id="invoice-standard-jp",
            template_version="1.0.0",
            issue_date="2026-08-01",
            service_period="2026-08",
            due_date="2026-08-31",
            currency="JPY",
            lines=[_line(unit_price="1000")],
            billing_key=key,
            created_by="owner-1",
        )

    issued_draft = draft("merge-issued")
    core.approve_invoice(issued_draft["id"], approved_by="owner-1")
    issued = core.issue_invoice(issued_draft["id"], issued_by="owner-1")
    draft_invoice = draft("merge-draft")

    core.merge_customers(
        source["id"], target["id"], merged_by="owner-1", reason="master consolidation"
    )

    assert core.search_invoices(customer_id=source["id"])[0]["id"] == issued["id"]
    assert core.search_invoices(customer_id=target["id"])[0]["id"] == draft_invoice["id"]
    assert core.list_customer_contacts(target["id"])[0]["customer_id"] == target["id"]
