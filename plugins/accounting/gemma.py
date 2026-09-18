"""Optional, local-model suggestions for the accounting plugin.

This module is deliberately an edge adapter.  It accepts only allow-listed
Finance Core projections, returns advisory text, and never changes an
invoice, schedule, payment, or delivery record.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from typing import Any


_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_NUMBER_RE = re.compile(r"^-?\d{1,18}(?:\.\d{1,4})?$")
_INVOICE_NUMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

_SUMMARY_FIELDS = (
    "eligible",
    "unmapped",
    "pending_completion",
    "awaiting_approval",
    "cancelled",
    "already_billed",
    "different_billing_rule",
)

_BILLING_RUN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "text": {"type": "string", "maxLength": 1600},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["text", "confidence"],
}

_REMINDER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "subject": {"type": "string", "maxLength": 200},
        "body": {"type": "string", "maxLength": 2400},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["subject", "body", "confidence"],
}

_SYSTEM_PROMPT = """あなたは請求業務の補助提案器です。
入力JSONに含まれる事実だけを使い、推測で顧客情報・金額・税額・日付・状態を補わないでください。
金額計算、税計算、実働時間の確定、請求書番号の採番、発行、送付、入金消込は行いません。
出力は指定されたJSONだけにし、提案は必ず人が確認する前提です。
"""


class GemmaSuggestionProvider:
    """Run bounded structured suggestions through a host-owned LLM facade."""

    def __init__(
        self,
        complete_structured: Callable[..., Any],
        *,
        provider: str = "custom",
        model: str = "gemma4:12b-mlx",
        timeout_seconds: float = 8,
    ) -> None:
        if not callable(complete_structured):
            raise TypeError("complete_structured must be callable")
        if not str(provider).strip() or not str(model).strip():
            raise ValueError("provider and model are required")
        if str(provider).strip().lower() not in {"custom", "ollama", "local"}:
            raise ValueError("Gemma suggestions require a local custom/Ollama provider")
        if "gemma4" not in str(model).strip().lower():
            raise ValueError("Gemma suggestions require a Gemma4 model")
        if not 1 <= float(timeout_seconds) <= 15:
            raise ValueError("timeout_seconds must be between 1 and 15")
        self._complete_structured = complete_structured
        self.provider = str(provider).strip()
        self.model = str(model).strip()
        self.timeout_seconds = float(timeout_seconds)

    def explain_billing_run(self, preview: Mapping[str, Any]) -> dict[str, Any]:
        """Explain billing-run preview categories without changing the run."""

        try:
            payload = _sanitize_billing_run_preview(preview)
        except ValueError:
            return {"success": False, "skipped": True, "reason": "invalid_input"}

        return self._complete(
            kind="billing_run_explanation",
            payload=payload,
            schema=_BILLING_RUN_SCHEMA,
            instructions=(
                "Billing Runの確認事項を日本語で簡潔に説明してください。"
                "eligible以外の件数がある場合は、Finance Coreで確認すべき項目として説明してください。"
                "件数を足し引きしたり、請求可否を決定したりしないでください。"
                "textとconfidenceを返してください。"
            ),
            normalize=_normalize_billing_run_output,
            evidence=_billing_run_evidence(payload),
        )

    def draft_invoice_reminder(self, invoice_summary: Mapping[str, Any]) -> dict[str, Any]:
        """Draft reminder prose from public invoice facts; never send it."""

        try:
            payload = _sanitize_invoice_summary(invoice_summary)
        except ValueError:
            return {"success": False, "skipped": True, "reason": "invalid_input"}

        return self._complete(
            kind="invoice_reminder_draft",
            payload=payload,
            schema=_REMINDER_SCHEMA,
            instructions=(
                "請求書の支払確認メールの下書きを日本語で作成してください。"
                "入力にない宛名、連絡先、金額、日付は書かず、入力された値は変更しないでください。"
                "送信済みと表現せず、subjectとbodyとconfidenceを返してください。"
            ),
            normalize=_normalize_reminder_output,
            evidence=_invoice_evidence(payload),
        )

    def _complete(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        instructions: str,
        normalize: Callable[[Mapping[str, Any]], dict[str, Any] | None],
        evidence: list[str],
    ) -> dict[str, Any]:
        try:
            response = self._complete_structured(
                instructions=instructions,
                input=[
                    {
                        "type": "text",
                        "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    }
                ],
                json_schema=schema,
                json_mode=True,
                schema_name=kind,
                system_prompt=_SYSTEM_PROMPT,
                provider=self.provider,
                model=self.model,
                temperature=0.1,
                max_tokens=600,
                timeout=self.timeout_seconds,
                purpose=f"accounting.{kind}",
            )
        except Exception:
            # Suggestions are optional.  Do not surface provider details or
            # raw prompts to the agent when a local model is unavailable.
            return {"success": False, "skipped": True, "reason": "unavailable"}

        parsed = response.get("parsed") if isinstance(response, Mapping) else getattr(response, "parsed", None)
        if not isinstance(parsed, Mapping):
            return {"success": False, "skipped": True, "reason": "invalid_output"}
        normalized = normalize(parsed)
        if normalized is None:
            return {"success": False, "skipped": True, "reason": "invalid_output"}

        return {
            "success": True,
            "suggestion": {
                "kind": kind,
                "value": normalized,
                "confidence": normalized.pop("confidence"),
                "evidence": evidence,
                "needs_review": [
                    "AI生成の提案であり、人による確認が必要です。",
                    "Finance Coreのデータや状態は変更されていません。",
                ],
            },
            "provider": self.provider,
            "model": self.model,
        }


def _sanitize_billing_run_preview(preview: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(preview, Mapping):
        raise ValueError("billing_run_preview must be an object")
    period = preview.get("service_period")
    if not isinstance(period, str) or not _PERIOD_RE.fullmatch(period.strip()):
        raise ValueError("service_period must use YYYY-MM")
    raw_summary = preview.get("summary")
    if not isinstance(raw_summary, Mapping):
        raise ValueError("summary must be an object")
    summary: dict[str, int] = {}
    for field in _SUMMARY_FIELDS:
        value = raw_summary.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100_000:
            raise ValueError(f"summary.{field} must be a bounded integer")
        summary[field] = value
    return {"service_period": period.strip(), "summary": summary}


def _sanitize_invoice_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(summary, Mapping):
        raise ValueError("invoice_summary must be an object")
    payload: dict[str, Any] = {}

    invoice_number = summary.get("invoice_number")
    if invoice_number is not None:
        if not isinstance(invoice_number, str) or not _INVOICE_NUMBER_RE.fullmatch(invoice_number.strip()):
            raise ValueError("invoice_number is invalid")
        payload["invoice_number"] = invoice_number.strip()

    for field in ("issue_date", "due_date", "today"):
        value = summary.get(field)
        if value is not None:
            if not isinstance(value, str) or not _DATE_RE.fullmatch(value.strip()):
                raise ValueError(f"{field} must use YYYY-MM-DD")
            payload[field] = value.strip()

    currency = summary.get("currency")
    if currency is not None:
        if not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency.strip().upper()):
            raise ValueError("currency is invalid")
        payload["currency"] = currency.strip().upper()

    for field in ("outstanding", "amount_due"):
        value = summary.get(field)
        if value is not None:
            text = str(value).strip()
            if not _NUMBER_RE.fullmatch(text):
                raise ValueError(f"{field} is invalid")
            payload[field] = text

    settlement_status = summary.get("settlement_status")
    if settlement_status is not None:
        allowed = {"UNPAID", "PARTIALLY_PAID", "PAID", "OVERPAID"}
        if not isinstance(settlement_status, str) or settlement_status.strip().upper() not in allowed:
            raise ValueError("settlement_status is invalid")
        payload["settlement_status"] = settlement_status.strip().upper()

    if "days_overdue" in summary:
        days_overdue = summary["days_overdue"]
        if isinstance(days_overdue, bool) or not isinstance(days_overdue, int) or not 0 <= days_overdue <= 10_000:
            raise ValueError("days_overdue must be a bounded integer")
        payload["days_overdue"] = days_overdue

    if not payload:
        raise ValueError("invoice_summary has no supported facts")
    return payload


def _normalize_text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = "".join(char for char in value.strip() if char in "\n\t" or ord(char) >= 32)
    if not text or len(text) > limit:
        return None
    return text


def _normalize_confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    confidence = float(value)
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        return None
    return confidence


def _normalize_billing_run_output(parsed: Mapping[str, Any]) -> dict[str, Any] | None:
    text = _normalize_text(parsed.get("text"), limit=1600)
    confidence = _normalize_confidence(parsed.get("confidence"))
    if text is None or confidence is None:
        return None
    return {"text": text, "confidence": confidence}


def _normalize_reminder_output(parsed: Mapping[str, Any]) -> dict[str, Any] | None:
    subject = _normalize_text(parsed.get("subject"), limit=200)
    body = _normalize_text(parsed.get("body"), limit=2400)
    confidence = _normalize_confidence(parsed.get("confidence"))
    if subject is None or body is None or confidence is None:
        return None
    return {"subject": subject, "body": body, "confidence": confidence}


def _billing_run_evidence(payload: Mapping[str, Any]) -> list[str]:
    summary = payload["summary"]
    return [
        f"service_period={payload['service_period']}",
        *(f"{field}={summary[field]}" for field in _SUMMARY_FIELDS if summary[field]),
    ]


def _invoice_evidence(payload: Mapping[str, Any]) -> list[str]:
    return [
        f"{field}={payload[field]}"
        for field in ("invoice_number", "due_date", "settlement_status", "days_overdue")
        if field in payload
    ]


__all__ = ["GemmaSuggestionProvider"]
