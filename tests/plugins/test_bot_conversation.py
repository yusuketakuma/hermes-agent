from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


_PLUGIN_PATH = Path(__file__).resolve().parents[3] / "plugins" / "bot-conversation" / "__init__.py"
_SPEC = importlib.util.spec_from_file_location("bot_conversation_test_plugin", _PLUGIN_PATH)
assert _SPEC and _SPEC.loader
bot_conversation = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bot_conversation)


def _settings() -> dict:
    return {
        "max_message_chars": 600,
        "max_posts_per_thread": 48,
        "max_posts_per_role": 12,
        "max_candidates_per_day": 3,
    }


def _source_receipt() -> dict[str, str]:
    return {
        "message_id": "100",
        "channel_id": "channel",
        "thread_id": "",
        "coordination_id": "mail-calendar-topic",
        "trusted_ref": "discord:message:100",
        "digest": "source-digest",
    }


def test_free_text_control_phrase_is_not_an_action():
    with pytest.raises(ValueError):
        bot_conversation._required_text({"summary": "approved"}, "summary", 240)


def test_redaction_failure_fails_closed(monkeypatch):
    def broken_redactor(_text, *, force):
        raise RuntimeError("redactor unavailable")

    monkeypatch.setattr("agent.redact.redact_sensitive_text", broken_redactor)
    with pytest.raises(RuntimeError, match="redaction is unavailable"):
        bot_conversation._required_text({"summary": "private"}, "summary", 240)


def test_sender_and_thread_caps_are_global_and_bounded(tmp_path):
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path):
        for _ in range(12):
            bot_conversation._reserve_message(_settings(), "channel", "thread", "topic", "cfo", "")
        with pytest.raises(ValueError, match="sender post cap"):
            bot_conversation._reserve_message(_settings(), "channel", "thread", "topic", "cfo", "")
        for role in ("ceo", "cro", "cso"):
            for _ in range(12):
                bot_conversation._reserve_message(_settings(), "channel", "thread", "topic", role, "")
        with pytest.raises(ValueError, match="thread post cap"):
            bot_conversation._reserve_message(_settings(), "channel", "thread", "topic", "coo", "")


def test_thread_cap_cannot_be_reset_by_changing_coordination_id(tmp_path):
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path):
        roles = ("cfo", "ceo", "cro", "cso", "cto", "cio", "coo", "cpo", "cmo", "cos")
        for index in range(48):
            role = roles[index % len(roles)]
            bot_conversation._reserve_message(
                _settings(), "channel", "thread", f"topic-{index}", role, ""
            )
        with pytest.raises(ValueError, match="thread post cap"):
            bot_conversation._reserve_message(
                _settings(), "channel", "thread", "new-topic", "ceo", ""
            )


def test_direct_channel_uses_fresh_coordination_roots(tmp_path):
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path), \
         patch.object(bot_conversation.time, "time", side_effect=[1000.0, 1601.0, 1601.0]):
        bot_conversation._reserve_message(
            _settings(), "channel", None, "old-topic", "cos", "m1"
        )
        bot_conversation._reserve_message(
            _settings(), "channel", None, "new-topic", "cos", "m2"
        )
        with pytest.raises(ValueError, match="expired"):
            bot_conversation._reserve_message(
                _settings(), "channel", None, "old-topic", "cro", "m3"
            )


def test_legacy_direct_root_is_reused_only_for_its_coordination(tmp_path):
    legacy_thread_key = bot_conversation._digest("channel", "channel")
    coordination_key = bot_conversation._digest("coordination", "channel", "old-topic")
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path), \
         patch.object(bot_conversation.time, "time", side_effect=[1001.0, 1002.0]):
        with bot_conversation._locked_ledger() as state:
            state.update({
                "threads": {legacy_thread_key: {
                    "total": 1, "by_role": {"cos": 1}, "last_at": 1000.0,
                    "conversation_key": "legacy-root",
                }},
                "thread_roots": {legacy_thread_key: "legacy-root"},
                "coordination_roots": {coordination_key: "legacy-root"},
                "conversations": {"legacy-root": {
                    "total": 1, "by_role": {"cos": 1}, "recipients": ["cto"],
                    "owner_role": "cos", "started_at": 1000.0,
                    "expires_at": 2000.0, "last_at": 1000.0,
                }},
                "sent": {}, "candidates": {}, "days": {},
            })
        new_root, duplicate = bot_conversation._reserve_message(
            _settings(), "channel", None, "new-topic", "cos", "new-message"
        )
        old_root, duplicate = bot_conversation._reserve_message(
            _settings(), "channel", None, "old-topic", "cro", "old-reply"
        )
    assert new_root != old_root
    assert duplicate is False


