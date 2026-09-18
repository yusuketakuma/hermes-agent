"""Safe, deterministic selection of a Discord accounting channel."""

from __future__ import annotations

import re
from typing import Any, Iterable


_ALLOWED_TYPES = frozenset(
    {
        "channel",
        "text",
        "forum",
        "announcement",
        "news",
        "announcement_thread",
    }
)
_NAME_TERMS = {
    "accounting": 120,
    "finance": 120,
    "invoice": 120,
    "billing": 120,
    "receivable": 110,
    "経理": 140,
    "請求": 140,
    "入金": 110,
    "売掛": 110,
}
_TOPIC_TERMS = {
    "accounting": 60,
    "finance": 60,
    "invoice": 60,
    "billing": 60,
    "receivable": 55,
    "経理": 70,
    "請求": 70,
    "入金": 55,
    "売掛": 55,
}
_NEGATIVE_TERMS = {
    "general": 25,
    "random": 25,
    "off-topic": 25,
    "雑談": 25,
}


def _normalize(value: Any) -> str:
    value = str(value or "").strip().casefold()
    value = value.lstrip("#")
    return re.sub(r"\s+", "-", value)


def _is_accounting_candidate(channel: dict[str, Any]) -> bool:
    channel_type = _normalize(channel.get("type") or "channel")
    return bool(channel.get("id")) and channel_type in _ALLOWED_TYPES


def _hint_matches(channel: dict[str, Any], hint: str) -> bool:
    if not hint:
        return False
    normalized_hint = _normalize(hint)
    if normalized_hint == _normalize(channel.get("id")):
        return True
    name = _normalize(channel.get("name"))
    guild = _normalize(channel.get("guild"))
    return normalized_hint in {name, f"{guild}/{name}"}


def _score(channel: dict[str, Any], hint: str) -> tuple[int, bool, str]:
    name = _normalize(channel.get("name"))
    topic = _normalize(channel.get("topic"))
    guild = _normalize(channel.get("guild"))

    if _hint_matches(channel, hint):
        return 10_000, True, "matched the configured channel hint"

    name_score = sum(weight for term, weight in _NAME_TERMS.items() if term in name)
    topic_score = sum(weight for term, weight in _TOPIC_TERMS.items() if term in topic)
    negative_score = sum(weight for term, weight in _NEGATIVE_TERMS.items() if term in name)
    score = name_score + topic_score - negative_score
    purpose_match = score > 0
    if purpose_match:
        reason = "matched accounting-related channel name/topic"
    else:
        reason = "no accounting purpose match"
    # Keep the sort deterministic when several channels have the same score.
    if guild:
        score += min(len(guild), 20) / 100
    return int(score), purpose_match, reason


def select_accounting_channel(
    channels: Iterable[dict[str, Any]],
    *,
    hint: str | None = None,
) -> dict[str, Any] | None:
    """Return the best purpose-matched Discord channel.

    Voice, stage, category, and direct-message entries are excluded. A
    configured ID/name hint may select a channel even when its name does not
    contain an accounting keyword. Without a purpose match this returns
    ``None`` instead of risking financial data in a general chat.
    """

    hint = str(hint or "").strip()
    candidates: list[tuple[int, bool, str, dict[str, Any]]] = []
    seen_ids: set[str] = set()
    for raw in channels:
        if not isinstance(raw, dict) or not _is_accounting_candidate(raw):
            continue
        channel_id = str(raw["id"])
        if channel_id in seen_ids:
            continue
        seen_ids.add(channel_id)
        score, purpose_match, reason = _score(raw, hint)
        candidates.append((score, purpose_match, reason, raw))

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: (
            -item[0],
            _normalize(item[3].get("name")),
            _normalize(item[3].get("guild")),
            str(item[3].get("id")),
        )
    )
    score, purpose_match, reason, raw = candidates[0]
    if not purpose_match:
        return None

    selected = {
        "id": str(raw["id"]),
        "name": str(raw.get("name") or raw["id"]),
        "type": str(raw.get("type") or "channel"),
        "selection_score": score,
        "selection_reason": reason,
    }
    if raw.get("guild"):
        selected["guild"] = str(raw["guild"])
    if raw.get("topic"):
        selected["topic"] = str(raw["topic"])
    return selected
