from types import SimpleNamespace
from typing import Any

from agent.turn_finalizer import finalize_turn


class FakeAgent:
    def __init__(self):
        self.max_iterations = 90
        self.iteration_budget = SimpleNamespace(remaining=10, used=1, max_total=90)
        self.quiet_mode = True
        self.model = "test-model"
        self.provider = "test-provider"
        self.base_url = ""
        self.session_id = "sess-test"
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = True
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []
        self.persisted_messages: list[dict[str, Any]] | None = None
        self._persist_user_message_idx: int | None = None
        self._persist_user_message_override: Any = None
        self._persist_user_message_timestamp: float | None = None
        self._turn_bot_chat_delivery_records: list[dict[str, Any]] = []

    def _handle_max_iterations(self, messages, api_call_count):
        raise AssertionError("not expected")

    def _emit_status(self, *_args, **_kwargs):
        pass

    def _safe_print(self, *_args, **_kwargs):
        pass

    def _save_trajectory(self, *_args, **_kwargs):
        pass

    def _cleanup_task_resources(self, *_args, **_kwargs):
        pass

    def _drop_trailing_empty_response_scaffolding(self, messages):
        pass

    def _persist_session(self, messages, conversation_history):
        # Capture the durable write before finalization restores API-local
        # guidance to the returned/live transcript.
        self.persisted_messages = [dict(message) for message in messages]

    def _apply_persist_user_message_override(self, messages):
        idx = self._persist_user_message_idx
        override = self._persist_user_message_override
        if idx is not None and override is not None:
            messages[idx]["content"] = override

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return False

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **_kwargs):
        pass


def _finalize(agent, messages, response):
    return finalize_turn(
        agent,
        final_response=response,
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="CROとCOOへPingして",
        original_user_message="CROとCOOへPingして",
        _should_review_memory=False,
        _turn_exit_reason="text_response(final)",
    )