def test_idempotent_send_is_deduplicated(tmp_path):
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path):
        first = bot_conversation._reserve_message(
            _settings(), "channel", "thread", "topic", "cfo", "same-message"
        )
        second = bot_conversation._reserve_message(
            _settings(), "channel", "thread", "topic", "cfo", "same-message"
        )
    assert first == (second[0], False)
    assert second[1] is True


def test_idempotency_key_rejects_different_payload(tmp_path):
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path):
        bot_conversation._reserve_message(
            _settings(), "channel", "thread", "topic", "cfo", "same-message",
            payload_digest="digest-a",
        )
        with pytest.raises(ValueError, match="different bot message content"):
            bot_conversation._reserve_message(
                _settings(), "channel", "thread", "topic", "cfo", "same-message",
                payload_digest="digest-b",
            )


def test_conversation_root_keeps_budget_across_threads_and_expires(tmp_path):
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path), \
         patch.object(bot_conversation.time, "time", side_effect=[1000.0, 1001.0, 1601.0]):
        conversation_key, _ = bot_conversation._reserve_message(
            _settings(), "channel", "thread-a", "topic", "cos", "m1"
        )
        bot_conversation._record_message_delivery(conversation_key, "cos", "m1", "sent")
        bot_conversation._reserve_message(
            _settings(), "channel", "thread-b", "topic", "cfo", "m2"
        )
        with pytest.raises(ValueError, match="expired"):
            bot_conversation._reserve_message(
                _settings(), "channel", "thread-b", "topic", "cro", "m3"
            )


def test_meaningful_messages_renew_idle_lease_but_not_hard_limit(tmp_path):
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path), \
         patch.object(bot_conversation.time, "time", side_effect=[1000.0, 1599.0, 4600.0]):
        root, _ = bot_conversation._reserve_message(
            _settings(), "channel", "thread", "topic", "cos", "", recipient_role="cto"
        )
        bot_conversation._reserve_message(
            _settings(), "channel", "thread", "topic", "cto", "", recipient_role="cos"
        )
        with bot_conversation._locked_ledger() as state:
            conversation = state["conversations"][root]
            assert conversation["hard_expires_at"] == 4600.0
            assert conversation["expires_at"] == 2199.0
        with pytest.raises(ValueError, match="expired"):
            bot_conversation._reserve_message(
                _settings(), "channel", "thread", "topic", "cro", "", recipient_role="cos"
            )


def test_unknown_delivery_stops_new_messages_in_the_same_topic(tmp_path):
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path):
        root, duplicate = bot_conversation._reserve_message(
            _settings(), "channel", "thread", "topic", "cos", "message-1",
            recipient_role="cto",
        )
        assert duplicate is False
        duplicate_root, duplicate = bot_conversation._reserve_message(
            _settings(), "channel", "thread", "topic", "cos", "message-1",
            recipient_role="cto",
        )
        assert duplicate_root == root
        assert duplicate is True
        with pytest.raises(RuntimeError, match="delivery is unknown"):
            bot_conversation._reserve_message(
                _settings(), "channel", "thread", "topic", "cro", "message-2",
                recipient_role="cos",
            )


def test_conversation_root_rejects_conflicting_thread_and_coordination_bindings(tmp_path):
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path), \
         patch.object(bot_conversation.time, "time", side_effect=[1000.0, 1001.0, 1601.0, 1601.0]):
        bot_conversation._reserve_message(
            _settings(), "channel", "thread-a", "topic-a", "cos", "m1"
        )
        bot_conversation._reserve_message(
            _settings(), "channel", "thread-b", "topic-b", "cfo", "m2"
        )
        with pytest.raises(RuntimeError, match="disagree"):
            bot_conversation._reserve_message(
                _settings(), "channel", "thread-b", "topic-a", "cro", "m3"
            )
        with pytest.raises(ValueError, match="expired"):
            bot_conversation._reserve_message(
                _settings(), "channel", "thread-c", "topic-a", "cro", "m4"
            )


def test_pruned_thread_cache_keeps_root_caps_and_ttl(tmp_path):
    roles = ("cos", "cfo", "ceo", "cro", "cso", "cto", "cio", "coo", "cpo", "cmo")
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path):
        for index in range(48):
            role = roles[index % len(roles)]
            bot_conversation._reserve_message(
                _settings(), "channel", "anchor", "anchor-topic", role, ""
            )
        for index in range(bot_conversation._MAX_LEDGER_THREADS + 1):
            bot_conversation._reserve_message(
                _settings(), "channel", f"other-{index}", f"topic-{index}", "cos", ""
            )
        with pytest.raises(ValueError, match="thread post cap"):
            bot_conversation._reserve_message(
                _settings(), "channel", "anchor", "anchor-topic-renamed", "cos", ""
            )


