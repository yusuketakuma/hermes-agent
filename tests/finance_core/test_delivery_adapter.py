"""End-to-end delivery and settlement behavior for Finance Core."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests.finance_core.test_core import FakePdfRenderer


class RecordingEmailTransport:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.messages: list[dict] = []

    def send(self, **payload):
        if self.fail:
            raise RuntimeError("SMTP credentials must never escape the adapter")
        self.messages.append(payload)
        return {"provider": "sandbox", "message_id": "email-sandbox-1"}


class RecordingDiscordTransport:
    def __init__(self):
        self.messages: list[dict] = []

    def send(self, **payload):
        self.messages.append(payload)
        return {"provider": "sandbox", "message_id": "discord-sandbox-1"}


def test_discord_delivery_transport_sends_issued_pdf_through_shared_transport(tmp_path: Path, monkeypatch):
    from finance_core.domains.delivery import DiscordDeliveryTransport

    attachment = tmp_path / "invoice_INV-0001.pdf"
    attachment.write_bytes(b"pdf")
    calls: list[dict] = []

    monkeypatch.setattr(
        "tools.send_message_tool._handle_send",
        lambda args: calls.append(args) or '{"success": true, "message_id": "discord-1"}',
    )

    result = DiscordDeliveryTransport().send(
        recipient="1234567890",
        subject="請求書 INV-0001",
        body="請求書を送付します。",
        attachment_path=str(attachment),
        attachment_sha256=hashlib.sha256(b"pdf").hexdigest(),
    )

    assert result == {"provider": "discord", "message_id": "discord-1"}
    assert calls[0]["target"] == "discord:1234567890"
    assert f"MEDIA:{attachment}" in calls[0]["message"]


def _core(tmp_path: Path, transport: RecordingEmailTransport):
    from finance_core.core import FinanceCore

    return FinanceCore(
        tmp_path,
        renderer=FakePdfRenderer(),
        email_transport=transport,
    )


def test_issue_discord_outbox_and_dispatch_uses_the_canonical_core(tmp_path: Path):
    from finance_core.core import FinanceCore

    transport = RecordingDiscordTransport()
    core = FinanceCore(
        tmp_path,
        renderer=FakePdfRenderer(),
        discord_transport=transport,
    )
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
        lines=[
            {
                "description": "Discord delivery",
                "quantity": "1",
                "unit_price": "1000",
                "tax_category": "STANDARD_10",
                "tax_rate": "10",
            }
        ],
        created_by="owner-1",
    )
    core.approve_invoice(draft["id"], approved_by="owner-1")
    issued = core.issue_invoice(draft["id"], issued_by="owner-1")
    queued = core.prepare_delivery(
        issued["id"],
        method="discord",
        recipient="1234567890",
        prepared_by="owner-1",
    )

    sent = core.dispatch_discord(queued["id"], sent_by="owner-1")

    assert sent["status"] == "SENT"
    assert transport.messages[0]["recipient"] == "1234567890"
    assert Path(transport.messages[0]["attachment_path"]).name.startswith("invoice_")
    assert core.list_delivery_outbox(issued["id"])[0]["status"] == "SENT"


def _masters(core):
    issuer = core.create_issuer(
        legal_name="E2E Issuer",
        qualified_invoice_issuer=True,
        registration_number="T1234567890123",
    )
    customer = core.create_customer(
        customer_kind="company",
        legal_name="E2E Customer",
        billing_profile={
            "billing_name": "E2E Customer",
            "billing_address": "Tokyo",
            "email": "e2e-recipient@example.test",
        },
    )
    return issuer, customer


def test_issue_email_outbox_and_payment_e2e(tmp_path: Path):
    transport = RecordingEmailTransport()
    core = _core(tmp_path, transport)
    issuer, customer = _masters(core)
    contract = core.create_contract(
        customer_id=customer["id"],
        name="E2E contract",
        effective_from="2026-01-01",
        created_by="owner-1",
    )
    rule = core.create_billing_rule(
        contract_id=contract["id"],
        rule_type="HOURLY",
        description="E2E consulting",
        unit="hour",
        unit_price="3000",
        tax_category="STANDARD_10",
        effective_from="2026-01-01",
        created_by="owner-1",
    )
    first = core.record_work_entry(
        customer_id=customer["id"],
        contract_id=contract["id"],
        billing_rule_id=rule["id"],
        service_date="2026-08-10",
        quantity="2",
        source_id="e2e-work-1",
        created_by="owner-1",
    )
    second = core.record_work_entry(
        customer_id=customer["id"],
        contract_id=contract["id"],
        billing_rule_id=rule["id"],
        service_date="2026-08-20",
        quantity="1",
        source_id="e2e-work-2",
        created_by="owner-1",
    )

    draft = core.draft_from_work_entries(
        issuer_id=issuer["id"],
        billing_rule_id=rule["id"],
        service_period="2026-08",
        issue_date="2026-08-31",
        due_date="2026-09-30",
        template_id="invoice-standard-jp",
        billing_key="e2e-work:2026-08",
        created_by="owner-1",
    )
    core.approve_invoice(draft["id"], approved_by="owner-1")
    issued = core.issue_invoice(draft["id"], issued_by="owner-1")
    queued = core.prepare_delivery(issued["id"], method="email", prepared_by="owner-1")

    sent = core.dispatch_email(queued["id"], sent_by="owner-1")

    assert sent["status"] == "SENT"
    assert transport.messages[0]["recipient"] == "e2e-recipient@example.test"
    assert Path(transport.messages[0]["attachment_path"]).name.startswith("invoice_")
    assert "recipient" not in sent
    assert core.list_delivery_outbox(issued["id"])[0]["status"] == "SENT"

    payment = core.import_payment_csv(
        csv_text=(
            "received_date,value_date,amount,currency,bank_transaction_id,payer_name_raw,remittance_information\n"
            f"2026-09-05,2026-09-05,{issued['totals']['collectible_total']},JPY,e2e-bank-1,Private Payer,{issued['invoice_number']}\n"
        ),
        apply=True,
        imported_by="owner-1",
    )
    allocated = core.allocate_payment(
        payment["payments"][0]["id"],
        allocations=[{"invoice_id": issued["id"], "amount": issued["totals"]["collectible_total"]}],
        approved_by="owner-1",
    )
    dashboard = core.dashboard_summary(from_date="2026-08-01", to_date="2026-09-30")

    entries = {row["id"]: row for row in core.list_work_entries()}
    assert entries[first["id"]]["status"] == "BILLED"
    assert entries[second["id"]]["status"] == "BILLED"
    assert allocated["payment_status"] == "ALLOCATED"
    assert core.check_payment(issued["id"])["outstanding"] == "0"
    assert dashboard["received_total"] == issued["totals"]["collectible_total"]


def test_email_dispatch_failure_is_redacted_and_retryable(tmp_path: Path):
    transport = RecordingEmailTransport(fail=True)
    core = _core(tmp_path, transport)
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
        lines=[
            {
                "description": "Support",
                "quantity": "1",
                "unit_price": "1000",
                "tax_category": "STANDARD_10",
                "tax_rate": "10",
            }
        ],
        created_by="owner-1",
    )
    core.approve_invoice(draft["id"], approved_by="owner-1")
    issued = core.issue_invoice(draft["id"], issued_by="owner-1")
    queued = core.prepare_delivery(issued["id"], method="email", prepared_by="owner-1")

    result = core.dispatch_email(queued["id"], sent_by="owner-1")

    assert result["success"] is False
    assert result["delivery_status"] == "FAILED"
    assert "SMTP credentials" not in str(result)
    assert core.list_delivery_outbox(issued["id"])[0]["attempts"] == 1


def test_smtp_adapter_rejects_header_injection_before_network(tmp_path: Path):
    from finance_core.domains.delivery import SmtpEmailTransport
    from finance_core.errors import FinanceValidationError

    attachment = tmp_path / "invoice.pdf"
    attachment.write_bytes(b"pdf")
    adapter = SmtpEmailTransport(
        sender="sender@example.test",
        password="secret",
        host="smtp.example.test",
    )

    with pytest.raises(FinanceValidationError, match="header"):
        adapter.send(
            recipient="victim@example.test\nBcc: attacker@example.test",
            subject="Invoice",
            body="Invoice",
            attachment_path=str(attachment),
            attachment_sha256=hashlib.sha256(b"pdf").hexdigest(),
        )
