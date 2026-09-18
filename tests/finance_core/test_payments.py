"""Payment and allocation behavior for Finance Core."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.finance_core.test_core import FakePdfRenderer, _line


def _core(tmp_path: Path):
    from finance_core.core import FinanceCore

    return FinanceCore(tmp_path, renderer=FakePdfRenderer())


def _masters(core):
    issuer = core.create_issuer(
        legal_name="Example LLC",
        qualified_invoice_issuer=True,
        registration_number="T1234567890123",
    )
    customer = core.create_customer(
        customer_kind="company",
        legal_name="Acme Co.",
        billing_profile={"billing_name": "Acme Co.", "billing_address": "Tokyo"},
    )
    return issuer, customer


def _issued(core, issuer, customer, price: str, period: str):
    draft = core.draft_create(
        issuer_id=issuer["id"],
        customer_id=customer["id"],
        template_id="invoice-standard-jp",
        template_version="1.0.0",
        issue_date=f"{period}-01",
        service_period=period,
        due_date=f"{period}-28",
        currency="JPY",
        lines=[_line(unit_price=price)],
    )
    core.approve_invoice(draft["id"], approved_by="owner-1")
    return core.issue_invoice(draft["id"], issued_by="owner-1")


def test_payment_csv_preview_requires_explicit_apply_and_keeps_pii_in_core(tmp_path: Path):
    core = _core(tmp_path)
    csv_text = (
        "received_date,value_date,amount,currency,bank_transaction_id,payer_name_raw,remittance_information\n"
        "2026-08-20,2026-08-20,10000,JPY,BANK-001,Acme Co.,INV-202608-0001\n"
    )

    preview = core.import_payment_csv(csv_text=csv_text)

    assert preview["success"] is True
    assert preview["dry_run"] is True
    assert preview["rows"][0]["status"] == "ready"
    assert "payer_name_raw" not in preview["rows"][0]
    assert core.unallocated_payments() == []

    unauthorized = core.import_payment_csv(csv_text=csv_text, apply=True)
    assert unauthorized["success"] is False
    assert core.unallocated_payments() == []

    applied = core.import_payment_csv(csv_text=csv_text, apply=True, imported_by="owner-1")
    assert applied["success"] is True
    assert applied["applied_count"] == 1
    assert core.unallocated_payments()[0]["bank_transaction_id"] == "BANK-001"
    assert "Acme Co." not in str(core.unallocated_payments())
    assert "INV-202608-0001" not in str(core.unallocated_payments())

    repeated = core.import_payment_csv(csv_text=csv_text, apply=True, imported_by="owner-1")
    assert repeated["success"] is False
    assert "already exists" in repeated["errors"][0]["error"]


def test_payment_match_and_allocation_support_partial_multi_invoice_and_overpayment(tmp_path: Path):
    core = _core(tmp_path)
    issuer, customer = _masters(core)
    first = _issued(core, issuer, customer, "10000", "2026-08")
    second = _issued(core, issuer, customer, "5000", "2026-09")
    imported = core.import_payment_csv(
        csv_text=(
            "received_date,value_date,amount,currency,bank_transaction_id,payer_name_raw,remittance_information\n"
            f"2026-09-10,2026-09-10,17000,JPY,BANK-002,Acme Co.,{first['invoice_number']} {second['invoice_number']}\n"
        ),
        apply=True,
        imported_by="owner-1",
    )
    payment_id = imported["payments"][0]["id"]

    matches = core.match_payment(payment_id)

    assert [match["invoice_number"] for match in matches] == [
        first["invoice_number"],
        second["invoice_number"],
    ]
    allocation = core.allocate_payment(
        payment_id,
        allocations=[
            {"invoice_id": first["id"], "amount": "11000"},
            {"invoice_id": second["id"], "amount": "6000"},
        ],
        approved_by="owner-1",
    )

    assert allocation["payment_status"] == "ALLOCATED"
    assert core.get_invoice(first["id"])["settlement_status"] == "PAID"
    assert core.get_invoice(second["id"])["settlement_status"] == "OVERPAID"
    assert core.check_payment(second["id"])["outstanding"] == "0"
    assert core.check_payment(second["id"])["overpaid"] == "500"


def test_allocation_is_atomic_when_one_row_is_invalid(tmp_path: Path):
    from finance_core.errors import FinanceConflictError

    core = _core(tmp_path)
    issuer, customer = _masters(core)
    first = _issued(core, issuer, customer, "10000", "2026-08")
    second = _issued(core, issuer, customer, "5000", "2026-09")
    imported = core.import_payment_csv(
        csv_text=(
            "received_date,value_date,amount,currency,bank_transaction_id,payer_name_raw,remittance_information\n"
            f"2026-09-10,2026-09-10,10000,JPY,BANK-003,Acme Co.,{first['invoice_number']}\n"
        ),
        apply=True,
        imported_by="owner-1",
    )

    with pytest.raises(FinanceConflictError):
        core.allocate_payment(
            imported["payments"][0]["id"],
            allocations=[
                {"invoice_id": first["id"], "amount": "10000"},
                {"invoice_id": second["id"], "amount": "100"},
            ],
            approved_by="owner-1",
        )

    assert core.get_invoice(first["id"])["settlement_status"] == "UNPAID"
    assert core.get_invoice(second["id"])["settlement_status"] == "UNPAID"


def test_empty_payment_csv_is_rejected_without_creating_a_batch(tmp_path: Path):
    core = _core(tmp_path)

    result = core.import_payment_csv(
        csv_text="received_date,value_date,amount,currency,bank_transaction_id\n"
    )

    assert result["success"] is False
    assert core.unallocated_payments() == []


def test_same_payment_cannot_allocate_the_same_invoice_twice(tmp_path: Path):
    from finance_core.errors import FinanceConflictError

    core = _core(tmp_path)
    issuer, customer = _masters(core)
    invoice = _issued(core, issuer, customer, "10000", "2026-08")
    imported = core.import_payment_csv(
        csv_text=(
            "received_date,value_date,amount,currency,bank_transaction_id,payer_name_raw,remittance_information\n"
            f"2026-09-10,2026-09-10,12000,JPY,BANK-004,Acme Co.,{invoice['invoice_number']}\n"
        ),
        apply=True,
        imported_by="owner-1",
    )
    payment_id = imported["payments"][0]["id"]
    core.allocate_payment(
        payment_id,
        allocations=[{"invoice_id": invoice["id"], "amount": "10000"}],
        approved_by="owner-1",
    )

    with pytest.raises(FinanceConflictError):
        core.allocate_payment(
            payment_id,
            allocations=[{"invoice_id": invoice["id"], "amount": "1000"}],
            approved_by="owner-1",
        )

    assert len(core.store.payment_allocations(payment_id)) == 1