def test_message_idempotency_is_bound_to_conversation_root_not_physical_thread(tmp_path):
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path):
        first = bot_conversation._reserve_message(
            _settings(), "channel", "thread-a", "topic", "cos", "same-message",
            payload_digest="digest-a",
        )
        second = bot_conversation._reserve_message(
            _settings(), "channel", "thread-b", "topic", "cos", "same-message",
            payload_digest="digest-a",
        )
    assert second == (first[0], True)


def test_active_send_receipts_are_not_evicted_at_capacity(tmp_path):
    root = "active-root"
    thread_key = bot_conversation._thread_key("channel", "thread")
    coordination_key = bot_conversation._digest("coordination", "channel", "topic")
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path), \
         patch.object(bot_conversation.time, "time", return_value=1000.0):
        with bot_conversation._locked_ledger() as state:
            state.update({
                "threads": {thread_key: {"conversation_key": root, "last_at": 1000.0}},
                "thread_roots": {thread_key: root},
                "coordination_roots": {coordination_key: root},
                "conversations": {root: {
                    "total": 0, "by_role": {}, "recipients": [], "owner_role": "cos",
                    "started_at": 1000.0, "expires_at": 2000.0, "last_at": 1000.0,
                }},
                "sent": {
                    bot_conversation._sent_key(root, "cos", f"existing-{index}"): {
                        "created_at": 1000.0, "conversation": root, "delivery": "sent",
                    }
                    for index in range(bot_conversation._MAX_LEDGER_KEYS)
                },
                "candidates": {}, "days": {},
            })
        duplicate = bot_conversation._reserve_message(
            _settings(), "channel", "thread", "topic", "cos", "existing-0"
        )
        assert duplicate[1] is True
        with pytest.raises(RuntimeError, match="send receipt capacity"):
            bot_conversation._reserve_message(
                _settings(), "channel", "thread", "topic", "cos", "new-message"
            )


def test_candidate_duplicate_does_not_create_coordination_aliases(tmp_path):
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path):
        candidate_key, _, _ = bot_conversation._reserve_candidate(
            _settings(), "channel", "thread", "topic", "candidate-1", "digest"
        )
        bot_conversation._record_candidate_task(candidate_key, "task-1")
        for index in range(bot_conversation._MAX_LEDGER_COORDINATION_ROOTS + 20):
            result = bot_conversation._reserve_candidate(
                _settings(), "channel", "thread", f"alias-{index}", "candidate-1", "digest"
            )
            assert result[1] == "task-1"
        with bot_conversation._locked_ledger() as state:
            assert len(state["coordination_roots"]) == 1


def test_owner_replies_do_not_consume_recipient_fanout(tmp_path):
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path):
        conversation_key, _ = bot_conversation._reserve_message(
            _settings(), "channel", "thread", "topic", "cos", "m1", recipient_role="cto"
        )
        bot_conversation._record_message_delivery(conversation_key, "cos", "m1", "sent")
        conversation_key, _ = bot_conversation._reserve_message(
            _settings(), "channel", "thread", "topic", "cto", "m2", recipient_role="cos"
        )
        bot_conversation._record_message_delivery(conversation_key, "cto", "m2", "sent")
        for index, role in enumerate(("cro", "cfo", "cso", "ceo", "cio", "coo", "cpo", "cmo"), 3):
            conversation_key, _ = bot_conversation._reserve_message(
                _settings(), "channel", "thread", "topic", "cos", f"m{index}", recipient_role=role
            )
            bot_conversation._record_message_delivery(conversation_key, "cos", f"m{index}", "sent")
        with bot_conversation._locked_ledger() as state:
            assert set(state["conversations"][conversation_key]["recipients"]) == {
                "cto", "cro", "cfo", "cso", "ceo", "cio", "coo", "cpo", "cmo",
            }


def test_recipient_fanout_covers_all_other_roles(tmp_path):
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path):
        recipients = ("ceo", "cfo", "cio", "cmo", "coo", "cpo", "cro", "cso", "cto")
        for index, role in enumerate(recipients):
            bot_conversation._reserve_message(
                _settings(), "channel", "thread", "topic", "cos", "", recipient_role=role
            )
        with bot_conversation._locked_ledger() as state:
            root = next(iter(state["conversations"].values()))
            assert root["recipients"] == sorted(recipients)


def test_candidate_is_one_per_thread_and_daily_capped(tmp_path):
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path):
        first = bot_conversation._reserve_candidate(_settings(), "channel", "thread", "topic", "candidate-1")
        assert first[1] is None
        with pytest.raises(RuntimeError, match="outcome is unknown"):
            bot_conversation._reserve_candidate(
                _settings(), "channel", "thread", "topic", "candidate-1"
            )
        with pytest.raises(ValueError, match="one task candidate"):
            bot_conversation._reserve_candidate(_settings(), "channel", "thread", "topic", "candidate-2")


