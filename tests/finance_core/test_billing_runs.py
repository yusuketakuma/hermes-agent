"""Monthly schedule-to-invoice closing behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.finance_core.test_core import FakePdfRenderer


def _core(tmp_path: Path):
    from finance_core.core import FinanceCore

    return FinanceCore(tmp_path, renderer=FakePdfRenderer())


def _masters(core):
    issuer = core.create_issuer(legal_name="Billing Run Issuer")
    customer = core.create_customer(
        customer_kind="company",
        legal_name="Billing Run Customer",
        billing_profile={
            "billing_name": "Billing Run Customer",
            "billing_address": "Tokyo",
        },
    )
    contract = core.create_contract(
        customer_id=customer["id"],
        name="Billing run contract",
        effective_from="2026-01-01",
        default_template_id="invoice-standard-jp",
        created_by="owner-1",
    )
    rule = core.create_billing_rule(
        contract_id=contract["id"],
        rule_type="HOURLY",
        description="Visit support",
        unit="hour",
        unit_price="5500",
        tax_category="STANDARD_10",
        effective_from="2026-01-01",
        created_by="owner-1",
    )
    connection = core.create_calendar_connection(
        provider="google_calendar",
        calendar_id="visit-calendar",
        timezone="Asia/Tokyo",
        created_by="owner-1",
    )
    return issuer, customer, contract, rule, connection


def _event(event_id: str, starts_at: str, ends_at: str, *, status: str = "CONFIRMED"):
    return {
        "calendar_id": "visit-calendar",
        "external_event_id": event_id,
        "summary": "Visit",
        "starts_at": starts_at,
        "ends_at": ends_at,
        "timezone": "Asia/Tokyo",
        "status": status,
        "etag": f"etag-{event_id}",
    }


def test_billing_run_preview_classifies_monthly_schedule_coverage(tmp_path: Path):
    core = _core(tmp_path)
    _issuer, customer, contract, rule, connection = _masters(core)
    core.sync_calendar_events(
        connection["id"],
        events=[
            _event("eligible", "2026-08-08T13:00:00+09:00", "2026-08-08T20:00:00+09:00"),
            _event("unmapped", "2026-08-09T10:00:00+09:00", "2026-08-09T11:00:00+09:00"),
            _event("pending", "2026-08-10T10:00:00+09:00", "2026-08-10T11:00:00+09:00"),
            _event(
                "cancelled",
                "2026-08-11T10:00:00+09:00",
                "2026-08-11T11:00:00+09:00",
                status="CANCELLED",
            ),
        ],
        synced_by="owner-1",
    )
    schedules = {row["external_event_id"]: row for row in core.list_schedules()}
    for event_id in ("eligible", "pending"):
        core.link_schedule(
            schedules[event_id]["id"],
            customer_id=customer["id"],
            contract_id=contract["id"],
            billing_rule_id=rule["id"],
            linked_by="owner-1",
        )
    core.complete_schedule(
        schedules["eligible"]["id"],
        actual_starts_at="2026-08-08T13:00:00+09:00",
        actual_ends_at="2026-08-08T20:00:00+09:00",
        completed_by="owner-1",
    )
    work = core.work_from_schedule(schedules["eligible"]["id"], created_by="owner-1")
    core.approve_work_entry(work["id"], approved_by="owner-1")
    core.complete_schedule(
        schedules["pending"]["id"],
        actual_starts_at="2026-08-10T10:00:00+09:00",
        actual_ends_at="2026-08-10T11:00:00+09:00",
        completed_by="owner-1",
    )
    core.work_from_schedule(schedules["pending"]["id"], created_by="owner-1")

    preview = core.preview_billing_run(
        calendar_connection_id=connection["id"],
        billing_rule_id=rule["id"],
        service_period="2026-08",
    )

    assert preview["summary"] == {
        "eligible": 1,
        "unmapped": 1,
        "pending_completion": 0,
        "awaiting_approval": 1,
        "cancelled": 1,
        "already_billed": 0,
        "different_billing_rule": 0,
    }
    assert preview["schedule_ids"]["eligible"] == [schedules["eligible"]["id"]]
    assert preview["schedule_ids"]["unmapped"] == [schedules["unmapped"]["id"]]


def test_billing_run_finalization_snapshots_targets_and_draft_is_idempotent(tmp_path: Path):
    core = _core(tmp_path)
    issuer, customer, contract, rule, connection = _masters(core)
    core.sync_calendar_events(
        connection["id"],
        events=[_event("eligible", "2026-08-08T13:00:00+09:00", "2026-08-08T20:00:00+09:00")],
        synced_by="owner-1",
    )
    schedule = core.list_schedules()[0]
    core.link_schedule(
        schedule["id"],
        customer_id=customer["id"],
        contract_id=contract["id"],
        billing_rule_id=rule["id"],
        linked_by="owner-1",
    )
    core.complete_schedule(
        schedule["id"],
        actual_starts_at="2026-08-08T13:00:00+09:00",
        actual_ends_at="2026-08-08T20:00:00+09:00",
        completed_by="owner-1",
    )
    work = core.work_from_schedule(schedule["id"], created_by="owner-1")
    core.approve_work_entry(work["id"], approved_by="owner-1")
    preview = core.preview_billing_run(
        calendar_connection_id=connection["id"],
        billing_rule_id=rule["id"],
        service_period="2026-08",
    )

    run = core.finalize_billing_run(
        calendar_connection_id=connection["id"],
        billing_rule_id=rule["id"],
        service_period="2026-08",
        schedule_ids=preview["schedule_ids"]["eligible"],
        finalized_by="owner-1",
    )

    assert run["status"] == "FINALIZED"
    assert run["schedule_ids"] == [schedule["id"]]
    assert run["work_entry_ids"] == [work["id"]]
    assert run["billing_key"] == f"schedule-run:{connection['id']}:{rule['id']}:2026-08"

    draft = core.draft_from_billing_run(
        run["id"],
        issuer_id=issuer["id"],
        issue_date="2026-08-31",
        created_by="owner-1",
    )
    repeated = core.draft_from_billing_run(
        run["id"],
        issuer_id=issuer["id"],
        issue_date="2026-08-31",
        created_by="owner-1",
    )

    assert draft["id"] == repeated["id"]
    assert draft["lines"][0]["quantity"] == "7"
    assert core.get_billing_run(run["id"])["status"] == "DRAFT_CREATED"
    assert core.get_schedule(schedule["id"])["billable_status"] == "BILLED"


def test_billing_run_cannot_finalize_non_eligible_schedule(tmp_path: Path):
    from finance_core.errors import FinanceConflictError

    core = _core(tmp_path)
    _issuer, customer, contract, rule, connection = _masters(core)
    core.sync_calendar_events(
        connection["id"],
        events=[_event("unmapped", "2026-08-09T10:00:00+09:00", "2026-08-09T11:00:00+09:00")],
        synced_by="owner-1",
    )
    schedule = core.list_schedules()[0]

    with pytest.raises(FinanceConflictError, match="eligible"):
        core.finalize_billing_run(
            calendar_connection_id=connection["id"],
            billing_rule_id=rule["id"],
            service_period="2026-08",
            schedule_ids=[schedule["id"]],
            finalized_by="owner-1",
        )


def test_work_from_schedule_uses_actual_service_date(tmp_path: Path):
    core = _core(tmp_path)
    _issuer, customer, contract, rule, _connection = _masters(core)
    schedule = core.create_schedule(
        customer_id=customer["id"],
        contract_id=contract["id"],
        billing_rule_id=rule["id"],
        title="Rescheduled visit",
        starts_at="2026-08-31T23:00:00+09:00",
        ends_at="2026-08-31T23:30:00+09:00",
        timezone="Asia/Tokyo",
        created_by="owner-1",
    )
    core.complete_schedule(
        schedule["id"],
        actual_starts_at="2026-09-01T10:00:00+09:00",
        actual_ends_at="2026-09-01T11:00:00+09:00",
        completed_by="owner-1",
    )

    work = core.work_from_schedule(schedule["id"], created_by="owner-1")

    assert work["service_date"] == "2026-09-01"
    assert work["service_period"] == "2026-09"
