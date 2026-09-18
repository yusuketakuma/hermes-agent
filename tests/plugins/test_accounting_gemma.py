"""Tests for the optional, advisory-only Gemma accounting suggestions."""

from __future__ import annotations

import json
from pathlib import Path


class _StructuredResult:
    def __init__(self, parsed):
        self.parsed = parsed


def test_default_model_uses_gemma4_12b_mlx():
    from plugins.accounting.gemma import GemmaSuggestionProvider

    provider = GemmaSuggestionProvider(lambda **_kwargs: None)

    assert provider.model == "gemma4:12b-mlx"


def test_billing_run_explanation_redacts_unknown_fields_and_requires_review():
    from plugins.accounting.gemma import GemmaSuggestionProvider

    calls: list[dict] = []

    def complete_structured(**kwargs):
        calls.append(kwargs)
        return _StructuredResult(
            {
                "text": "未完了と承認待ちの予定を確認してください。",
                "confidence": 0.82,
            }
        )

    provider = GemmaSuggestionProvider(
        complete_structured,
        provider="custom",
        model="gemma4:12b-mlx",
        timeout_seconds=8,
    )
    result = provider.explain_billing_run(
        {
            "service_period": "2026-08",
            "summary": {
                "eligible": 2,
                "pending_completion": 1,
                "awaiting_approval": 1,
                "unmapped": 0,
            },
            "customer_name": "Example Customer",
            "address": "must-not-be-sent",
        }
    )

    assert result["success"] is True
    assert result["suggestion"]["kind"] == "billing_run_explanation"
    assert result["suggestion"]["needs_review"]
    assert "Example Customer" not in json.dumps(calls, ensure_ascii=False)
    payload = json.loads(calls[0]["input"][0]["text"])
    assert payload == {
        "service_period": "2026-08",
        "summary": {
            "eligible": 2,
            "unmapped": 0,
            "pending_completion": 1,
            "awaiting_approval": 1,
            "cancelled": 0,
            "already_billed": 0,
            "different_billing_rule": 0,
        },
    }
    assert calls[0]["provider"] == "custom"
    assert calls[0]["model"] == "gemma4:12b-mlx"
    assert calls[0]["timeout"] == 8
    assert calls[0]["json_mode"] is True


def test_invoice_reminder_draft_accepts_public_facts_but_does_not_send_or_write():
    from plugins.accounting.gemma import GemmaSuggestionProvider

    calls: list[dict] = []

    def complete_structured(**kwargs):
        calls.append(kwargs)
        return _StructuredResult(
            {
                "subject": "請求内容のご確認のお願い",
                "body": "支払期限をご確認いただけますと幸いです。",
                "confidence": 0.74,
            }
        )

    provider = GemmaSuggestionProvider(complete_structured)
    result = provider.draft_invoice_reminder(
        {
            "invoice_number": "INV-202608-0001",
            "due_date": "2026-08-31",
            "currency": "JPY",
            "outstanding": "12000",
            "settlement_status": "UNPAID",
            "customer": {"name": "Example Customer", "email": "secret@example.test"},
        }
    )

    assert result["success"] is True
    assert result["suggestion"]["kind"] == "invoice_reminder_draft"
    assert result["suggestion"]["value"]["subject"] == "請求内容のご確認のお願い"
    assert result["suggestion"]["needs_review"]
    assert "secret@example.test" not in json.dumps(calls, ensure_ascii=False)
    payload = json.loads(calls[0]["input"][0]["text"])
    assert payload == {
        "invoice_number": "INV-202608-0001",
        "due_date": "2026-08-31",
        "currency": "JPY",
        "outstanding": "12000",
        "settlement_status": "UNPAID",
    }


def test_invalid_or_failed_model_output_is_a_non_mutating_skip():
    from plugins.accounting.gemma import GemmaSuggestionProvider

    def invalid_output(**_kwargs):
        return _StructuredResult({"text": "missing required confidence"})

    invalid = GemmaSuggestionProvider(invalid_output).explain_billing_run(
        {"service_period": "2026-08", "summary": {"eligible": 1}}
    )
    assert invalid == {"success": False, "skipped": True, "reason": "invalid_output"}

    def unavailable(**_kwargs):
        raise TimeoutError("synthetic timeout")

    unavailable_result = GemmaSuggestionProvider(unavailable).explain_billing_run(
        {"service_period": "2026-08", "summary": {"eligible": 1}}
    )
    assert unavailable_result == {
        "success": False,
        "skipped": True,
        "reason": "unavailable",
    }


def test_disabled_gemma_action_does_not_change_accounting_store(tmp_path: Path):
    from plugins.accounting.service import AccountingService
    from plugins.accounting.store import AccountingStore

    service = AccountingService(store=AccountingStore(tmp_path / "accounting.sqlite3"))
    result = service.handle(
        "suggest_billing_run_explanation",
        billing_run_preview={"service_period": "2026-08", "summary": {"eligible": 1}},
    )

    assert result == {"success": False, "skipped": True, "reason": "disabled"}
    assert service.list_invoices() == []