def test_candidate_idempotency_key_rejects_different_payload(tmp_path):
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path):
        bot_conversation._reserve_candidate(
            _settings(), "channel", "thread", "topic", "candidate-1", "digest-a"
        )
        with pytest.raises(ValueError, match="different candidate content"):
            bot_conversation._reserve_candidate(
                _settings(), "channel", "thread", "topic", "candidate-1", "digest-b"
            )


def test_candidate_idempotency_key_rejects_different_origin(tmp_path):
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path):
        bot_conversation._reserve_candidate(
            _settings(), "channel", "thread", "topic-a", "candidate-1", "same-content"
        )
        with pytest.raises(ValueError, match="different candidate content"):
            bot_conversation._reserve_candidate(
                _settings(), "channel", "thread-b", "topic-b", "candidate-1", "same-content"
            )


def test_bot_conversation_rejects_media_directives():
    with pytest.raises(ValueError, match="text-only"):
        bot_conversation._required_text({"summary": "MEDIA:/tmp/report.pdf"}, "summary", 240)


@pytest.mark.parametrize("message_type", ["CHAT", "INFO"])
def test_chat_and_info_attest_without_adding_turn_policy(monkeypatch, message_type):
    monkeypatch.setattr(
        bot_conversation, "_config", lambda: {"channel_id": "bot-lounge"}
    )
    event = SimpleNamespace(
        source=SimpleNamespace(
            platform=SimpleNamespace(value="discord"), chat_id="bot-lounge", is_bot=True,
        ),
        text=f"🧭 HERMES-BOT-CHAT v1\n🧭 種別={message_type} 送信元=cos 宛先=cto\n話題ID=topic\n要約=hello",
        metadata={"trace": "keep"},
    )
    assert bot_conversation._on_pre_gateway_dispatch(event=event) is None
    assert event.metadata == {"trace": "keep", "_bot_conversation_route_checked": True}
    assert "turn_policy" not in event.metadata


def test_attested_chat_uses_standard_nous_route():
    from gateway.run_turn import GatewayTurnMixin

    route = GatewayTurnMixin._resolve_turn_agent_config(
        SimpleNamespace(_service_tier=None),
        "hello",
        "upstage/solar-pro4:free",
        {
            "provider": "nous",
            "base_url": "https://inference-api.nousresearch.com/v1",
            "api_mode": "chat_completions",
        },
        turn_policy=None,
    )
    assert route["model"] == "upstage/solar-pro4:free"
    assert route["runtime"]["provider"] == "nous"
    assert route["runtime"]["base_url"] == "https://inference-api.nousresearch.com/v1"
    assert "allow_fallback" not in route


@pytest.mark.parametrize("chat_id", [bot_conversation._CHANNEL_ID, "custom-lounge"])
@pytest.mark.parametrize("message_type", ["CHAT", "TASK_CREATED"])
def test_config_failure_skips_bot_chat_event(monkeypatch, chat_id, message_type):
    event = SimpleNamespace(
        source=SimpleNamespace(
            platform=SimpleNamespace(value="discord"), chat_id=chat_id, is_bot=True,
        ),
        text=f"種別={message_type}\n要約=hello",
        metadata={},
    )
    monkeypatch.setattr(
        bot_conversation, "_config",
        lambda: (_ for _ in ()).throw(RuntimeError("config unavailable")),
    )
    assert bot_conversation._on_pre_gateway_dispatch(event=event) == {
        "action": "skip", "reason": "bot_chat_local_route_unavailable",
    }
    assert "_bot_conversation_route_checked" not in event.metadata


def test_send_formatted_requires_confirmed_success(monkeypatch):
    monkeypatch.setattr(
        "tools.send_message_tool.send_message_tool",
        lambda _args: json.dumps({"success": False, "error": "rejected"}),
    )
    assert bot_conversation._send_formatted("channel", None, "hello") == {
        "ok": False, "error": "rejected"
    }


def test_send_formatted_rejects_thread_targets(monkeypatch):
    monkeypatch.setattr(
        "tools.send_message_tool.send_message_tool", lambda _args: pytest.fail("thread send reached transport")
    )
    assert bot_conversation._send_formatted("channel", "thread", "hello") == {
        "ok": False, "error": "bot conversation threads are disabled"
    }


