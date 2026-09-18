"""Behavioral tests for the accounting workflow plugin."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace


def _channels() -> list[dict[str, str]]:
    return [
        {
            "id": "100",
            "name": "general",
            "guild": "Acme",
            "type": "channel",
        },
        {
            "id": "200",
            "name": "invoice-delivery",
            "guild": "Acme",
            "topic": "請求書と入金管理",
            "type": "channel",
        },
        {
            "id": "300",
            "name": "finance",
            "guild": "Other",
            "type": "channel",
        },
    ]


def test_select_accounting_channel_prefers_finance_purpose():
    from plugins.accounting.channel_selection import select_accounting_channel

    selected = select_accounting_channel(_channels())

    assert selected is not None
    assert selected["id"] == "200"
    assert selected["selection_reason"]


def test_accounting_workflow_requires_approval_before_send(tmp_path: Path):
    from plugins.accounting.service import AccountingService
    from plugins.accounting.store import AccountingStore

    sent: list[dict] = []

    def send_message(**payload):
        sent.append(payload)
        return {"success": True, "message_id": "message-1"}

    service = AccountingService(
        store=AccountingStore(tmp_path / "accounting.sqlite3"),
        channel_provider=lambda: _channels(),
        send_message=send_message,
        config={"approver_user_id": "owner"},
    )
    created = service.create_invoice(
        customer={"name": "Example Co."},
        items=[{"description": "Consulting", "quantity": 2, "unit_price": "5000"}],
        currency="JPY",
        due_date="2030-01-31",
    )

    assert created["status"] == "draft"
    assert created["total"] == "10000.00"
    assert Path(created["document_path"]).exists()

    prepared = service.prepare_invoice(created["id"])
    assert prepared["status"] == "pending_approval"
    assert prepared["channel"]["id"] == "200"

    rejected = service.send_invoice(created["id"])
    assert rejected["success"] is False
    assert "approval" in rejected["error"]
    assert sent == []

    approved = service.approve_invoice(
        created["id"], approved_by="owner", actor_id="owner"
    )
    assert approved["success"] is True
    assert approved["invoice"]["status"] == "sent"
    assert sent[0]["target"] == "discord:200"
    assert f"MEDIA:{created['document_path']}" in sent[0]["message"]


def test_payment_confirmation_transitions_sent_invoice_to_paid(tmp_path: Path):
    from plugins.accounting.service import AccountingService
    from plugins.accounting.store import AccountingStore

    service = AccountingService(
        store=AccountingStore(tmp_path / "accounting.sqlite3"),
        channel_provider=lambda: _channels(),
        send_message=lambda **_: {"success": True, "message_id": "message-1"},
        config={"approver_user_id": "owner"},
    )
    invoice = service.create_invoice(
        customer={"name": "Example Co."},
        items=[{"description": "Support", "quantity": 1, "unit_price": "1200"}],
        currency="JPY",
        due_date="2030-01-31",
    )
    service.prepare_invoice(invoice["id"])
    service.approve_invoice(invoice["id"], approved_by="owner", actor_id="owner")

    before = service.check_payment(invoice["id"])
    assert before["status"] == "sent"
    assert before["outstanding"] == "1200.00"

    paid = service.record_payment(
        invoice["id"], amount="1200", payment_date="2030-01-15", reference="BANK-1"
    )

    assert paid["status"] == "paid"
    assert paid["outstanding"] == "0.00"
    assert paid["payment_reference"] == "BANK-1"


def test_payment_csv_preview_does_not_mutate_invoice(tmp_path: Path):
    from plugins.accounting.service import AccountingService
    from plugins.accounting.store import AccountingStore

    service = AccountingService(
        store=AccountingStore(tmp_path / "accounting.sqlite3"),
        channel_provider=lambda: _channels(),
        send_message=lambda **_: {"success": True, "message_id": "message-1"},
        config={"approver_user_id": "owner-1"},
    )
    invoice = service.create_invoice(
        customer={"name": "Example Co."},
        items=[{"description": "Support", "quantity": 1, "unit_price": "2000"}],
        currency="JPY",
        due_date="2030-01-31",
    )
    service.prepare_invoice(invoice["id"])
    service.approve_invoice(invoice["id"], actor_id="owner-1")
    csv_text = (
        "invoice_number,amount,payment_date,reference\n"
        f"{invoice['invoice_number']},1000,2030-01-15,BANK-CSV-1\n"
    )

    preview = service.import_payments_csv(csv_text=csv_text)

    assert preview["success"] is True
    assert preview["dry_run"] is True
    assert preview["rows"][0]["status"] == "ready"
    assert service.check_payment(invoice["id"])["paid_amount"] == "0.00"


def test_payment_csv_apply_is_authenticated_atomic_and_idempotent(tmp_path: Path):
    from plugins.accounting.service import AccountingService
    from plugins.accounting.store import AccountingStore

    service = AccountingService(
        store=AccountingStore(tmp_path / "accounting.sqlite3"),
        channel_provider=lambda: _channels(),
        send_message=lambda **_: {"success": True, "message_id": "message-1"},
        config={"approver_user_id": "owner-1"},
    )
    invoice = service.create_invoice(
        customer={"name": "Example Co."},
        items=[{"description": "Support", "quantity": 1, "unit_price": "2000"}],
        currency="JPY",
        due_date="2030-01-31",
    )
    service.prepare_invoice(invoice["id"])
    service.approve_invoice(invoice["id"], actor_id="owner-1")
    csv_text = (
        "invoice_number,amount,payment_date,reference\n"
        f"{invoice['invoice_number']},1000,2030-01-15,BANK-CSV-1\n"
        f"{invoice['invoice_number']},1000,2030-01-16,BANK-CSV-2\n"
    )

    unauthorized = service.import_payments_csv(csv_text=csv_text, apply=True)
    assert unauthorized["success"] is False
    assert service.check_payment(invoice["id"])["paid_amount"] == "0.00"

    applied = service.import_payments_csv(
        csv_text=csv_text,
        apply=True,
        actor_id="owner-1",
    )
    assert applied["success"] is True
    assert applied["applied_count"] == 2
    assert service.check_payment(invoice["id"])["paid_amount"] == "2000.00"

    repeated = service.import_payments_csv(
        csv_text=csv_text,
        apply=True,
        actor_id="owner-1",
    )
    assert repeated["success"] is False
    assert service.check_payment(invoice["id"])["paid_amount"] == "2000.00"


def test_payment_csv_overpayment_is_rejected_without_partial_application(tmp_path: Path):
    from plugins.accounting.service import AccountingService
    from plugins.accounting.store import AccountingStore

    service = AccountingService(
        store=AccountingStore(tmp_path / "accounting.sqlite3"),
        channel_provider=lambda: _channels(),
        send_message=lambda **_: {"success": True, "message_id": "message-1"},
        config={"approver_user_id": "owner-1"},
    )
    invoice = service.create_invoice(
        customer={"name": "Example Co."},
        items=[{"description": "Support", "quantity": 1, "unit_price": "2000"}],
        currency="JPY",
        due_date="2030-01-31",
    )
    service.prepare_invoice(invoice["id"])
    service.approve_invoice(invoice["id"], actor_id="owner-1")
    csv_text = (
        "invoice_number,amount,payment_date,reference\n"
        f"{invoice['invoice_number']},1500,2030-01-15,BANK-CSV-1\n"
        f"{invoice['invoice_number']},600,2030-01-16,BANK-CSV-2\n"
    )

    result = service.import_payments_csv(
        csv_text=csv_text,
        apply=True,
        actor_id="owner-1",
    )

    assert result["success"] is False
    assert any("exceed" in error["error"] for error in result["errors"])
    assert service.check_payment(invoice["id"])["paid_amount"] == "0.00"


def test_concurrent_payments_are_not_lost(tmp_path: Path):
    from plugins.accounting.service import AccountingService
    from plugins.accounting.store import AccountingStore

    read_barrier = Barrier(2)

    class CoordinatedStore(AccountingStore):
        def get(self, invoice_id):
            record = super().get(invoice_id)
            if record and record["status"] == "sent" and record["paid_amount"] == "0.00":
                read_barrier.wait(timeout=5)
            return record

    db_path = tmp_path / "accounting.sqlite3"
    creator = AccountingService(
        store=AccountingStore(db_path),
        channel_provider=lambda: _channels(),
        send_message=lambda **_: {"success": True, "message_id": "message-1"},
        config={"approver_user_id": "owner"},
    )
    invoice = creator.create_invoice(
        customer={"name": "Example Co."},
        items=[{"description": "Support", "quantity": 1, "unit_price": "2000"}],
        currency="JPY",
        due_date="2030-01-31",
    )
    creator.prepare_invoice(invoice["id"])
    creator.approve_invoice(invoice["id"], actor_id="owner")

    def build_service():
        return AccountingService(
            store=CoordinatedStore(db_path),
            channel_provider=lambda: _channels(),
        )

    services = [build_service(), build_service()]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda pair: pair[0].record_payment(
                    invoice["id"],
                    amount="1000",
                    payment_date="2030-01-15",
                    reference=pair[1],
                ),
                zip(services, ("BANK-A", "BANK-B")),
            )
        )

    assert all(result["success"] is True for result in results)
    final = creator.check_payment(invoice["id"])
    assert final["paid_amount"] == "2000.00"
    assert {item["reference"] for item in final["payments"]} == {"BANK-A", "BANK-B"}


def test_overpayment_is_rejected(tmp_path: Path):
    from plugins.accounting.service import AccountingError, AccountingService
    from plugins.accounting.store import AccountingStore

    service = AccountingService(
        store=AccountingStore(tmp_path / "accounting.sqlite3"),
        channel_provider=lambda: _channels(),
        send_message=lambda **_: {"success": True},
        config={"approver_user_id": "owner"},
    )
    invoice = service.create_invoice(
        customer={"name": "Example Co."},
        items=[{"description": "Support", "quantity": 1, "unit_price": "1000"}],
        currency="JPY",
        due_date="2030-01-31",
    )
    service.prepare_invoice(invoice["id"])
    service.approve_invoice(invoice["id"], actor_id="owner")

    try:
        service.record_payment(
            invoice["id"],
            amount="1000.01",
            payment_date="2030-01-15",
            reference="BANK-OVER",
        )
    except AccountingError as exc:
        assert "exceed" in str(exc)
    else:
        raise AssertionError("overpayment was accepted")


def test_reminders_are_idempotent_for_one_day(tmp_path: Path):
    from plugins.accounting.service import AccountingService
    from plugins.accounting.store import AccountingStore

    sent: list[dict] = []
    service = AccountingService(
        store=AccountingStore(tmp_path / "accounting.sqlite3"),
        channel_provider=lambda: _channels(),
        send_message=lambda **payload: sent.append(payload) or {"success": True},
        config={"approver_user_id": "owner-1"},
    )
    invoice = service.create_invoice(
        customer={"name": "Example Co."},
        items=[{"description": "Retainer", "quantity": 1, "unit_price": "3000"}],
        currency="JPY",
        due_date="2030-01-10",
        issue_date="2030-01-01",
    )
    service.prepare_invoice(invoice["id"])

    first = service.remind_due(today="2030-01-03", reminder_days=7)
    second = service.remind_due(today="2030-01-03", reminder_days=7)

    assert first["sent_count"] == 1
    assert second["sent_count"] == 0
    assert len(sent) == 1


def test_concurrent_reminders_send_only_once(tmp_path: Path):
    from plugins.accounting.service import AccountingService
    from plugins.accounting.store import AccountingStore

    list_barrier = Barrier(2)
    sent: list[dict] = []

    class CoordinatedStore(AccountingStore):
        def list(self, statuses=None):
            records = super().list(statuses)
            if set(statuses or ()) == {"draft", "pending_approval"}:
                list_barrier.wait(timeout=5)
            return records

    db_path = tmp_path / "accounting.sqlite3"
    creator = AccountingService(
        store=AccountingStore(db_path),
        channel_provider=lambda: _channels(),
        send_message=lambda **payload: {"success": True, "message_id": "message-1"},
    )
    creator.create_invoice(
        customer={"name": "Example Co."},
        items=[{"description": "Retainer", "quantity": 1, "unit_price": "3000"}],
        currency="JPY",
        due_date="2030-01-10",
        issue_date="2030-01-01",
    )

    def build_service():
        return AccountingService(
            store=CoordinatedStore(db_path),
            channel_provider=lambda: _channels(),
            send_message=lambda **payload: sent.append(payload) or {"success": True},
        )

    services = [build_service(), build_service()]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda service: service.remind_due(
                    today="2030-01-03", reminder_days=7
                ),
                services,
            )
        )

    assert sum(result["sent_count"] for result in results) == 1
    assert len(sent) == 1


def test_reminder_delivery_exception_is_retryable(tmp_path: Path):
    from plugins.accounting.service import AccountingService
    from plugins.accounting.store import AccountingStore

    should_fail = True

    def send_message(**_payload):
        if should_fail:
            raise RuntimeError("temporary Discord outage")
        return {"success": True}

    service = AccountingService(
        store=AccountingStore(tmp_path / "accounting.sqlite3"),
        channel_provider=lambda: _channels(),
        send_message=send_message,
    )
    invoice = service.create_invoice(
        customer={"name": "Example Co."},
        items=[{"description": "Retainer", "quantity": 1, "unit_price": "3000"}],
        currency="JPY",
        due_date="2030-01-10",
        issue_date="2030-01-01",
    )

    failed = service.remind_due(today="2030-01-03", reminder_days=7)
    assert failed["success"] is False
    assert "RuntimeError" in failed["errors"][0]["error"]
    assert service.store.get(invoice["id"]).get("last_reminder_on") is None

    should_fail = False
    retried = service.remind_due(today="2030-01-03", reminder_days=7)
    assert retried["sent_count"] == 1


def test_stale_send_requires_explicit_retry(tmp_path: Path):
    from plugins.accounting.service import AccountingService
    from plugins.accounting.store import AccountingStore

    sent: list[dict] = []
    service = AccountingService(
        store=AccountingStore(tmp_path / "accounting.sqlite3"),
        channel_provider=lambda: _channels(),
        send_message=lambda **payload: sent.append(payload) or {"success": True},
        config={"approver_user_id": "owner-1"},
    )
    invoice = service.create_invoice(
        customer={"name": "Example Co."},
        items=[{"description": "Review", "quantity": 1, "unit_price": "100"}],
        currency="JPY",
        due_date="2030-01-31",
    )
    service.store.update(
        invoice["id"],
        {"status": "sending", "send_started_at": "2030-01-01T00:00:00+00:00"},
    )

    recovered = service.recover_send(
        invoice["id"],
        max_age_minutes=30,
        now="2030-01-02T00:00:00+00:00",
        actor_id="owner-1",
    )

    assert recovered["success"] is True
    assert recovered["invoice"]["status"] == "approved"
    assert recovered["invoice"]["delivery_uncertain"] is True
    assert sent == []

    rejected = service.send_invoice(invoice["id"])
    assert rejected["success"] is False
    retried = service.send_invoice(
        invoice["id"], force_retry=True, actor_id="owner-1"
    )
    assert retried["success"] is True
    assert len(sent) == 1


def test_accounting_does_not_fallback_to_general_channel(tmp_path: Path):
    from plugins.accounting.service import AccountingError, AccountingService
    from plugins.accounting.store import AccountingStore

    service = AccountingService(
        store=AccountingStore(tmp_path / "accounting.sqlite3"),
        channel_provider=lambda: [
            {"id": "1", "name": "general", "type": "channel"},
            {"id": "2", "name": "random", "type": "channel"},
        ],
    )

    assert service.list_channels()["selected"] is None
    invoice = service.create_invoice(
        customer={"name": "Example Co."},
        items=[{"description": "Review", "quantity": 1, "unit_price": "100"}],
        currency="JPY",
        due_date="2030-01-31",
    )
    try:
        service.prepare_invoice(invoice["id"])
    except AccountingError as exc:
        assert "accounting-purpose Discord channel" in str(exc)
    else:
        raise AssertionError("prepare_invoice unexpectedly selected a general channel")


def test_approval_can_be_restricted_to_configured_operator(tmp_path: Path):
    from plugins.accounting.service import AccountingService
    from plugins.accounting.store import AccountingStore

    service = AccountingService(
        store=AccountingStore(tmp_path / "accounting.sqlite3"),
        channel_provider=lambda: _channels(),
        send_message=lambda **_: {"success": True},
        config={"approver_user_id": "owner-1"},
    )
    invoice = service.create_invoice(
        customer={"name": "Example Co."},
        items=[{"description": "Review", "quantity": 1, "unit_price": "100"}],
        currency="JPY",
        due_date="2030-01-31",
    )
    service.prepare_invoice(invoice["id"])

    rejected = service.approve_invoice(
        invoice["id"], approved_by="other-user", actor_id="other-user"
    )

    assert rejected["success"] is False
    assert service.store.get(invoice["id"])["status"] == "pending_approval"


def test_approval_requires_authenticated_configured_operator(tmp_path: Path):
    from plugins.accounting.service import AccountingService
    from plugins.accounting.store import AccountingStore

    service = AccountingService(
        store=AccountingStore(tmp_path / "accounting.sqlite3"),
        channel_provider=lambda: _channels(),
        send_message=lambda **_: {"success": True, "message_id": "message-1"},
        config={"approver_user_id": "owner-1"},
    )
    invoice = service.create_invoice(
        customer={"name": "Example Co."},
        items=[{"description": "Review", "quantity": 1, "unit_price": "100"}],
        currency="JPY",
        due_date="2030-01-31",
    )
    service.prepare_invoice(invoice["id"])

    spoofed = service.approve_invoice(
        invoice["id"], approved_by="owner-1"
    )
    assert spoofed["success"] is False
    assert "authenticated" in spoofed["error"]
    assert service.store.get(invoice["id"])["status"] == "pending_approval"

    approved = service.approve_invoice(
        invoice["id"], approved_by="spoofed-label", actor_id="owner-1"
    )
    assert approved["success"] is True
    assert approved["invoice"]["approved_by"] == "owner-1"


def test_invoice_document_is_private_and_escapes_customer_data(tmp_path: Path):
    from plugins.accounting.service import AccountingService
    from plugins.accounting.store import AccountingStore

    service = AccountingService(
        store=AccountingStore(tmp_path / "accounting.sqlite3"),
        channel_provider=lambda: _channels(),
    )
    invoice = service.create_invoice(
        customer={"name": "<script>alert(1)</script>"},
        items=[{"description": "<b>Work</b>", "quantity": 1, "unit_price": "100"}],
        currency="JPY",
        due_date="2030-01-31",
    )
    document = Path(invoice["document_path"])

    assert os.stat(document).st_mode & 0o077 == 0
    contents = document.read_text(encoding="utf-8")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in contents
    assert "<script>alert(1)</script>" not in contents


def test_invoice_number_must_be_unique(tmp_path: Path):
    from plugins.accounting.service import AccountingError, AccountingService
    from plugins.accounting.store import AccountingStore

    service = AccountingService(
        store=AccountingStore(tmp_path / "accounting.sqlite3"),
        channel_provider=lambda: _channels(),
    )
    kwargs = {
        "customer": {"name": "Example Co."},
        "items": [{"description": "Review", "quantity": 1, "unit_price": "100"}],
        "currency": "JPY",
        "due_date": "2030-01-31",
        "invoice_number": "INV-001",
    }
    service.create_invoice(**kwargs)

    try:
        service.create_invoice(**kwargs)
    except AccountingError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("duplicate invoice number was accepted")


def test_recurring_invoice_creation_is_idempotent_for_a_period(tmp_path: Path):
    from plugins.accounting.service import AccountingService
    from plugins.accounting.store import AccountingStore

    service = AccountingService(
        store=AccountingStore(tmp_path / "accounting.sqlite3"),
        channel_provider=lambda: _channels(),
    )
    kwargs = {
        "template_id": "monthly-support",
        "period": "2030-02",
        "customer": {"name": "Example Co."},
        "items": [{"description": "Support", "quantity": 1, "unit_price": "3000"}],
        "currency": "JPY",
        "issue_day": 1,
        "due_days": 30,
    }

    first = service.create_recurring_invoice(**kwargs)
    second = service.create_recurring_invoice(**kwargs)

    assert first["status"] == "draft"
    assert first["issue_date"] == "2030-02-01"
    assert first["due_date"] == "2030-03-03"
    assert first["invoice_number"] == "monthly-support-203002"
    assert first["recurrence"] == {
        "template_id": "monthly-support",
        "period": "2030-02",
    }
    assert second["id"] == first["id"]
    assert len(service.list_invoices()) == 1


def test_invoice_events_are_available_as_an_audit_trail(tmp_path: Path):
    from plugins.accounting.service import AccountingService
    from plugins.accounting.store import AccountingStore

    service = AccountingService(
        store=AccountingStore(tmp_path / "accounting.sqlite3"),
        channel_provider=lambda: _channels(),
        send_message=lambda **_: {"success": True, "message_id": "message-1"},
        config={"approver_user_id": "owner-1"},
    )
    invoice = service.create_invoice(
        customer={"name": "Example Co."},
        items=[{"description": "Review", "quantity": 1, "unit_price": "100"}],
        currency="JPY",
        due_date="2030-01-31",
        actor_id="owner-1",
    )
    service.prepare_invoice(invoice["id"])
    service.approve_invoice(invoice["id"], actor_id="owner-1")

    result = service.handle("list_events", invoice_id=invoice["id"])

    assert result["success"] is True
    assert [event["action"] for event in result["events"]] == [
        "create_invoice",
        "prepare_for_approval",
        "approve_send",
        "begin_send",
        "send_invoice",
    ]
    assert result["events"][2]["actor"] == "owner-1"
    assert result["events"][0]["actor"] == "owner-1"


def test_plugin_exposes_no_local_accounting_tool_to_discord(tmp_path: Path):
    from plugins.accounting import register

    registered: list[dict] = []
    skills: list[dict] = []
    context = SimpleNamespace(
        state=SimpleNamespace(data_dir=tmp_path),
        get_config=lambda _key, default=None: default,
        register_tool=lambda **kwargs: registered.append(kwargs),
        register_skill=lambda **kwargs: skills.append(kwargs),
    )

    register(context)

    assert registered == []
    assert any(skill["name"] == "skill-fin-discord-finance-workflow" for skill in skills)
    assert any(skill["name"] == "skill-fin-invoice-generation" for skill in skills)
    assert any(skill["name"] == "skill-fin-payment-status-check" for skill in skills)
    assert any(skill["name"] == "skill-fin-revenue-tracking" for skill in skills)
    assert any(skill["name"] == "skill-fin-spreadsheet-integration" for skill in skills)
    assert not any(skill["name"] == "skill-fin-ai-assistance" for skill in skills)


def test_finance_core_is_the_only_model_visible_discord_invoice_route(tmp_path: Path):
    from plugins.accounting import register

    registered: list[dict] = []
    skills: list[dict] = []
    context = SimpleNamespace(
        state=SimpleNamespace(data_dir=tmp_path),
        get_config=lambda _key, default=None: default,
        register_tool=lambda **kwargs: registered.append(kwargs),
        register_skill=lambda **kwargs: skills.append(kwargs),
    )

    register(context)

    assert registered == []
    assert any(skill["name"] == "skill-fin-discord-finance-workflow" for skill in skills)


def test_removed_legacy_accounting_tool_is_never_registered(tmp_path: Path):
    from plugins.accounting import register

    registered: list[dict] = []
    context = SimpleNamespace(
        state=SimpleNamespace(data_dir=tmp_path),
        get_config=lambda key, default=None: True if key == "legacy_tool_enabled" else default,
        register_tool=lambda **kwargs: registered.append(kwargs),
        register_skill=lambda **_kwargs: None,
    )

    register(context)

    assert registered == []
