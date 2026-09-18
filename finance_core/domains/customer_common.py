"""Shared validation and normalization helpers for customer domain services."""

from __future__ import annotations

import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from finance_core.errors import FinanceValidationError


def text(value: Any, *, field: str, required: bool = False, limit: int = 1000) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise FinanceValidationError(f"{field} is required")
    if len(result) > limit:
        raise FinanceValidationError(f"{field} is too long")
    return result


def money_text(value: Decimal) -> str:
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def normalize(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in normalized if character.isalnum())


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_datetime(value: Any, *, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise FinanceValidationError(f"{field} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()