def test_unknown_send_is_not_retried_or_reported_as_duplicate_success(tmp_path):
    args = {
        "action": "send", "message_type": "CHAT", "recipient_role": "cto",
        "coordination_id": "topic", "summary": "hello", "idempotency_key": "message-1",
    }
    calls = []
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path), \
         patch.object(bot_conversation, "_session_target", return_value=("channel", None)), \
         patch.object(bot_conversation, "_live_bot_id", return_value="200"), \
         patch.object(bot_conversation, "_send_formatted", side_effect=lambda *_args: calls.append(1) or {
             "ok": False, "error": "timeout"
         }):
        first = bot_conversation._send_message(SimpleNamespace(profile_name="default"), args, _settings())
        second = bot_conversation._send_message(SimpleNamespace(profile_name="default"), args, _settings())

    assert first["delivery"] == "unknown"
    assert second == {
        "ok": False, "duplicate": True, "delivery": "unknown", "coordination_id": "topic",
        "error": "previous bot message delivery was not confirmed; refusing to resend",
    }
    assert calls == [1]


def test_duplicate_sent_message_does_not_resolve_recipient_again(tmp_path):
    args = {
        "action": "send", "message_type": "CHAT", "recipient_role": "cto",
        "coordination_id": "topic", "summary": "hello", "idempotency_key": "message-1",
    }
    live_id_calls = []
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path), \
         patch.object(bot_conversation, "_session_target", return_value=("channel", None)), \
         patch.object(
             bot_conversation, "_live_bot_id",
             side_effect=lambda role: live_id_calls.append(role) or "200",
         ), \
         patch.object(bot_conversation, "_send_formatted", return_value={"ok": True}):
        first = bot_conversation._send_message(SimpleNamespace(profile_name="default"), args, _settings())
        second = bot_conversation._send_message(SimpleNamespace(profile_name="default"), args, _settings())

    assert first["delivery"] == "sent"
    assert second["duplicate"] is True
    assert second["delivery"] == "sent"
    assert live_id_calls == ["cto"]


def test_persisted_reservation_is_unknown_and_not_retried(tmp_path):
    args = {
        "action": "send", "message_type": "CHAT", "recipient_role": "cto",
        "coordination_id": "topic", "summary": "hello", "idempotency_key": "message-1",
    }
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path), \
         patch.object(bot_conversation, "_session_target", return_value=("channel", None)), \
         patch.object(bot_conversation, "_live_bot_id", return_value="200"), \
         patch.object(bot_conversation, "_send_formatted") as send:
        message = bot_conversation._format_message(
            _settings(), "cto", "cos", "CHAT", "topic", args,
            language="en",
        )
        bot_conversation._reserve_message(
            _settings(), "channel", None, "topic", "cos", "message-1",
            recipient_role="cto", payload_digest=bot_conversation._digest(message),
        )
        result = bot_conversation._send_message(
            SimpleNamespace(profile_name="default"), args, _settings(),
        )

    assert result == {
        "ok": False, "duplicate": True, "delivery": "unknown", "coordination_id": "topic",
        "error": "previous bot message receipt is legacy and cannot be verified; refusing to resend",
    }
    send.assert_not_called()


def test_formatted_bot_message_uses_plain_japanese_and_one_recipient_mention():
    args = {
        "summary": "わかりやすい共有です。",
        "purpose": "疎通確認",
        "question": "確認できますか。",
        "answer": "確認できました。",
    }
    with patch.object(bot_conversation, "_live_bot_id", return_value="200"):
        message = bot_conversation._format_message(
            _settings(), "cto", "cos", "CHAT", "topic", args,
        )

    assert message.startswith("<@200> 🧭 HERMES-BOT-CHAT v1\n")
    assert "種別=CHAT 送信元=cos 宛先=cto" in message
    assert "話題ID=topic" in message
    assert "要約=わかりやすい共有です。" in message
    assert "目的=疎通確認" in message
    assert "質問=確認できますか。" in message
    assert "回答=確認できました。" in message
    assert message.count("<@200>") == 1
    assert "type=" not in message


def test_message_digest_stays_compatible_with_english_ledger_format():
    args = {
        "summary": "hello",
        "purpose": "handoff",
        "source_refs": ["synthetic:source:1"],
    }
    with patch.object(bot_conversation, "_live_bot_id", return_value="200"):
        old_format = bot_conversation._format_message(
            _settings(), "cto", "cos", "CHAT", "topic", args, language="en",
        )
        new_format = bot_conversation._format_message(
            _settings(), "cto", "cos", "CHAT", "topic", args,
        )
        digest = bot_conversation._message_payload_digest(
            _settings(), "cto", "cos", "CHAT", "topic", args,
        )

    assert old_format != new_format
    assert digest == bot_conversation._digest(old_format)


def test_legacy_message_digest_fixture_is_stable():
    args = {"summary": "hello"}
    with patch.object(bot_conversation, "_live_bot_id", return_value="200"):
        assert bot_conversation._message_payload_digest(
            _settings(), "cto", "cos", "CHAT", "topic", args,
        ) == "888ad5369b39f3cdfc1e61916c86e343"


