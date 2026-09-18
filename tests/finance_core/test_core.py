"""Behavioral tests for the independent Finance Core."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


class FakePdfRenderer:
    def __init__(self) -> None:
        self.html: list[str] = []

    def render(self, html: str) -> bytes:
        self.html.append(html)
        return b"%PDF-1.7\nfinance-core-test\n%%EOF\n"


def _line(
    description: str = "業務委託",
    *,
    quantity: str = "1",
    unit_price: str = "5500",
    tax_category: str = "STANDARD_10",
    tax_rate: str = "10",
) -> dict[str, str]:
    return {
        "description": description,
        "quantity": quantity,
        "unit": "hour",
        "unit_price": unit_price,
        "tax_category": tax_category,
        "tax_rate": tax_rate,
        "service_period": "2026-08",
        "generated_by": "manual",
    }


def _build_core(tmp_path: Path):
    from finance_core.core import FinanceCore
    from finance_core.renderer import PdfRenderer

    renderer = FakePdfRenderer()
    core = FinanceCore(tmp_path, renderer=renderer)
    return core, renderer


def _create_masters(core):
    issuer = core.create_issuer(
        legal_name="Example LLC",
        qualified_invoice_issuer=True,
        registration_number="T1234567890123",
        bank_account={"bank_name": "Example Bank", "account_number": "1234567"},
    )
    customer = core.create_customer(
        customer_kind="company",
        legal_name="Acme Co.",
        billing_profile={
            "billing_name": "Acme Co.",
            "billing_address": "Tokyo",
            "email": "billing@example.test",
        },
    )
    return issuer, customer


def test_tax_is_explicit_and_rounded_once_per_category():
    from finance_core.errors import FinanceValidationError
    from finance_core.tax import calculate_totals

    lines = [_line(f"line-{index}", unit_price="1") for index in range(5)]
    lines.append(_line("非課税", unit_price="100", tax_category="EXEMPT", tax_rate="0"))

    totals = calculate_totals(lines, currency="JPY", rounding="HALF_UP")

    assert totals["subtotal"] == "105"
    assert totals["tax"] == "1"
    assert totals["total"] == "106"
    assert totals["tax_breakdown"] == [
        {
            "tax_category": "STANDARD_10",
            "tax_rate": "10",
            "taxable_amount": "5",
            "tax_amount": "1",
        },
        {
            "tax_category": "EXEMPT",
            "tax_rate": "0",
            "taxable_amount": "100",
            "tax_amount": "0",
        },
    ]

    missing_category = _line()
    missing_category.pop("tax_category")
    with pytest.raises(FinanceValidationError, match="tax_category"):
        calculate_totals([missing_category], currency="JPY", rounding="HALF_UP")


def test_template_version_contains_only_non_personal_layout_metadata():
    from finance_core.templates import TemplateRegistry

    version = TemplateRegistry().get("invoice-standard-jp", "1.0.0")

    assert version["source_format"] == "xlsx"
    assert version["runtime_format"] == "html-css"
    assert version["currency"] == "JPY"
    assert version["locale"] == "ja-JP"
    assert version["page_size"] == "A4"
    assert len(version["source_asset_sha256"]) == 64
    assert version["source_asset_retained"] is False
    assert "{{ customer.billing_address }}" in version["placeholders"]
    assert version["approved_at"] == "2026-08-14"
    assert version["approved_by"] == "owner"
    assert "Acme" not in json.dumps(version, ensure_ascii=False)


def test_draft_is_unissued_and_preview_has_draft_watermark(tmp_path: Path):
    core, renderer = _build_core(tmp_path)
    issuer, customer = _create_masters(core)

    draft = core.draft_create(
        issuer_id=issuer["id"],
        customer_id=customer["id"],
        template_id="invoice-standard-jp",
        template_version="1.0.0",
        issue_date="2026-08-01",
        service_period="2026-08",
        due_date="2026-08-31",
        currency="JPY",
        lines=[_line()],
    )

    assert draft["document_status"] == "DRAFT"
    assert draft["invoice_number"] is None
    preview = core.preview_pdf(draft["id"])

    assert preview["document_status"] == "DRAFT"
    assert Path(preview["path"]).name == f"draft_{draft['id']}.pdf"
    assert preview["sha256"]
    assert "DRAFT" in renderer.html[-1]
    assert "請求No." in renderer.html[-1]
    assert "お支払い期限" in renderer.html[-1]
    assert "お振込先詳細" in renderer.html[-1]
    assert "課税10％" in renderer.html[-1]


def test_issue_is_immutable_snapshotted_and_exclusively_numbered(tmp_path: Path):
    from finance_core.errors import FinanceConflictError

    core, _renderer = _build_core(tmp_path)
    issuer, customer = _create_masters(core)

    def create_and_issue():
        draft = core.draft_create(
            issuer_id=issuer["id"],
            customer_id=customer["id"],
            template_id="invoice-standard-jp",
            template_version="1.0.0",
            issue_date="2026-08-01",
            service_period="2026-08",
            due_date="2026-08-31",
            currency="JPY",
            lines=[_line()],
        )
        core.approve_invoice(draft["id"], approved_by="owner-1")
        return core.issue_invoice(draft["id"], issued_by="owner-1")

    first = create_and_issue()
    second = create_and_issue()

    assert first["invoice_number"] == "INV-202608-0001"
    assert second["invoice_number"] == "INV-202608-0002"
    assert first["document_status"] == "ISSUED"
    assert first["master_snapshot"]["customer"]["billing_profile"]["billing_address"] == "Tokyo"
    assert Path(first["document"]["pdf_path"]).name == "invoice_INV-202608-0001.pdf"
    assert Path(first["document"]["json_path"]).name == "invoice_INV-202608-0001.json"
    assert len(first["document"]["pdf_sha256"]) == 64
    assert len(first["document"]["json_sha256"]) == 64

    with pytest.raises(FinanceConflictError):
        core.issue_invoice(first["id"], issued_by="owner-1")

    audit = core.list_audit_events(first["id"])
    assert [event["action"] for event in audit] == [
        "invoice.draft_create",
        "invoice.approve",
        "invoice.issue",
    ]
    assert "billing@example.test" not in json.dumps(audit, ensure_ascii=False)


def test_mcp_server_exposes_finance_boundary_tools(tmp_path: Path):
    pytest.importorskip("mcp.server.fastmcp")
    from finance_core.mcp_server import create_server

    server = create_server(home=tmp_path)
    names = set(server._tool_manager._tools)

    assert {
        "finance.customer.create",
        "finance.customer.get",
        "finance.template.list_versions",
        "finance.invoice.draft_create",
        "finance.invoice.validate",
        "finance.invoice.preview_pdf",
        "finance.invoice.approve",
        "finance.invoice.issue",
        "finance.invoice.get",
        "finance.invoice.bulk_draft_create",
        "finance.invoice.search",
        "finance.receivables.overdue_list",
        "finance.receivables.reminder_candidates",
        "finance.revenue.summary",
        "finance.revenue.by_customer",
        "finance.revenue.by_month",
        "finance.delivery.prepare",
        "finance.delivery.update_status",
        "finance.delivery.list",
        "finance.delivery.record_download",
        "finance.accounting.export_csv",
        "finance.payment.import_csv",
        "finance.payment.match_suggest",
        "finance.payment.allocate",
        "finance.payment.unallocated_list",
        "finance.payment.check",
    } <= names

    created = server._tool_manager._tools["finance.customer.create"].fn(
        "company",
        "Private Co.",
        {"billing_name": "Private Co.", "billing_address": "Secret street", "email": "secret@example.test"},
    )
    assert created["legal_name"] == "Private Co."
    assert "email" not in json.dumps(created, ensure_ascii=False)
    assert "Secret street" not in json.dumps(created, ensure_ascii=False)


def test_mcp_preview_keeps_artifact_metadata_without_master_pii():
    from finance_core.mcp_server import _public_invoice

    public = _public_invoice(
        {
            "id": "finv_preview",
            "path": "/private/finance-core/draft_finv_preview.pdf",
            "sha256": "a" * 64,
            "customer": {"billing_address": "Secret street"},
        }
    )

    assert public["path"].endswith("draft_finv_preview.pdf")
    assert public["sha256"] == "a" * 64
    assert "Secret street" not in json.dumps(public, ensure_ascii=False)


def test_mcp_approval_labels_are_bound_to_the_session_identity(tmp_path: Path, monkeypatch):
    pytest.importorskip("mcp.server.fastmcp")
    from finance_core.errors import FinanceAuthorizationError
    from finance_core.mcp_server import create_server

    server = create_server(home=tmp_path)
    approve = server._tool_manager._tools["finance.invoice.approve"].fn
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "owner-1")

    with pytest.raises(FinanceAuthorizationError):
        approve("invoice-1", "spoofed-owner")

    monkeypatch.delenv("HERMES_SESSION_USER_ID")
    with pytest.raises(FinanceAuthorizationError):
        approve("invoice-1", "owner-1")


def test_mcp_approval_uses_per_call_request_metadata():
    from finance_core.errors import FinanceAuthorizationError
    from finance_core.mcp_server import _authenticated_actor

    class Meta:
        model_extra = {"hermes_session_user_id": "discord-user-42"}

    class RequestContext:
        meta = Meta()

    class Context:
        request_context = RequestContext()

    assert _authenticated_actor(
        "discord-user-42",
        field="approved_by",
        context=Context(),
    ) == "discord-user-42"

    with pytest.raises(FinanceAuthorizationError):
        _authenticated_actor("spoofed", field="approved_by", context=Context())
