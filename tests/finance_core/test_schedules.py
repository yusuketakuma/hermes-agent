"""Schedule lifecycle, calendar sync, and billing-source behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.finance_core.test_core import FakePdfRenderer


def _core(tmp_path: Path):
    from finance_core.core import FinanceCore

    return FinanceCore(tmp_path, renderer=FakePdfRenderer())


def _masters(core):
    issuer = core.create_issuer(legal_name="Schedule Issuer")
    customer = core.create_customer(
        customer_kind="company",
        legal_name="Schedule Customer",
        billing_profile={
            "billing_name": "Schedule Customer",
            "billing_address": "Tokyo",
        },
    )
    contract = core.create_contract(
        customer_id=customer["id"],
        name="Schedule contract",
        effective_from="2026-01-01",
        default_template_id="invoice-standard-jp",
        created_by="owner-1",
    )
    rule = core.create_billing_rule(
        contract_id=contract["id"],
        rule_type="HOURLY",
        description="Scheduled support",
        unit="hour",
        unit_price="5500",
        tax_category="STANDARD_10",
        effective_from="2026-01-01",
        created_by="owner-1",
    )
    return issuer, customer, contract, rule


def test_schedule_service_imports_google_events_idempotently(tmp_path: Path):
    from finance_core.errors import FinanceConflictError

    core = _core(tmp_path)
    connection = core.create_calendar_connection(
        provider="google_calendar",
        calendar_id="primary",
        timezone="Asia/Tokyo",
        created_by="owner-1",
    )
    assert connection["calendar_name"] == "訪問薬剤管理"
    event = {
        "calendar_id": "primary",
        "external_event_id": "event-1",
        "summary": "Support session",
        "starts_at": "2026-08-20T10:00:00+09:00",
        "ends_at": "2026-08-20T12:00:00+09:00",
        "timezone": "Asia/Tokyo",
        "status": "CONFIRMED",
        "etag": "etag-1",
        "description": "Should not be retained in the public schedule",
        "attendees": ["customer@example.test"],
    }

    first = core.sync_calendar_events(
        connection["id"], events=[event], next_sync_token="sync-1", synced_by="owner-1"
    )
    second = core.sync_calendar_events(
        connection["id"], events=[event], next_sync_token="sync-1", synced_by="owner-1"
    )

    assert first["created_count"] == 1
    assert second["created_count"] == 0
    assert second["updated_count"] == 0
    schedules = core.list_schedules()
    assert len(schedules) == 1
    assert schedules[0]["external_event_id"] == "event-1"
    assert "description" not in schedules[0]
    assert "attendees" not in schedules[0]
    assert core.get_calendar_connection(connection["id"])["sync_token"] == "sync-1"
    with pytest.raises(FinanceConflictError, match="linked"):
        core.complete_schedule(
            schedules[0]["id"],
            actual_starts_at="2026-08-20T10:00:00+09:00",
            actual_ends_at="2026-08-20T11:00:00+09:00",
            completed_by="owner-1",
        )

    cancelled = {**event, "status": "CANCELLED", "etag": "etag-2"}
    core.sync_calendar_events(connection["id"], events=[cancelled], synced_by="owner-1")
    assert core.get_schedule(schedules[0]["id"])["status"] == "CANCELLED"


def test_completed_schedule_creates_one_pending_work_source_until_approval(tmp_path: Path):
    core = _core(tmp_path)
    _issuer, customer, contract, rule = _masters(core)
    schedule = core.create_schedule(
        customer_id=customer["id"],
        contract_id=contract["id"],
        billing_rule_id=rule["id"],
        title="Support session",
        starts_at="2026-08-20T10:00:00+09:00",
        ends_at="2026-08-20T12:00:00+09:00",
        timezone="Asia/Tokyo",
        planned_quantity="2",
        created_by="owner-1",
    )
    completed = core.complete_schedule(
        schedule["id"],
        actual_starts_at="2026-08-20T10:15:00+09:00",
        actual_ends_at="2026-08-20T11:45:00+09:00",
        completed_by="owner-1",
    )
    assert completed["status"] == "COMPLETED"
    assert completed["actual_quantity"] == "1.5"

    first = core.work_from_schedule(schedule["id"], created_by="owner-1")
    repeated = core.work_from_schedule(schedule["id"], created_by="owner-1")

    assert first["id"] == repeated["id"]
    assert first["source_id"] == f"schedule:{schedule['id']}"
    assert first["status"] == "PENDING_APPROVAL"
    assert first["quantity"] == "1.5"

    approved = core.approve_work_entry(first["id"], approved_by="owner-1")
    assert approved["status"] == "APPROVED"


def test_schedule_completion_derives_billable_hours_from_actual_times(tmp_path: Path):
    core = _core(tmp_path)
    _issuer, customer, contract, rule = _masters(core)
    schedule = core.create_schedule(
        customer_id=customer["id"],
        contract_id=contract["id"],
        billing_rule_id=rule["id"],
        title="Support session",
        starts_at="2026-08-20T10:00:00+09:00",
        ends_at="2026-08-20T12:00:00+09:00",
        timezone="Asia/Tokyo",
        planned_quantity="2",
        created_by="owner-1",
    )

    completed = core.complete_schedule(
        schedule["id"],
        actual_starts_at="2026-08-20T10:15:00+09:00",
        actual_ends_at="2026-08-20T11:45:00+09:00",
        completed_by="owner-1",
    )

    assert completed["actual_starts_at"] == "2026-08-20T10:15:00+09:00"
    assert completed["actual_ends_at"] == "2026-08-20T11:45:00+09:00"
    assert completed["actual_duration_seconds"] == "5400"
    assert completed["actual_quantity"] == "1.5"

    work = core.work_from_schedule(schedule["id"], created_by="owner-1")
    assert work["quantity"] == "1.5"
    assert work["unit"] == "hour"


def test_schedule_completion_rejects_non_hourly_billing_rules(tmp_path: Path):
    from finance_core.errors import FinanceConflictError

    core = _core(tmp_path)
    _issuer, customer, contract, _hourly_rule = _masters(core)
    visit_rule = core.create_billing_rule(
        contract_id=contract["id"],
        rule_type="PER_EVENT",
        description="Per-visit service",
        unit="visit",
        unit_price="3000",
        tax_category="STANDARD_10",
        effective_from="2026-01-01",
        created_by="owner-1",
    )
    schedule = core.create_schedule(
        customer_id=customer["id"],
        contract_id=contract["id"],
        billing_rule_id=visit_rule["id"],
        title="Visit",
        starts_at="2026-08-20T10:00:00+09:00",
        ends_at="2026-08-20T11:00:00+09:00",
        timezone="Asia/Tokyo",
        created_by="owner-1",
    )

    with pytest.raises(FinanceConflictError, match="HOURLY"):
        core.complete_schedule(
            schedule["id"],
            actual_starts_at="2026-08-20T10:00:00+09:00",
            actual_ends_at="2026-08-20T11:00:00+09:00",
            completed_by="owner-1",
        )


def test_only_named_invoice_calendar_can_be_connected_and_synced(tmp_path: Path):
    from finance_core.errors import FinanceConflictError, FinanceValidationError

    core = _core(tmp_path)
    with pytest.raises(FinanceValidationError, match="訪問薬剤管理"):
        core.create_calendar_connection(
            provider="google_calendar",
            calendar_id="other-calendar",
            calendar_name="別のカレンダー",
            timezone="Asia/Tokyo",
            created_by="owner-1",
        )

    connection = core.create_calendar_connection(
        provider="google_calendar",
        calendar_id="visit-calendar",
        calendar_name="訪問薬剤管理",
        timezone="Asia/Tokyo",
        created_by="owner-1",
    )
    with pytest.raises(FinanceConflictError, match="target calendar"):
        core.sync_calendar_events(
            connection["id"],
            events=[
                {
                    "calendar_id": "other-calendar",
                    "external_event_id": "event-other",
                    "summary": "Other calendar event",
                    "starts_at": "2026-08-20T10:00:00+09:00",
                    "ends_at": "2026-08-20T11:00:00+09:00",
                    "timezone": "Asia/Tokyo",
                    "status": "CONFIRMED",
                }
            ],
            synced_by="owner-1",
        )


def test_connection_can_resolve_the_named_calendar_without_accepting_a_primary_fallback(
    tmp_path: Path,
):
    from finance_core.core import FinanceCore

    class Resolver:
        def __init__(self):
            self.requested: list[str] = []

        def resolve_calendar_id(self, calendar_name: str) -> str:
            self.requested.append(calendar_name)
            return "visit-calendar"

    provider = Resolver()
    core = FinanceCore(tmp_path, renderer=FakePdfRenderer(), calendar_provider=provider)

    connection = core.create_calendar_connection(
        provider="google_calendar",
        timezone="Asia/Tokyo",
        created_by="owner-1",
    )

    assert connection["calendar_id"] == "visit-calendar"
    assert provider.requested == ["訪問薬剤管理"]


def test_named_calendar_schedule_work_can_generate_invoice_draft(tmp_path: Path):
    core = _core(tmp_path)
    issuer, customer, contract, rule = _masters(core)
    connection = core.create_calendar_connection(
        provider="google_calendar",
        calendar_id="visit-calendar",
        timezone="Asia/Tokyo",
        created_by="owner-1",
    )
    core.sync_calendar_events(
        connection["id"],
        events=[
            {
                "calendar_id": "visit-calendar",
                "external_event_id": "visit-001",
                "summary": "訪問実績",
                "starts_at": "2026-08-20T13:00:00+09:00",
                "ends_at": "2026-08-20T20:00:00+09:00",
                "timezone": "Asia/Tokyo",
                "status": "CONFIRMED",
                "etag": "visit-etag-1",
            }
        ],
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
        actual_starts_at="2026-08-20T13:00:00+09:00",
        actual_ends_at="2026-08-20T20:00:00+09:00",
        completed_by="owner-1",
    )
    work = core.work_from_schedule(schedule["id"], created_by="owner-1")
    core.approve_work_entry(work["id"], approved_by="owner-1")

    draft = core.draft_from_schedule_entries(
        issuer_id=issuer["id"],
        calendar_connection_id=connection["id"],
        billing_rule_id=rule["id"],
        service_period="2026-08",
        issue_date="2026-08-31",
        billing_key="visit-calendar:2026-08",
        created_by="owner-1",
    )

    assert draft["document_status"] == "DRAFT"
    assert draft["work_entry_ids"] == [work["id"]]
    assert draft["lines"][0]["quantity"] == "7"
    assert draft["lines"][0]["unit"] == "hour"
    assert core.get_schedule(schedule["id"])["billable_status"] == "BILLED"