def test_semantically_identical_message_dedupes_despite_argument_order(tmp_path):
    first = {
        "action": "send", "message_type": "CHAT", "recipient_role": "cto",
        "coordination_id": "topic", "summary": "hello", "idempotency_key": "message-1",
    }
    second = {
        "idempotency_key": "message-1", "summary": "hello", "coordination_id": "topic",
        "recipient_role": "cto", "message_type": "CHAT", "action": "send",
    }
    calls = []
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path), \
         patch.object(bot_conversation, "_session_target", return_value=("channel", None)), \
         patch.object(bot_conversation, "_live_bot_id", return_value="200"), \
         patch.object(bot_conversation, "_send_formatted", side_effect=lambda *_args: calls.append(1) or {
             "ok": True
         }):
        first_result = bot_conversation._send_message(
            SimpleNamespace(profile_name="default"), first, _settings()
        )
        second_result = bot_conversation._send_message(
            SimpleNamespace(profile_name="default"), second, _settings()
        )

    assert first_result["delivery"] == "sent"
    assert second_result["duplicate"] is True
    assert second_result["delivery"] == "sent"
    assert calls == [1]


def test_direct_channel_chat_sends_without_thread(tmp_path):
    args = {
        "action": "send", "message_type": "CHAT", "recipient_role": "cto",
        "coordination_id": "topic", "summary": "hello", "idempotency_key": "direct-1",
    }
    sent = []
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path), \
         patch.object(bot_conversation, "_session_target", return_value=("channel", None)), \
         patch.object(bot_conversation, "_live_bot_id", return_value="200"), \
         patch.object(
             bot_conversation, "_send_formatted",
             side_effect=lambda channel, thread, _message: sent.append((channel, thread)) or {"ok": True},
         ):
        result = bot_conversation._send_message(
            SimpleNamespace(profile_name="default"), args, _settings()
        )

    assert result["delivery"] == "sent"
    assert sent == [("channel", None)]


def test_chat_rejects_threads(tmp_path):
    args = {
        "action": "send", "message_type": "CHAT", "recipient_role": "cto",
        "coordination_id": "topic", "summary": "hello", "idempotency_key": "thread-1",
    }
    with patch.object(
        bot_conversation, "_session_target",
        side_effect=ValueError("bot_chat uses the dedicated bot-lounge channel chat; threads are disabled"),
    ):
        with pytest.raises(ValueError, match="threads are disabled"):
            bot_conversation._send_message(SimpleNamespace(profile_name="default"), args, _settings())


def test_cos_candidate_creates_triage_card_and_notifies_assignee(tmp_path):
    args = {
        "action": "submit_candidate",
        "message_type": "TASK_PROPOSAL",
        "coordination_id": "mail-calendar-topic",
        "summary": "Prepare two meeting slots",
        "title": "Draft meeting options",
        "deliverable": "Two candidate slots and a reply draft",
        "scope": "Read-only synthetic calendar data",
        "assignee": "cto",
        "reviewer": "cro",
        "idempotency_key": "candidate-1",
        "source_refs": ["synthetic:calendar:1"],
    }
    created_args = {}
    sent = []

    def fake_create(payload):
        created_args.update(payload)
        return json.dumps({"ok": True, "task_id": "task-1", "status": "triage"})

    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path), \
         patch.object(bot_conversation, "_config", return_value=_settings()), \
         patch.object(bot_conversation, "_session_target", return_value=("channel", None)), \
         patch.object(bot_conversation, "_candidate_source_receipt", return_value=_source_receipt()), \
         patch.object(bot_conversation, "_live_bot_id", return_value="200"), \
         patch.object(bot_conversation, "_send_formatted", side_effect=lambda *_args: sent.append(True) or {"ok": True}), \
         patch("tools.kanban_tools._handle_create", side_effect=fake_create):
        result = bot_conversation._submit_candidate(
            SimpleNamespace(profile_name="default"), args, _settings()
        )

    assert result["ok"] is True
    assert result["task_id"] == "task-1"
    assert result["status"] == "triage"
    assert result["notification"] == {"delivery": "sent"}
    assert created_args["triage"] is True
    assert created_args["initial_status"] == "triage"
    assert created_args["execution_scope"]["reviewer"] == "cro"
    assert sent == [True]


