"""Behavioral coverage for the freee-inspired local Finance Core features."""

from __future__ import annotations

import csv
import io
from pathlib import Path


class FakePdfRenderer:
    def __init__(self) -> None:
        self.html: list[str] = []

    def render(self, html: str) -> bytes:
        self.html.append(html)
        return b"%PDF-1.7\nfeature-test\n%%EOF\n"


def _core(tmp_path: Path):
    from finance_core.core import FinanceCore

    return FinanceCore(tmp_path, renderer=FakePdfRenderer())


def _masters(core):
    issuer = core.create_issuer(
        legal_name="Feature Issuer",
        qualified_invoice_issuer=True,
        registration_number="T1234567890123",
    )
    customer = core.create_customer(
        customer_kind="company",
        legal_name="Feature Customer",
        billing_profile={"billing_name": "Feature Customer", "billing_address": "Tokyo"},
    )
    return issuer, customer


def _request(issuer_id: str, customer_id: str, *, period: str, billing_key: str):
    return {
        "issuer_id": issuer_id,
        "customer_id": customer_id,
        "template_id": "invoice-standard-jp",
        "template_version": "1.0.0",
        "issue_date": f"{period}-01",
        "service_period": period,
        "due_date": f"{period}-28",
        "currency": "JPY",
        "billing_key": billing_key,
        "lines": [{
            "description": "Feature service",
            "quantity": "1",
            "unit": "month",
            "unit_price": "10000",
            "tax_category": "STANDARD_10",
            "tax_rate": "10",
            "service_period": period,
            "generated_by": "rule",
        }],
    }


def _issue(core, request):
    draft = core.draft_create(**request)
    core.approve_invoice(draft["id"], approved_by="owner-1")
    return core.issue_invoice(draft["id"], issued_by="owner-1")


def test_bulk_draft_creation_is_idempotent_by_billing_key(tmp_path: Path):
    core = _core(tmp_path)
    issuer, customer = _masters(core)
    request = _request(issuer["id"], customer["id"], period="2026-08", billing_key="contract-1:2026-08")

    first = core.bulk_draft_create([request])
    second = core.bulk_draft_create([request])

    assert len(first["created"]) == 1
    assert second["created"] == []
    assert second["existing"][0]["invoice_id"] == first["created"][0]["id"]


def test_search_overdue_and_revenue_reports_are_pii_free(tmp_path: Path):
    core = _core(tmp_path)
    issuer, customer = _masters(core)
    overdue = _issue(core, _request(issuer["id"], customer["id"], period="2026-07", billing_key="old"))
    current = _issue(core, _request(issuer["id"], customer["id"], period="2026-08", billing_key="new"))

    results = core.search_invoices(customer_id=customer["id"], document_status="ISSUED")
    overdue_rows = core.overdue_invoices(today="2026-08-14")
    summary = core.revenue_summary(from_date="2026-07-01", to_date="2026-08-31")
    by_customer = core.revenue_by_customer(from_date="2026-07-01", to_date="2026-08-31")
    by_month = core.revenue_by_month(from_date="2026-07-01", to_date="2026-08-31")

    assert {row["id"] for row in results} == {overdue["id"], current["id"]}
    assert [row["invoice_id"] for row in overdue_rows] == [overdue["id"]]
    assert summary["invoice_count"] == 2
    assert summary["invoiced_total"] == "22000"
    assert summary["outstanding_total"] == "22000"
    assert by_customer[0]["customer_id"] == customer["id"]
    assert by_customer[0]["invoiced_total"] == "22000"
    assert {row["month"] for row in by_month} == {"2026-07", "2026-08"}
    assert "Tokyo" not in str(summary)


def test_delivery_queue_tracks_status_and_download_history(tmp_path: Path):
    core = _core(tmp_path)
    issuer, customer = _masters(core)
    invoice = _issue(core, _request(issuer["id"], customer["id"], period="2026-08", billing_key="delivery"))

    queued = core.prepare_delivery(invoice["id"], method="email", prepared_by="owner-1")
    sent = core.update_delivery(
        queued["id"], status="SENT", updated_by="owner-1", external_reference="delivery-1"
    )
    downloaded = core.record_delivery_download(queued["id"], downloaded_at="2026-08-15T01:02:03+00:00")

    assert queued["status"] == "QUEUED"
    assert sent["status"] == "SENT"
    assert downloaded["downloaded_at"] == "2026-08-15T01:02:03+00:00"
    assert core.list_deliveries(invoice["id"])[0]["external_reference"] == "delivery-1"


def test_accounting_export_is_importable_and_excludes_customer_pii(tmp_path: Path):
    core = _core(tmp_path)
    issuer, customer = _masters(core)
    _issue(core, _request(issuer["id"], customer["id"], period="2026-08", billing_key="export"))

    exported = core.export_accounting_csv(from_date="2026-08-01", to_date="2026-08-31")
    rows = list(csv.DictReader(io.StringIO(exported["csv"])))

    assert exported["row_count"] == 3
    assert len(rows) == 3
    assert {row["account"] for row in rows} == {"accounts_receivable", "sales", "consumption_tax"}
    assert "Tokyo" not in exported["csv"]
    assert "billing_profile" not in exported["csv"]


def test_supported_document_types_use_their_label_in_the_preview(tmp_path: Path):
    from finance_core.core import FinanceCore

    renderer = FakePdfRenderer()
    core = FinanceCore(tmp_path, renderer=renderer)
    issuer, customer = _masters(core)
    request = _request(issuer["id"], customer["id"], period="2026-08", billing_key="quote")
    request["document_type"] = "quote"

    draft = core.draft_create(**request)
    preview = core.preview_pdf(draft["id"])

    assert preview["document_type"] == "quote"
    assert "見積書" in renderer.html[-1]