def test_bot_chat_claim_without_receipts_is_corrected_and_persisted(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    messages = [
        {"role": "user", "content": "CROとCOOへPingして"},
        {"role": "assistant", "content": "CRO・COOへPingを送信しました。"},
    ]

    result = _finalize(agent, messages, "CRO・COOへPingを送信しました。")

    assert "上記の送信完了表現を取り消します" in result["final_response"]
    assert "COO=送信未確認" in result["final_response"]
    assert "CRO=送信未確認" in result["final_response"]
    assert result["response_transformed"] is True
    assert agent.persisted_messages[-1]["content"] == result["final_response"]


def test_bot_chat_claim_with_matching_receipts_is_unchanged(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    for role in ("cro", "coo"):
        agent._turn_bot_chat_delivery_records.append({
            "args": {"action": "send", "recipient_role": role},
            "result": '{"ok": true, "delivery": "sent"}',
            "is_error": False,
        })
    response = "CRO・COOへPingを送信しました。"
    messages = [{"role": "user", "content": "Pingして"}, {"role": "assistant", "content": response}]

    result = _finalize(agent, messages, response)

    assert result["final_response"] == response
    assert result["response_transformed"] is False
    assert agent.persisted_messages[-1]["content"] == response


def test_bot_chat_claim_reports_partial_unknown_per_recipient(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    agent._turn_bot_chat_delivery_records = [
        {"args": {"action": "send", "recipient_role": "cro"},
         "result": {"ok": True, "delivery": "sent"}, "is_error": False},
        {"args": {"action": "send", "recipient_role": "coo"},
         "result": "not-json", "is_error": True},
    ]
    response = "CRO・COOへPingを送信しました。"
    messages = [{"role": "user", "content": "Pingして"}, {"role": "assistant", "content": response}]

    result = _finalize(agent, messages, response)

    assert "CRO=送信確認" in result["final_response"]
    assert "COO=送信結果不明" in result["final_response"]


def test_bot_chat_claim_does_not_reuse_receipt_for_another_coordination_id(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    agent._turn_bot_chat_delivery_records = [{
        "args": {"action": "send", "recipient_role": "cro", "coordination_id": "current-cro"},
        "result": {"ok": True, "delivery": "sent"},
        "is_error": False,
    }]
    response = "宛先: CRO\ncoordination_id: prior-cro\ndelivery: sent"
    messages = [{"role": "user", "content": "Pingして"}, {"role": "assistant", "content": response}]

    result = _finalize(agent, messages, response)

    assert "話題ID=prior-cro=送信未確認" in result["final_response"]


def test_bot_chat_negative_or_planned_wording_is_not_a_completion_claim(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    for response in (
        "CROへは送信できませんでした。",
        "COOへ送信する予定です。",
        "> CROへ送信しました",
        "Reply received from CRO. Pingを受け取りました。",
    ):
        agent = FakeAgent()
        messages = [{"role": "user", "content": "status"}, {"role": "assistant", "content": response}]
        result = _finalize(agent, messages, response)
        assert result["final_response"] == response


def test_bot_chat_claims_require_matching_recipient_and_coordination_id(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    agent._turn_bot_chat_delivery_records = [
        {"args": {"action": "send", "recipient_role": "cro", "coordination_id": "topic-a"},
         "result": {"ok": True, "delivery": "sent"}, "is_error": False},
        {"args": {"action": "send", "recipient_role": "coo", "coordination_id": "topic-b"},
         "result": {"ok": True, "delivery": "sent"}, "is_error": False},
    ]
    response = (
        "宛先: CRO\ncoordination_id: topic-b\ndelivery: sent\n\n"
        "宛先: COO\ncoordination_id: topic-a\ndelivery: sent"
    )
    messages = [{"role": "user", "content": "status"}, {"role": "assistant", "content": response}]

    result = _finalize(agent, messages, response)

    assert "上記の送信完了表現を取り消します" in result["final_response"]
    assert "話題ID=topic-a=送信未確認" in result["final_response"]
    assert "話題ID=topic-b=送信未確認" in result["final_response"]


def test_bot_chat_common_positive_claim_forms_are_checked(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    for response, expected in (
        ("CRO、COOへ送信しました", ("CRO=送信未確認", "COO=送信未確認")),
        ("宛先: CRO\ncoordination_id: topic-a\ndelivery: sent", ("CRO=送信未確認",)),
        ("Sent a ping to CRO.", ("CRO=送信未確認",)),
        ("CROへ送信できませんでしたがCOOへ送信しました", ("COO=送信未確認",)),
        ("CROへ送信できず、COOへ送信しました", ("COO=送信未確認",)),
        ("CROへ送信できませんでした\nCOOへ送信しました", ("COO=送信未確認",)),
        ("CROから報告を受け、COOへ送信しました", ("COO=送信未確認",)),
    ):
        agent = FakeAgent()
        messages = [{"role": "user", "content": "status"}, {"role": "assistant", "content": response}]
        result = _finalize(agent, messages, response)
        for marker in expected:
            assert marker in result["final_response"]


def test_bot_chat_non_claim_forms_do_not_trigger_gate(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    for response in (
        "> CROへ送信しました。coordination_id: topic-a",
        "CROから「送信しました」と報告を受けました",
        "CROへ送信していませんでした",
        "CRO will present tomorrow.",
        "I have not sent a ping to CRO.",
        "CRO sent me a ping.",
        "「CROへ送信しました」という文は送信完了を意味します。",
        "CROへ送信したわけではありません。",
    ):
        agent = FakeAgent()
        messages = [{"role": "user", "content": "status"}, {"role": "assistant", "content": response}]
        assert _finalize(agent, messages, response)["final_response"] == response


def test_bot_chat_gate_survives_persistence_shaping_failure(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    agent._apply_persist_user_message_override = lambda _messages: (_ for _ in ()).throw(
        RuntimeError("override failed")
    )
    response = "CROへ送信しました"
    messages = [
        {"role": "user", "content": "status"},
        {"role": "assistant", "content": response, "api_content": response},
    ]

    result = _finalize(agent, messages, response)

    assert "CRO=送信未確認" in result["final_response"]
    assert result["messages"][-1]["content"] == result["final_response"]
    assert "api_content" not in result["messages"][-1]
    assert agent._session_messages[-1]["content"] == result["final_response"]
    assert any("override failed" in error for error in result["cleanup_errors"])


def test_bot_chat_gate_drops_stale_assistant_api_content(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    response = "CROへ送信しました"
    messages = [
        {"role": "user", "content": "status"},
        {"role": "assistant", "content": response, "api_content": response},
    ]

    result = _finalize(agent, messages, response)

    assert "api_content" not in result["messages"][-1]
    assert "CRO=送信未確認" in result["messages"][-1]["content"]


def test_bot_chat_status_parser_preserves_exact_ids_and_record_pairs(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    for response in (
        "宛先: CRO\ncoordination_id: topic-a.new\ndelivery: sent",
        "CROへ送信しました coordination_id: `topic-a.new`",
        "宛先: `CRO`\ncoordination_id: `topic-a.new`\ndelivery: `sent`",
    ):
        agent = FakeAgent()
        agent._turn_bot_chat_delivery_records = [{
            "args": {"action": "send", "recipient_role": "cro", "coordination_id": "topic-a"},
            "result": {"ok": True, "delivery": "sent"},
            "is_error": False,
        }]
        messages = [{"role": "user", "content": "status"}, {"role": "assistant", "content": response}]
        result = _finalize(agent, messages, response)
        assert "話題ID=topic-a.new=送信未確認" in result["final_response"]

    agent = FakeAgent()
    agent._turn_bot_chat_delivery_records = [
        {"args": {"action": "send", "recipient_role": "cro", "coordination_id": "topic-a"},
         "result": {"ok": True, "delivery": "sent"}, "is_error": False},
        {"args": {"action": "send", "recipient_role": "coo", "coordination_id": "topic-b"},
         "result": {"ok": True, "delivery": "sent"}, "is_error": False},
    ]
    response = (
        "宛先: CRO\ncoordination_id: topic-a\ndelivery: sent\n"
        "宛先: COO\ncoordination_id: topic-b\ndelivery: sent"
    )
    messages = [{"role": "user", "content": "status"}, {"role": "assistant", "content": response}]
    assert _finalize(agent, messages, response)["final_response"] == response


def test_bot_chat_interrupted_nonempty_response_is_checked(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    response = "CROへ送信しました"
    messages = [{"role": "user", "content": "status"}, {"role": "assistant", "content": response}]

    result = finalize_turn(
        agent,
        final_response=response,
        api_call_count=1,
        interrupted=True,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="status",
        original_user_message="status",
        _should_review_memory=False,
        _turn_exit_reason="interrupted",
    )

    assert "CRO=送信未確認" in result["final_response"]






def test_final_response_closes_tool_tail_before_persistence(monkeypatch):
    """A recovered/previewed final response must be durable in session history.

    Regression for turns where the caller receives a non-empty final_response,
    but the message transcript still ends at a tool result. If persisted that
    way, the next turn reloads a stale/malformed history and can appear to loop
    because the assistant's visible final answer is missing from durable state.
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    messages = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "I'll check.",
            "tool_calls": [
                {"id": "call-1", "function": {"name": "terminal", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "name": "terminal", "content": "ok"},
    ]

    result = finalize_turn(
        agent,
        final_response="Done.",
        api_call_count=2,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="do it",
        original_user_message="do it",
        _should_review_memory=False,
        _turn_exit_reason="fallback_prior_turn_content",
    )

    assert result["messages"][-1]["role"] == "assistant"
    assert result["messages"][-1]["content"] == "Done."
    assert isinstance(result["messages"][-1]["timestamp"], float)
    assert agent.persisted_messages is not None
    assert agent.persisted_messages[-1] == result["messages"][-1]


def test_fallback_timestamp_survives_delayed_sqlite_persistence(
    monkeypatch, tmp_path
):
    """The durable row records message creation, not the later DB flush."""
    from hermes_state import SessionDB

    created_at = 1_781_976_577.25
    persisted_at = created_at + 600
    monkeypatch.setattr("agent.message_metadata.wall_time", lambda: created_at)
    monkeypatch.setattr("hermes_state.time.time", lambda: persisted_at)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("sess-test", source="cli")
    agent = FakeAgent()

    def persist_to_sqlite(messages, _conversation_history):
        db.replace_messages(agent.session_id, messages)
        agent.persisted_messages = db.get_messages_as_conversation(agent.session_id)

    agent._persist_session = persist_to_sqlite
    messages = [
        {"role": "user", "content": "do it", "timestamp": created_at - 1},
        {"role": "tool", "content": "ok", "tool_call_id": "call-1"},
    ]

    finalize_turn(
        agent,
        final_response="Done.",
        api_call_count=2,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="do it",
        original_user_message="do it",
        _should_review_memory=False,
        _turn_exit_reason="fallback_prior_turn_content",
    )

    assert agent.persisted_messages[-1]["timestamp"] == created_at
    assert agent.persisted_messages[-1]["timestamp"] != persisted_at


def test_final_response_fills_pure_tool_call_tail(monkeypatch):
    """A tail assistant row that is a *pure tool-call turn* carries no answer.

    The role check alone ("tail is assistant ⇒ nothing to do") leaves the
    #43849/#44100 invariant unmet when the tail is ``assistant(tool_calls)``
    with no text of its own: the caller and the gateway already delivered
    ``final_response``, but it never reaches the transcript. The next turn then
    replays the user backlog and the model re-answers it — the exact symptom
    that block exists to prevent.
    """
    agent = FakeAgent()
    messages = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "type": "function",
                 "function": {"name": "f", "arguments": "{}"}}
            ],
        },
    ]

    result = finalize_turn(
        agent,
        final_response="Here is your answer.",
        api_call_count=3,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="t",
        turn_id="tid",
        user_message="q",
        original_user_message="q",
        _should_review_memory=False,
        _turn_exit_reason="text_response(final)",
    )

    persisted = agent.persisted_messages
    assert any(
        m.get("role") == "assistant" and m.get("content") == result["final_response"]
        for m in persisted
    ), "delivered final_response never reached the durable transcript"
    # Filled in place — no assistant→assistant pair, tool_calls preserved.
    assert persisted[-1]["content"] == "Here is your answer."
    assert persisted[-1]["tool_calls"]
    assert sum(1 for m in persisted if m.get("role") == "assistant") == 1






def test_final_response_fill_invalidates_flush_scan_cursor():
    """The fill's marker pop must invalidate the bounded flush-scan cursor.

    The cursor (run_agent.py) skips the identity-matched prefix of its
    previous snapshot assuming no live dict loses ``_db_persisted`` in place
    — the fill is the one path that pops it. Without invalidation, the
    turn-end flush skips the filled row as 'already stamped' and the
    delivered answer never reaches state.db (the #43849 class resurfacing).
    """
    agent = FakeAgent()
    agent._db_flush_scan_prefix = ["prior-snapshot"]
    messages = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "type": "function",
                 "function": {"name": "f", "arguments": "{}"}}
            ],
            "_db_persisted": True,
        },
    ]

    finalize_turn(
        agent,
        final_response="Here is your answer.",
        api_call_count=3,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="t",
        turn_id="tid",
        user_message="q",
        original_user_message="q",
        _should_review_memory=False,
        _turn_exit_reason="text_response(final)",
    )

    assert agent._db_flush_scan_prefix is None


def test_empty_final_response_recovers_stream_buffer_into_blank_assistant_row(
    monkeypatch, tmp_path
):
    """#95514: empty terminal completion must not persist content='' over a live stream.

    After a tool result the incremental flush can leave a blank assistant tail.
    If finalize_turn then receives final_response='' while the stream buffer
    still holds the delivered text, that blank row is the durable transcript —
    Desktop reload shows nothing even though the user already saw the answer.
    """
    from hermes_state import SessionDB

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("sess-test", source="cli")
    agent = FakeAgent()
    agent._current_streamed_assistant_text = "Already streamed to the user."

    def persist_to_sqlite(messages, _conversation_history):
        db.replace_messages(agent.session_id, messages)
        agent.persisted_messages = db.get_messages_as_conversation(agent.session_id)

    agent._persist_session = persist_to_sqlite
    messages = [
        {"role": "user", "content": "summarize"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "type": "function",
                 "function": {"name": "terminal", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "name": "terminal", "content": "ok"},
        {"role": "assistant", "content": "", "_db_persisted": True},
    ]

    result = finalize_turn(
        agent,
        final_response="",
        api_call_count=2,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="summarize",
        original_user_message="summarize",
        _should_review_memory=False,
        _turn_exit_reason="text_response(final)",
    )

    assert result["final_response"] == "Already streamed to the user."
    assistants = [m for m in agent.persisted_messages if m.get("role") == "assistant"]
    assert assistants[-1]["content"] == "Already streamed to the user."
    assert assistants[0].get("tool_calls")
    assert sum(1 for m in assistants) == 2

    reopened = SessionDB(db_path=tmp_path / "state.db")
    reloaded = reopened.get_messages_as_conversation("sess-test")
    assert reloaded[-1]["role"] == "assistant"
    assert reloaded[-1]["content"] == "Already streamed to the user."
    assert sum(1 for m in reloaded if m.get("role") == "assistant") == 2


def test_failed_turn_does_not_recover_stream_buffer_as_final_response(monkeypatch):
    """A failed turn must not invent a successful answer from the stream buffer."""
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    agent._current_streamed_assistant_text = "partial tokens"

    result = finalize_turn(
        agent,
        final_response="",
        api_call_count=1,
        interrupted=False,
        failed=True,
        messages=[{"role": "user", "content": "q"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="q",
        original_user_message="q",
        _should_review_memory=False,
        _turn_exit_reason="error",
    )

    assert result["final_response"] in ("", None)
    assert result["failed"] is True


def test_stream_recovered_final_response_survives_persist_step_failure(monkeypatch):
    """#95514 + #8049 ordering: the stream-recovered ``final_response`` is bound as soon
    as it is computed, BEFORE the fallible tail-shaping / override / persist calls in the
    guarded persist step. A raise later in that step (here the persist-override) must not
    drop text the user already saw — the caller still gets the streamed answer and the
    failure is reported via ``cleanup_errors``."""
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    agent._current_streamed_assistant_text = "  streamed answer  "

    def exploding_override(_messages):
        raise RuntimeError("override exploded")

    agent._apply_persist_user_message_override = exploding_override
    messages = [{"role": "user", "content": "q"}, {"role": "assistant", "content": ""}]

    result = finalize_turn(
        agent,
        final_response="",
        api_call_count=2,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="q",
        original_user_message="q",
        _should_review_memory=False,
        _turn_exit_reason="text_response(final)",
    )

    assert result["final_response"] == "streamed answer"
    assert result["cleanup_errors"] == ["persist_session: override exploded"]
    # The blank tail was filled before the raise (same order as the inline BASE block).
    assert messages[-1]["content"] == "streamed answer"


def test_delivery_only_reasoning_excerpt_does_not_fill_blank_assistant(monkeypatch):
    """Labeled empty-terminal excerpt is delivery-only, not durable content.

    ``empty_response_exhausted`` sets final_response to a labeled reasoning
    excerpt while the transcript tail is a blank/sentinel assistant without
    tool_calls. Filling that tail would persist the excerpt and break replay
    (test_empty_terminal_reasoning_surface).
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    excerpt = (
        "⚠️ The model produced only internal reasoning and no final answer, "
        "despite retries. Its last reasoning, which may contain the answer:\n\n"
        "The answer is 42"
    )
    messages = [
        {"role": "user", "content": "what is the answer?"},
        {"role": "assistant", "content": ""},
    ]

    result = finalize_turn(
        agent,
        final_response=excerpt,
        api_call_count=6,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="what is the answer?",
        original_user_message="what is the answer?",
        _should_review_memory=False,
        _turn_exit_reason="empty_response_exhausted",
    )

    assert result["final_response"] == excerpt
    assert not any(
        m.get("role") == "assistant"
        and "only internal reasoning" in (m.get("content") or "")
        for m in result["messages"]
    )