def test_candidate_creation_keeps_task_id_when_notification_fails(tmp_path):
    args = {
        "action": "submit_candidate", "message_type": "TASK_PROPOSAL",
        "coordination_id": "mail-calendar-topic", "summary": "Prepare two meeting slots",
        "title": "Draft meeting options", "deliverable": "Two candidate slots and a reply draft",
        "scope": "Read-only synthetic calendar data", "assignee": "cto", "reviewer": "cro",
        "idempotency_key": "candidate-1", "source_refs": ["synthetic:calendar:1"],
    }
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path), \
         patch.object(bot_conversation, "_session_target", return_value=("channel", None)), \
         patch.object(bot_conversation, "_candidate_source_receipt", return_value=_source_receipt()), \
         patch("tools.kanban_tools._handle_create", return_value=json.dumps({
             "ok": True, "task_id": "task-1", "status": "triage"
         })), \
         patch.object(bot_conversation, "_send_message", side_effect=RuntimeError("blocked")):
        result = bot_conversation._submit_candidate(
            SimpleNamespace(profile_name="default"), args, _settings()
        )

    assert result["ok"] is True
    assert result["task_id"] == "task-1"
    assert result["notification"] == {"delivery": "unknown"}


def test_candidate_creation_reports_task_id_when_receipt_recording_fails(tmp_path):
    args = {
        "action": "submit_candidate", "message_type": "TASK_PROPOSAL",
        "coordination_id": "mail-calendar-topic", "summary": "Prepare two meeting slots",
        "title": "Draft meeting options", "deliverable": "Two candidate slots and a reply draft",
        "scope": "Read-only synthetic calendar data", "assignee": "cto", "reviewer": "cro",
        "idempotency_key": "candidate-1", "source_refs": ["synthetic:calendar:1"],
    }
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path), \
         patch.object(bot_conversation, "_session_target", return_value=("channel", None)), \
         patch.object(bot_conversation, "_candidate_source_receipt", return_value=_source_receipt()), \
         patch("tools.kanban_tools._handle_create", return_value=json.dumps({
             "ok": True, "task_id": "task-1", "status": "triage"
         })), \
         patch.object(bot_conversation, "_record_candidate_task", side_effect=RuntimeError("ledger down")), \
         patch.object(bot_conversation, "_send_message") as notify:
        result = bot_conversation._submit_candidate(
            SimpleNamespace(profile_name="default"), args, _settings()
        )

    assert result == {
        "ok": False, "task_id": "task-1", "status": "triage", "candidate": True,
        "ledger": "unknown", "notification": {"delivery": "not-attempted"},
        "error": "Kanban task was created but its local candidate receipt could not be recorded",
    }
    notify.assert_not_called()


def test_candidate_requires_authenticated_discord_source_receipt(tmp_path):
    args = {
        "action": "submit_candidate", "message_type": "TASK_PROPOSAL",
        "coordination_id": "mail-calendar-topic", "summary": "Prepare two meeting slots",
        "title": "Draft meeting options", "deliverable": "Two candidate slots",
        "scope": "Read-only synthetic calendar data", "assignee": "cto", "reviewer": "cro",
        "idempotency_key": "candidate-1",
    }
    with patch.object(bot_conversation, "_session_target", return_value=("channel", None)), \
         patch.object(
             bot_conversation, "_candidate_source_receipt",
             side_effect=ValueError("candidate submission requires an authenticated Discord message receipt"),
         ), \
         patch("tools.kanban_tools._handle_create") as create:
        with pytest.raises(ValueError, match="authenticated Discord message receipt"):
            bot_conversation._submit_candidate(SimpleNamespace(profile_name="default"), args, _settings())
    create.assert_not_called()


def test_candidate_source_receipt_binds_message_and_channel(monkeypatch):
    values = {
        "HERMES_SESSION_PLATFORM": "discord",
        "HERMES_SESSION_MESSAGE_ID": "1234567890",
        "HERMES_SESSION_CHAT_ID": "channel",
        "HERMES_SESSION_CHAT_TYPE": "group",
        "HERMES_SESSION_PARENT_CHAT_ID": "",
        "HERMES_SESSION_THREAD_ID": "",
    }
    monkeypatch.setattr(
        "gateway.session_context.get_session_env",
        lambda name, default="": values.get(name, default),
    )
    receipt = bot_conversation._candidate_source_receipt("channel", None, "topic")
    assert receipt["trusted_ref"] == "discord:message:1234567890"
    assert receipt["channel_id"] == "channel"
    assert receipt["thread_id"] == ""


def test_session_target_requires_exact_direct_channel(monkeypatch):
    values = {
        "HERMES_SESSION_PLATFORM": "discord",
        "HERMES_SESSION_CHAT_ID": "channel",
        "HERMES_SESSION_CHAT_TYPE": "group",
        "HERMES_SESSION_PARENT_CHAT_ID": "",
        "HERMES_SESSION_THREAD_ID": "",
    }
    monkeypatch.setattr(
        "gateway.session_context.get_session_env",
        lambda name, default="": values.get(name, default),
    )
    assert bot_conversation._session_target({"channel_id": "channel"}) == ("channel", None)

    values.update({"HERMES_SESSION_CHAT_ID": "child", "HERMES_SESSION_PARENT_CHAT_ID": "channel"})
    with pytest.raises(ValueError, match="threads are disabled"):
        bot_conversation._session_target({"channel_id": "channel"})


def test_candidate_source_receipt_rejects_parent_only_context(monkeypatch):
    values = {
        "HERMES_SESSION_PLATFORM": "discord",
        "HERMES_SESSION_MESSAGE_ID": "1234567890",
        "HERMES_SESSION_CHAT_ID": "child",
        "HERMES_SESSION_CHAT_TYPE": "thread",
        "HERMES_SESSION_PARENT_CHAT_ID": "channel",
        "HERMES_SESSION_THREAD_ID": "",
    }
    monkeypatch.setattr(
        "gateway.session_context.get_session_env",
        lambda name, default="": values.get(name, default),
    )
    with pytest.raises(ValueError, match="authenticated Discord message receipt"):
        bot_conversation._candidate_source_receipt("channel", None, "topic")


def test_send_requires_idempotency_key():
    args = {
        "action": "send", "message_type": "CHAT", "recipient_role": "cto",
        "coordination_id": "topic", "summary": "hello",
    }
    with pytest.raises(ValueError, match="idempotency_key is required"):
        bot_conversation._send_message(SimpleNamespace(profile_name="default"), args, _settings())


def test_candidate_notification_receipt_failure_after_send_is_unknown_and_not_retried(tmp_path):
    args = {
        "action": "submit_candidate", "message_type": "TASK_PROPOSAL",
        "coordination_id": "mail-calendar-topic", "summary": "Prepare two meeting slots",
        "title": "Draft meeting options", "deliverable": "Two candidate slots",
        "scope": "Read-only synthetic calendar data", "assignee": "cto", "reviewer": "cro",
        "idempotency_key": "candidate-1",
    }
    original_record_notification = bot_conversation._record_candidate_notification

    def record_notification(candidate_key, delivery):
        if delivery == "sent":
            raise RuntimeError("ledger down")
        return original_record_notification(candidate_key, delivery)

    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path), \
         patch.object(bot_conversation, "_session_target", return_value=("channel", None)), \
         patch.object(bot_conversation, "_candidate_source_receipt", return_value=_source_receipt()), \
         patch("tools.kanban_tools._handle_create", return_value=json.dumps({
             "ok": True, "task_id": "task-1", "status": "triage"
         })), \
         patch.object(bot_conversation, "_record_candidate_notification",
                      side_effect=record_notification), \
         patch.object(bot_conversation, "_send_message", return_value={"ok": True}) as notify:
        first = bot_conversation._submit_candidate(
            SimpleNamespace(profile_name="default"), args, _settings()
        )
        second = bot_conversation._submit_candidate(
            SimpleNamespace(profile_name="default"), args, _settings()
        )

    assert first["notification"] == {"delivery": "sent"}
    assert first["ledger"] == "unknown"
    assert second["duplicate"] is True
    assert second["notification"] == {"delivery": "unknown"}
    notify.assert_called_once()


def test_duplicate_candidate_returns_recorded_notification_state(tmp_path):
    args = {
        "action": "submit_candidate", "message_type": "TASK_PROPOSAL",
        "coordination_id": "mail-calendar-topic", "summary": "Prepare two meeting slots",
        "title": "Draft meeting options", "deliverable": "Two candidate slots and a reply draft",
        "scope": "Read-only synthetic calendar data", "assignee": "cto", "reviewer": "cro",
        "idempotency_key": "candidate-1", "source_refs": ["synthetic:calendar:1"],
    }
    with patch.object(bot_conversation, "_ledger_root", return_value=tmp_path), \
         patch.object(bot_conversation, "_session_target", return_value=("channel", None)), \
         patch.object(bot_conversation, "_candidate_source_receipt", return_value=_source_receipt()), \
         patch("tools.kanban_tools._handle_create", return_value=json.dumps({
             "ok": True, "task_id": "task-1", "status": "triage"
         })), \
         patch.object(bot_conversation, "_send_message", return_value={"ok": False}) as notify:
        first = bot_conversation._submit_candidate(
            SimpleNamespace(profile_name="default"), args, _settings()
        )
        second = bot_conversation._submit_candidate(
            SimpleNamespace(profile_name="default"), args, _settings()
        )

    assert first["notification"] == {"delivery": "unknown"}
    assert second == {
        "ok": True, "duplicate": True, "task_id": "task-1", "status": "triage",
        "notification": {"delivery": "unknown"},
    }
    notify.assert_called_once()
