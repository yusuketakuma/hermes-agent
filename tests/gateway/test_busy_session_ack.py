"""Tests for busy-session acknowledgment when user sends messages during active agent runs.

Verifies that users get an immediate status response instead of total silence
when the agent is working on a task. See PR fix for the @Lonely__MH report.
"""
import asyncio
import time
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Minimal stubs so we can import gateway code without heavy deps
# ---------------------------------------------------------------------------
import sys, types

_tg = types.ModuleType("telegram")
_tg.constants = types.ModuleType("telegram.constants")
_ct = MagicMock()
_ct.SUPERGROUP = "supergroup"
_ct.GROUP = "group"
_ct.PRIVATE = "private"
_tg.constants.ChatType = _ct
sys.modules.setdefault("telegram", _tg)
sys.modules.setdefault("telegram.constants", _tg.constants)
sys.modules.setdefault("telegram.ext", types.ModuleType("telegram.ext"))

from gateway.platforms.base import (
    Platform,
    SessionSource,
    build_session_key,
)
from gateway.platforms.event import MessageEvent, MessageType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(text="hello", chat_id="123", platform_val="telegram"):
    """Build a minimal MessageEvent."""
    source = SessionSource(
        platform=MagicMock(value=platform_val),
        chat_id=chat_id,
        chat_type="private",
        user_id="user1",
    )
    evt = MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg1",
    )
    return evt


def _make_runner():
    """Build a minimal GatewayRunner-like object for testing."""
    from gateway.run import GatewayRunner, _AGENT_PENDING_SENTINEL

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._busy_ack_ts = {}
    runner._draining = False
    runner._busy_text_mode = "interrupt"
    runner.adapters = {}
    runner.config = MagicMock()
    runner.config.group_sessions_per_user = True
    runner.config.thread_sessions_per_user = False
    runner.session_store = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner._is_user_authorized = lambda _source: True
    return runner, _AGENT_PENDING_SENTINEL


def _make_adapter(platform_val="telegram"):
    """Build a minimal adapter mock."""
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._send_with_retry = AsyncMock()
    adapter.config = MagicMock()
    adapter.config.extra = {}
    adapter.platform = MagicMock(value=platform_val)
    adapter._text_debounce = {}
    adapter._busy_text_debounce_seconds = 0.6
    return adapter


def _load_bot_conversation_plugin():
    plugin_path = Path(__file__).resolve().parents[3] / "plugins" / "bot-conversation" / "__init__.py"
    spec = importlib.util.spec_from_file_location("bot_conversation_gateway_test_plugin", plugin_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBusySessionAck:
    """User sends a message while agent is running — should get acknowledgment."""


    def test_pre_dispatch_skip_has_priority_over_allow(self, monkeypatch):
        runner, _sentinel = _make_runner()
        event = _make_event()

        monkeypatch.setattr(
            "hermes_cli.lifecycle.invoke_hook",
            lambda *_args, **_kwargs: [{"action": "allow"}, {"action": "skip", "reason": "blocked"}],
        )

        assert runner._hm_pre_gateway_dispatch_hook(event, event.source) is None


    def test_bot_chat_hook_failure_fails_closed(self, monkeypatch):
        runner, _sentinel = _make_runner()
        event = _make_event(text="種別=CHAT\n要約=hello", chat_id="1548242914268807228", platform_val="discord")
        event.source.is_bot = True

        def fail(*_args, **_kwargs):
            raise RuntimeError("hook unavailable")

        monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", fail)

        assert runner._hm_pre_gateway_dispatch_hook(event, event.source) is None


    def test_unregistered_bot_chat_route_fails_closed(self, monkeypatch):
        runner, _sentinel = _make_runner()
        event = _make_event(text="種別=CHAT\n要約=hello", chat_id="1548242914268807228", platform_val="discord")
        event.source.is_bot = True
        monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_args, **_kwargs: [])

        assert runner._hm_pre_gateway_dispatch_hook(event, event.source) is None


    @pytest.mark.parametrize(
        "before,after_text",
        [
            ("CHAT", "種別=TASK_PROPOSAL\n要約=rewritten"),
            ("TASK_PROPOSAL", "種別=CHAT\n要約=rewritten"),
            ("TASK_PROPOSAL", "🧭 種別=TASK_PROPOSAL\n種別=CHAT\n要約=rewritten"),
            ("CHAT", "🧭 種別=CHAT\n種別=TASK_PROPOSAL\n要約=rewritten"),
        ],
    )
    def test_routing_relevant_rewrite_is_rejected(self, monkeypatch, before, after_text):
        runner, _sentinel = _make_runner()
        event = _make_event(
            text=f"種別={before}\n要約=hello", chat_id="1548242914268807228", platform_val="discord",
        )
        event.source.is_bot = True
        plugin = _load_bot_conversation_plugin()
        monkeypatch.setattr(
            plugin, "_config", lambda: {"channel_id": "1548242914268807228"}
        )
        monkeypatch.setattr(
            "hermes_cli.lifecycle.invoke_hook",
            lambda name, **kwargs: [
                plugin._on_pre_gateway_dispatch(**kwargs),
                {"action": "rewrite", "text": after_text},
            ] if name == "pre_gateway_dispatch" else [],
        )

        assert runner._hm_pre_gateway_dispatch_hook(event, event.source) is None
        assert event.metadata["_bot_conversation_route_checked"] is True


    @pytest.mark.asyncio
    async def test_pending_slot_keeps_formal_and_local_routes_separate(self):
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        adapter = _make_adapter()
        source = SessionSource(
            platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", user_id="user1",
        )
        sk = build_session_key(source)
        runner.adapters[source.platform] = adapter
        runner._running_agents[sk] = sentinel

        local = MessageEvent(
            text="local", source=source, message_id="local", metadata={
                "turn_policy": {"route": "local", "enabled_toolsets": ["bot_conversation"]},
            },
        )
        local._gateway_dispatch_classified = True
        formal = MessageEvent(text="formal", source=source, message_id="formal", metadata={})
        formal._gateway_dispatch_classified = True

        await runner._handle_active_session_busy_message(local, sk)
        await runner._handle_active_session_busy_message(formal, sk)

        assert adapter._pending_messages[sk] is local
        assert [event.text for event in runner._queued_events[sk]] == ["formal"]


    @pytest.mark.asyncio
    async def test_telegram_grace_followups_respect_queue_fifo(self, monkeypatch):
        """Rapid Telegram text follow-ups in queue mode must not merge."""
        from gateway.run import GatewayRunner

        monkeypatch.setenv("HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS", "3.0")

        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "queue"
        runner._queued_events = {}
        adapter = _make_adapter()

        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="123",
            chat_type="dm",
            user_id="user1",
        )
        sk = build_session_key(source)
        runner.adapters[source.platform] = adapter

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "seconds_since_activity": 0.0,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time()

        events = [
            MessageEvent(
                text=text,
                message_type=MessageType.TEXT,
                source=source,
                message_id=f"m-{idx}",
            )
            for idx, text in enumerate(("first", "second", "third"), start=1)
        ]

        for event in events:
            result = await GatewayRunner._handle_message(runner, event)
            assert result is None

        assert adapter._pending_messages[sk].text == "first"
        assert [event.text for event in runner._queued_events[sk]] == [
            "second",
            "third",
        ]
        agent.interrupt.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_ack_when_agent_running(self):
        """First message during busy session should get a status ack."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event = _make_event(text="Are you working?")
        sk = build_session_key(event.source)

        # Simulate running agent
        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 21,
            "max_iterations": 60,
            "current_tool": "terminal",
            "last_activity_ts": time.time(),
            "last_activity_desc": "terminal",
            "seconds_since_activity": 1.0,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 600  # 10 min ago
        runner.adapters[event.source.platform] = adapter

        result = await runner._handle_active_session_busy_message(event, sk)

        assert result is True  # handled
        # Verify ack was sent
        adapter._send_with_retry.assert_called_once()
        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content", "")
        if not content and call_kwargs.args:
            # positional args
            content = str(call_kwargs)
        assert "Interrupting" in content or "respond" in content
        assert "/stop" not in content  # no need — we ARE interrupting

        # Verify agent interrupt was called
        agent.interrupt.assert_called_once_with("Are you working?")


    @pytest.mark.asyncio
    async def test_steer_mode_calls_agent_steer_no_interrupt_no_queue(self, monkeypatch):
        """busy_input_mode='steer' injects via agent.steer() and skips queueing."""
        import gateway.run as _gr

        monkeypatch.delenv("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED", raising=False)
        monkeypatch.setattr(_gr, "_load_gateway_config", lambda: {})
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        adapter = _make_adapter()

        event = _make_event(text="also check the tests")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        runner._running_agents[sk] = agent

        with patch("gateway.platforms.base.merge_pending_message_event") as mock_merge:
            await runner._handle_active_session_busy_message(event, sk)

        # VERIFY: Agent was steered, NOT interrupted
        agent.steer.assert_called_once()
        injected = agent.steer.call_args.args[0]
        assert injected.endswith("also check the tests")
        assert '"chat_id": "123"' in injected
        agent.interrupt.assert_not_called()

        # VERIFY: No queueing — successful steer must NOT replay as next turn
        mock_merge.assert_not_called()

        # VERIFY: Ack mentions steer wording
        adapter._send_with_retry.assert_called_once()
        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content", "")
        assert "Steered" in content or "steer" in content.lower()
        assert "Interrupting" not in content

    @pytest.mark.asyncio
    @pytest.mark.parametrize("running_policy,incoming_policy", [
        (None, {"route": "local", "provider": "custom:mac-mini-ollama", "model": "hermes-fallback:latest", "enabled_toolsets": ["bot_conversation"]}),
        ({"route": "local", "provider": "custom:mac-mini-ollama", "model": "hermes-fallback:latest", "enabled_toolsets": ["bot_conversation"]}, None),
    ])
    async def test_busy_route_change_is_queued_without_steer_or_interrupt(
        self, running_policy, incoming_policy, monkeypatch,
    ):
        import gateway.run as _gr

        monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
        monkeypatch.setattr(_gr, "_load_gateway_config", lambda: {})
        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        adapter = _make_adapter()
        event = _make_event(text="change route")
        event.metadata = {"turn_policy": incoming_policy} if incoming_policy else {}
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        agent._gateway_turn_policy = running_policy
        agent.steer.return_value = True
        runner._running_agents[sk] = agent

        await runner._handle_active_session_busy_message(event, sk)

        assert adapter._pending_messages[sk] is event
        agent.steer.assert_not_called()
        agent.interrupt.assert_not_called()

    @pytest.mark.asyncio
    async def test_pending_interrupt_rejects_mismatched_turn_policy(self):
        runner, _sentinel = _make_runner()
        adapter = _make_adapter()
        event = _make_event(text="formal follow-up")
        event.metadata = {"turn_policy": {"route": "formal"}}
        sk = build_session_key(event.source)
        adapter._pending_messages[sk] = event

        agent = MagicMock()
        agent._gateway_turn_policy = {"route": "local"}
        detected = asyncio.Event()
        log = MagicMock()

        interrupted = await runner._run_agent_fire_pending_interrupt(
            adapter, agent, event.source, sk, detected, [None], log_context="test", log=log,
        )

        assert interrupted is False
        assert adapter._pending_messages[sk] is event
        agent.interrupt.assert_not_called()
        assert not detected.is_set()
        log.assert_not_called()

    @pytest.mark.asyncio
    async def test_pending_interrupt_handles_compatible_event(self):
        runner, _sentinel = _make_runner()
        adapter = _make_adapter()
        event = _make_event(text="same route")
        sk = build_session_key(event.source)
        adapter._pending_messages[sk] = event

        agent = MagicMock()
        agent._gateway_turn_policy = None
        detected = asyncio.Event()
        log = MagicMock()

        interrupted = await runner._run_agent_fire_pending_interrupt(
            adapter, agent, event.source, sk, detected, [None], log_context="test", log=log,
        )

        assert interrupted is True
        agent.interrupt.assert_called_once_with("same route")
        assert detected.is_set()
        log.assert_called_once()

    @pytest.mark.asyncio
    async def test_pending_bot_interrupt_requires_dispatch_attestation(self):
        runner, _sentinel = _make_runner()
        adapter = _make_adapter(platform_val="discord")
        event = _make_event(
            text="種別=CHAT\n要約=hello", chat_id="1548242914268807228", platform_val="discord",
        )
        event.source.is_bot = True
        event.metadata = {}
        sk = build_session_key(event.source)
        adapter._pending_messages[sk] = event

        agent = MagicMock()
        agent._gateway_turn_policy = None
        detected = asyncio.Event()

        interrupted = await runner._run_agent_fire_pending_interrupt(
            adapter, agent, event.source, sk, detected, [None], log_context="test", log=MagicMock(),
        )

        assert interrupted is False
        assert adapter._pending_messages[sk] is event
        agent.interrupt.assert_not_called()
        assert not detected.is_set()

    @pytest.mark.asyncio
    async def test_interrupt_monitor_retries_after_rejected_pending_event(self, monkeypatch):
        runner, _sentinel = _make_runner()
        adapter = _make_adapter()
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm")
        sk = build_session_key(source)
        runner._adapter_for_source = lambda _source: adapter
        adapter.has_pending_interrupt = MagicMock(return_value=True)
        runner._run_agent_fire_pending_interrupt = AsyncMock(side_effect=[False, True])
        sleep_calls = []

        async def no_sleep(_delay):
            sleep_calls.append(_delay)

        monkeypatch.setattr("gateway.run_turn.asyncio.sleep", no_sleep)
        turn_ctx = SimpleNamespace(
            source=source, session_key=sk, agent_holder=[MagicMock()],
            streaming_tts_consumer_holder=[None],
        )

        await runner._run_agent_monitor_for_interrupt(turn_ctx, asyncio.Event())

        assert runner._run_agent_fire_pending_interrupt.await_count == 2
        assert sleep_calls == [0.2, 0.2]

    @pytest.mark.asyncio
    async def test_completed_turn_stops_monitor_before_queued_followup(self, monkeypatch):
        runner, _sentinel = _make_runner()
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm")
        turn_ctx = SimpleNamespace(
            source=source, session_key=build_session_key(source), session_id="session-id",
            run_generation=None, _interrupt_depth=0, history=[], context_prompt="",
            _status_thread_metadata=None, event_message_id=None, inbound_message_id=None,
            turn_policy=None, agent_holder=[MagicMock()], result_holder=[None],
            stream_consumer_holder=[None], streaming_tts_consumer_holder=[None],
        )
        turn_runner = SimpleNamespace(run_sync=MagicMock())
        display = SimpleNamespace(
            needs_progress_queue=False, log_mode_enabled=False, _native_slack_task_cards=False,
        )
        monitor_started = asyncio.Event()
        monitor_cancelled = asyncio.Event()

        async def monitor(_ctx, _detected):
            monitor_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                monitor_cancelled.set()
                raise

        async def await_worker(*_args):
            await monitor_started.wait()
            turn_ctx.result_holder[0] = {
                "final_response": "parent", "messages": [], "interrupted": True,
            }
            return {"final_response": "parent"}

        async def child_run(*_args, **_kwargs):
            assert monitor_cancelled.is_set()
            return {"final_response": "child", "messages": []}

        async def noop(*_args, **_kwargs):
            return None

        runner._get_proxy_url = lambda: None
        runner._run_agent_display_settings = lambda _source: display
        runner._run_agent_build_turn_context = lambda *args, **kwargs: (turn_ctx, turn_runner, None)
        runner._run_agent_bind_turn_wiring = lambda *args, **kwargs: None
        runner._run_agent_start_streaming_tts = lambda *args, **kwargs: None
        runner._run_agent_start_turn_worker = lambda *args, **kwargs: SimpleNamespace(executor_task=None)
        runner._run_agent_await_turn_worker = await_worker
        runner._run_agent_monitor_for_interrupt = monitor
        runner._run_agent_finalize_streaming_tts = noop
        runner._run_agent_drain_pending = AsyncMock(return_value=(None, "queued"))
        runner._run_agent_evict_on_fallback = lambda _ctx: None
        runner._run_agent_cleanup_turn_tasks = noop
        runner._run_agent_stream_consumer_task = noop
        runner._run_agent_track_agent = noop
        runner._run_agent_notify_long_running = noop
        runner._adapter_for_source = lambda _source: None
        runner._refresh_agent_cache_message_count = noop
        runner._run_agent = child_run

        result = await runner._run_agent_inner(
            message="parent", context_prompt="", history=[], source=source,
            session_id="session-id", session_key=turn_ctx.session_key,
        )

        assert result["final_response"] == "child"
        assert monitor_cancelled.is_set()

    @pytest.mark.asyncio
    async def test_parent_cancellation_during_monitor_shutdown_propagates(self):
        runner, _sentinel = _make_runner()
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm")
        turn_ctx = SimpleNamespace(
            source=source, session_key=build_session_key(source), session_id="session-id",
            run_generation=None, _interrupt_depth=0, history=[], context_prompt="",
            _status_thread_metadata=None, event_message_id=None, inbound_message_id=None,
            turn_policy=None, agent_holder=[MagicMock()], result_holder=[None],
            stream_consumer_holder=[None], streaming_tts_consumer_holder=[None],
        )
        turn_runner = SimpleNamespace(run_sync=MagicMock())
        display = SimpleNamespace(
            needs_progress_queue=False, log_mode_enabled=False, _native_slack_task_cards=False,
        )
        monitor_started = asyncio.Event()
        monitor_cancelled = asyncio.Event()

        async def monitor(_ctx, _detected):
            monitor_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                monitor_cancelled.set()
                raise

        async def await_worker(*_args):
            await monitor_started.wait()
            turn_ctx.result_holder[0] = {
                "final_response": "parent", "messages": [], "interrupted": True,
            }
            asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
            return {"final_response": "parent"}

        drain_pending = AsyncMock(return_value=(None, "queued"))
        child_run = AsyncMock(return_value={"final_response": "child", "messages": []})

        async def noop(*_args, **_kwargs):
            return None

        runner._get_proxy_url = lambda: None
        runner._run_agent_display_settings = lambda _source: display
        runner._run_agent_build_turn_context = lambda *args, **kwargs: (turn_ctx, turn_runner, None)
        runner._run_agent_bind_turn_wiring = lambda *args, **kwargs: None
        runner._run_agent_start_streaming_tts = lambda *args, **kwargs: None
        runner._run_agent_start_turn_worker = lambda *args, **kwargs: SimpleNamespace(executor_task=None)
        runner._run_agent_await_turn_worker = await_worker
        runner._run_agent_monitor_for_interrupt = monitor
        runner._run_agent_finalize_streaming_tts = noop
        runner._run_agent_drain_pending = drain_pending
        runner._run_agent_evict_on_fallback = lambda _ctx: None
        runner._run_agent_cleanup_turn_tasks = noop
        runner._run_agent_stream_consumer_task = noop
        runner._run_agent_track_agent = noop
        runner._run_agent_notify_long_running = noop
        runner._adapter_for_source = lambda _source: None
        runner._refresh_agent_cache_message_count = noop
        runner._run_agent = child_run

        with pytest.raises(asyncio.CancelledError):
            await runner._run_agent_inner(
                message="parent", context_prompt="", history=[], source=source,
                session_id="session-id", session_key=turn_ctx.session_key,
            )

        assert monitor_cancelled.is_set()
        drain_pending.assert_not_awaited()
        child_run.assert_not_awaited()

    @pytest.mark.parametrize("interrupted", [False, True])
    @pytest.mark.asyncio
    async def test_stream_error_does_not_replace_queued_child_result(self, interrupted):
        from gateway.run import GatewayRunner

        runner, _sentinel = _make_runner()
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm")
        session_key = build_session_key(source)
        stream_done = asyncio.Event()
        consumer = SimpleNamespace(final_response_sent=False, final_content_delivered=False)
        turn_ctx = SimpleNamespace(
            source=source, session_key=session_key, session_id="session-id", run_generation=None,
            _interrupt_depth=0, history=[], context_prompt="", _status_thread_metadata=None,
            event_message_id=None, inbound_message_id=None, turn_policy=None,
            agent_holder=[MagicMock()], result_holder=[None], stream_consumer_holder=[consumer],
            streaming_tts_consumer_holder=[None], stream_task_drained=False,
        )
        display = SimpleNamespace(
            needs_progress_queue=False, log_mode_enabled=False, _native_slack_task_cards=False,
        )

        async def idle(*_args, **_kwargs):
            await asyncio.Event().wait()

        async def noop(*_args, **_kwargs):
            return None

        async def stream_consumer_task(*_args, **_kwargs):
            await asyncio.sleep(0)
            stream_done.set()
            raise RuntimeError("parent stream error")

        async def await_worker(*_args):
            await asyncio.sleep(0)
            turn_ctx.result_holder[0] = {
                "final_response": "", "messages": [], "interrupted": interrupted,
            }
            return {"final_response": ""}

        child_run = AsyncMock(return_value={"final_response": "child", "messages": []})
        runner._get_proxy_url = lambda: None
        runner._run_agent_display_settings = lambda _source: display
        runner._run_agent_build_turn_context = lambda *args, **kwargs: (
            turn_ctx, SimpleNamespace(run_sync=MagicMock()), None,
        )
        runner._run_agent_bind_turn_wiring = lambda *args, **kwargs: None
        runner._run_agent_start_streaming_tts = lambda *args, **kwargs: None
        runner._run_agent_start_turn_worker = lambda *args, **kwargs: SimpleNamespace(executor_task=None)
        runner._run_agent_await_turn_worker = await_worker
        runner._run_agent_monitor_for_interrupt = idle
        runner._run_agent_stream_consumer_task = stream_consumer_task
        runner._run_agent_track_agent = idle
        runner._run_agent_notify_long_running = idle
        runner._run_agent_finalize_streaming_tts = noop
        runner._run_agent_drain_pending = AsyncMock(return_value=(None, "queued"))
        runner._run_agent_evict_on_fallback = lambda _ctx: None
        runner._adapter_for_source = lambda _source: None
        runner._refresh_agent_cache_message_count = noop
        runner._run_agent = child_run
        runner._release_running_agent_state = MagicMock()

        result = await GatewayRunner._run_agent_inner(
            runner, message="parent", context_prompt="", history=[], source=source,
            session_id="session-id", session_key=session_key,
        )

        assert result["final_response"] == "child"
        assert stream_done.is_set()
        assert turn_ctx.stream_task_drained is True
        child_run.assert_awaited_once()
        runner._release_running_agent_state.assert_called_once_with(session_key, run_generation=None)

    @pytest.mark.asyncio
    async def test_stream_task_wait_preserves_parent_cancellation(self):
        from gateway.run import GatewayRunner

        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def stream():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        stream_task = asyncio.create_task(stream())
        parent = asyncio.current_task()

        async def cancel_parent():
            await started.wait()
            parent.cancel()

        asyncio.create_task(cancel_parent())
        with pytest.raises(asyncio.CancelledError):
            await GatewayRunner._await_stream_task(stream_task)

        assert cancelled.is_set()

    @pytest.mark.asyncio
    async def test_stream_task_wait_preserves_cancellation_when_child_absorbs_it(self):
        from gateway.run import GatewayRunner

        started = asyncio.Event()

        async def stream():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return

        stream_task = asyncio.create_task(stream())
        parent = asyncio.current_task()

        async def cancel_parent():
            await started.wait()
            parent.cancel()

        asyncio.create_task(cancel_parent())
        with pytest.raises(asyncio.CancelledError):
            await GatewayRunner._await_stream_task(stream_task)

    @pytest.mark.asyncio
    async def test_proxy_typing_cancellation_finishes_stream_task(self, monkeypatch):
        from gateway.run import GatewayRunner

        monkeypatch.setenv("GATEWAY_PROXY_URL", "http://127.0.0.1:8642")
        runner, _sentinel = _make_runner()
        source = SessionSource(platform=Platform.MATRIX, chat_id="room", chat_type="group")
        typing_started = asyncio.Event()
        stream_finished = asyncio.Event()

        class Consumer:
            def __init__(self):
                self.task = None
                self.finish = MagicMock(side_effect=stream_finished.set)

            async def run(self):
                self.task = asyncio.current_task()
                await asyncio.Event().wait()

        consumer = Consumer()

        async def send_typing(*_args, **_kwargs):
            typing_started.set()
            await asyncio.Event().wait()

        runner._proxy_stream_consumer = lambda *_args: consumer
        runner._adapter_for_source = lambda _source: SimpleNamespace(send_typing=send_typing)
        runner._thread_metadata_for_source = lambda *_args: None
        runner._run_still_current_fn = lambda *_args: lambda: True
        parent = asyncio.current_task()

        async def cancel_parent():
            await typing_started.wait()
            parent.cancel()

        canceller = asyncio.create_task(cancel_parent())
        try:
            with pytest.raises(asyncio.CancelledError):
                await runner._run_agent_via_proxy(
                    message="hello", context_prompt="", history=[], source=source, session_id="session",
                )
        finally:
            await asyncio.gather(canceller, return_exceptions=True)

        assert stream_finished.is_set()
        assert consumer.task is not None and consumer.task.done()

    @pytest.mark.asyncio
    async def test_outer_finalizer_releases_slot_and_lease_when_durable_clear_is_cancelled(self):
        from gateway.run import GatewayRunner

        runner, sentinel = _make_runner()
        event = _make_event()
        source = event.source
        session_key = "session-key"
        state = SimpleNamespace(turn=SimpleNamespace(lease=None))

        runner._hm_admit_event = AsyncMock(return_value=(event, source, False))
        runner._hm_estop_gate = MagicMock(return_value=None)
        runner._session_key_for_source = MagicMock(return_value=session_key)
        runner._hm_pending_reply_intercepts = AsyncMock(return_value=None)
        runner._hm_evict_idle_stale_agent = MagicMock()
        runner._is_session_running = MagicMock(return_value=False)
        runner._hm_dispatch_idle_commands = AsyncMock(return_value=(False, None))
        runner._is_telegram_topic_root_lobby = MagicMock(return_value=False)
        runner._external_drain_active = False
        runner._claim_active_session_slot = MagicMock(return_value=(None, None))
        runner._hm_rescue_orphaned_fifo = lambda *_args: (event, source, False)
        runner._session_state = MagicMock(return_value=state)
        runner._persist_active_agents = MagicMock()
        runner._begin_session_run_generation = MagicMock(return_value=7)
        runner._handle_message_with_agent = AsyncMock(return_value="ok")
        runner._run_post_turn_hooks = AsyncMock()
        runner._restore_pending_one_turn_model_override = MagicMock()
        runner._clear_durable_active_turn = AsyncMock(side_effect=asyncio.CancelledError)
        runner._release_running_agent_state = MagicMock()
        runner._release_turn_lease = MagicMock()

        with pytest.raises(asyncio.CancelledError):
            await GatewayRunner._handle_message(runner, event)

        assert state.turn.agent is sentinel
        runner._release_running_agent_state.assert_called_once_with(session_key, run_generation=7)
        runner._release_turn_lease.assert_called_once_with(session_key, 7)

    @pytest.mark.asyncio
    async def test_cleanup_releases_session_slot_after_stream_error(self):
        from gateway.run import GatewayRunner

        runner, _sentinel = _make_runner()
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm")
        session_key = build_session_key(source)

        async def stream():
            raise RuntimeError("stream failed")

        stream_task = asyncio.create_task(stream())
        tracking_task = asyncio.create_task(asyncio.sleep(3600))
        runner._release_running_agent_state = MagicMock()
        turn_ctx = SimpleNamespace(
            stream_consumer_holder=[object()], session_key=session_key,
            run_generation=None, streaming_tts_consumer_holder=[None],
        )

        with pytest.raises(RuntimeError, match="stream failed"):
            await GatewayRunner._run_agent_cleanup_turn_tasks(
                runner, turn_ctx, progress_task=None, log_task=None,
                interrupt_monitor=None, _notify_task=None,
                tracking_task=tracking_task, stream_task=stream_task,
            )

        runner._release_running_agent_state.assert_called_once_with(session_key, run_generation=None)

    @pytest.mark.asyncio
    async def test_cleanup_releases_session_slot_when_tts_abort_fails(self):
        from gateway.run import GatewayRunner

        runner, _sentinel = _make_runner()
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm")
        session_key = build_session_key(source)
        tracking_task = asyncio.create_task(asyncio.sleep(3600))
        runner._release_running_agent_state = MagicMock()

        class FailingTTS:
            done = False

            def abort(self, _reason):
                raise RuntimeError("tts abort failed")

            async def wait_complete(self, timeout):
                return None

        turn_ctx = SimpleNamespace(
            stream_consumer_holder=[None], session_key=session_key,
            run_generation=None, streaming_tts_consumer_holder=[FailingTTS()],
        )

        await GatewayRunner._run_agent_cleanup_turn_tasks(
            runner, turn_ctx, progress_task=None, log_task=None,
            interrupt_monitor=None, _notify_task=None,
            tracking_task=tracking_task, stream_task=None,
        )

        runner._release_running_agent_state.assert_called_once_with(session_key, run_generation=None)

    @pytest.mark.asyncio
    async def test_cleanup_releases_session_slot_before_propagating_cancellation(self):
        from gateway.run import GatewayRunner

        runner, _sentinel = _make_runner()
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm")
        session_key = build_session_key(source)
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def stream():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        stream_task = asyncio.create_task(stream())
        runner._release_running_agent_state = MagicMock()
        parent = asyncio.current_task()

        async def cancel_parent():
            await started.wait()
            parent.cancel()

        asyncio.create_task(cancel_parent())
        tracking_task = asyncio.create_task(asyncio.sleep(3600))
        turn_ctx = SimpleNamespace(
            stream_consumer_holder=[object()], session_key=session_key,
            run_generation=None, streaming_tts_consumer_holder=[None],
        )

        with pytest.raises(asyncio.CancelledError):
            await GatewayRunner._run_agent_cleanup_turn_tasks(
                runner, turn_ctx, progress_task=None, log_task=None,
                interrupt_monitor=None, _notify_task=None,
                tracking_task=tracking_task, stream_task=stream_task,
            )

        assert cancelled.is_set()
        runner._release_running_agent_state.assert_called_once_with(session_key, run_generation=None)

    @pytest.mark.asyncio
    async def test_steer_mode_transcribes_voice_before_injection(self, monkeypatch):
        """A busy voice follow-up is transcribed and steered, never queued."""
        import gateway.run as _gr

        monkeypatch.delenv("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED", raising=False)
        monkeypatch.setattr(_gr, "_load_gateway_config", lambda: {})
        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        runner._should_echo_stt_transcripts = MagicMock(return_value=False)
        runner._enrich_message_with_transcription = AsyncMock(
            return_value=('"yönü teknik mimariye çevir"', ["yönü teknik mimariye çevir"])
        )
        adapter = _make_adapter()

        event = _make_event(text="")
        event.message_type = MessageType.VOICE
        event.media_urls = ["/tmp/follow-up.ogg"]
        event.media_types = ["audio/ogg"]
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        runner._running_agents[sk] = agent

        await runner._handle_active_session_busy_message(event, sk)

        runner._enrich_message_with_transcription.assert_awaited_once_with(
            "", ["/tmp/follow-up.ogg"]
        )
        agent.steer.assert_called_once()
        injected = agent.steer.call_args.args[0]
        assert injected.endswith('"yönü teknik mimariye çevir"')
        assert '"chat_id": "123"' in injected
        agent.interrupt.assert_not_called()
        assert sk not in adapter._pending_messages
        content = adapter._send_with_retry.call_args.kwargs["content"]
        assert "Steered" in content
        assert "Queued" not in content


    @pytest.mark.asyncio
    async def test_steer_mode_falls_back_to_queue_when_agent_rejects(self):
        """If agent.steer() returns False, fall back to queue behavior."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        adapter = _make_adapter()

        event = _make_event(text="empty or rejected")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        agent.steer = MagicMock(return_value=False)  # rejected
        runner._running_agents[sk] = agent

        await runner._handle_active_session_busy_message(event, sk)

        agent.steer.assert_called_once()
        agent.interrupt.assert_not_called()
        # Fell back to queue semantics: event was stored for the next turn
        # via the FIFO path (each follow-up its own turn — no newline-merge
        # that would mash separate messages together, #43066).
        assert adapter._pending_messages.get(sk) is event

        # Ack uses queue-mode wording (not steer, not interrupt)
        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content", "")
        assert "Queued for the next turn" in content
        assert "Steered" not in content

    @pytest.mark.asyncio
    async def test_steer_mode_falls_back_to_queue_when_agent_pending(self):
        """If agent is still starting (sentinel), steer mode falls back to queue."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        adapter = _make_adapter()

        event = _make_event(text="arrived too early")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        # Agent is still being set up — sentinel in place
        runner._running_agents[sk] = sentinel

        await runner._handle_active_session_busy_message(event, sk)

        # Event was queued instead of steered (FIFO path, #43066)
        assert adapter._pending_messages.get(sk) is event

        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content", "")
        assert "Queued for the next turn" in content

    @pytest.mark.asyncio
    async def test_interrupt_mode_text_followups_fifo_not_merged(self):
        """Two TEXT follow-ups during a busy turn (interrupt mode) must each
        get their OWN next-turn slot via FIFO — NOT newline-merged into one
        mashed-together turn (#43066 sub-bug 2). Before the fix the
        interrupt/steer-fallback path called merge_pending_message_event
        with merge_text=True, collapsing 'first' and 'second' into
        'first\\nsecond' and destroying message boundaries."""
        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        runner._queued_events = {}
        adapter = _make_adapter()

        # Both events must share the SAME platform object so they resolve to
        # the same adapter (a fresh MagicMock per event would not).
        shared_platform = Platform.TELEGRAM

        def _evt(text):
            src = SessionSource(
                platform=shared_platform, chat_id="123",
                chat_type="dm", user_id="user1",
            )
            return MessageEvent(text=text, message_type=MessageType.TEXT,
                                source=src, message_id=f"m-{text[:5]}")

        first = _evt("first message")
        second = _evt("second message")
        sk = build_session_key(first.source)
        runner.adapters[shared_platform] = adapter

        agent = MagicMock()
        agent._active_children = []  # real list → not demoted to queue
        runner._running_agents[sk] = agent

        await runner._handle_active_session_busy_message(first, sk)
        runner._busy_ack_ts = {}  # avoid the 30s ack-debounce early return
        await runner._handle_active_session_busy_message(second, sk)

        # First lands in the head slot; second goes to the FIFO overflow —
        # they are NOT merged into a single pending event.
        head = adapter._pending_messages.get(sk)
        assert head is first
        assert head.text == "first message"  # not "first message\nsecond message"
        overflow = runner._queued_events.get(sk, [])
        assert [e.text for e in overflow] == ["second message"]


    @pytest.mark.asyncio
    async def test_includes_status_detail_when_opted_in(self, monkeypatch):
        """Ack message should include iteration and tool info when available."""
        import gateway.run as _gr

        monkeypatch.setattr(
            _gr,
            "_load_gateway_config",
            lambda: {"display": {"platforms": {"telegram": {"busy_ack_detail": True}}}},
        )
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event = _make_event(text="yo")
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 21,
            "max_iterations": 60,
            "current_tool": "terminal",
            "last_activity_ts": time.time(),
            "last_activity_desc": "terminal",
            "seconds_since_activity": 0.5,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 600  # 10 min
        runner.adapters[event.source.platform] = adapter

        await runner._handle_active_session_busy_message(event, sk)

        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content", "")
        assert "21/60" in content  # iteration
        assert "terminal" in content  # current tool
        assert "10 min" in content  # elapsed

    @pytest.mark.asyncio
    async def test_status_detail_omits_denominator_for_unbounded_max_iterations(
        self, monkeypatch,
    ):
        """#102806: a top-level session's real max_iterations is sys.maxsize
        (unlimited — see AIAgent's default). The busy-ack must not print that
        literal sentinel as an iteration ceiling."""
        import sys

        import gateway.run as _gr

        monkeypatch.setattr(
            _gr,
            "_load_gateway_config",
            lambda: {"display": {"platforms": {"telegram": {"busy_ack_detail": True}}}},
        )
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event = _make_event(text="yo")
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 3,
            "max_iterations": sys.maxsize,
            "current_tool": "terminal",
            "last_activity_ts": time.time(),
            "last_activity_desc": "terminal",
            "seconds_since_activity": 0.5,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 600
        runner.adapters[event.source.platform] = adapter

        await runner._handle_active_session_busy_message(event, sk)

        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content", "")
        assert "iteration 3" in content
        assert str(sys.maxsize) not in content


class TestBusySessionOnboardingHint:
    """First-touch hint appended to the busy-ack the first time it fires."""

    @pytest.mark.asyncio
    async def test_first_busy_ack_appends_interrupt_hint(self, tmp_path, monkeypatch):
        """First busy-while-running message gets an extra hint about /busy."""
        import gateway.run as _gr

        monkeypatch.setattr(_gr, "_hermes_home", tmp_path)
        # mark_seen imports utils.atomic_yaml_write; make sure it resolves
        # against a writable dir by pointing _hermes_home at tmp_path.
        monkeypatch.setattr(_gr, "_load_gateway_config", lambda: {})

        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event = _make_event(text="ping")
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 3, "max_iterations": 60,
            "current_tool": None, "last_activity_ts": time.time(),
            "last_activity_desc": "api", "seconds_since_activity": 0.1,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 5
        runner.adapters[event.source.platform] = adapter

        await runner._handle_active_session_busy_message(event, sk)

        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content", "")

        # Normal ack body
        assert "Interrupting" in content
        # First-touch hint appended
        assert "First-time tip" in content
        assert "/busy queue" in content

        # The flag is now persisted to tmp_path/config.yaml
        import yaml
        cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert cfg["onboarding"]["seen"]["busy_input_prompt"] is True


class TestLongRunningNotificationOwnership:
    """The long-running heartbeat must stop once its run no longer owns the
    session slot or the executor finished — otherwise a stale
    'running: delegate_task' bubble outlives the run that spawned it (#12029).
    """

    def test_notification_stops_after_session_ownership_moves(self):
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner._running_agents = {}

        original_agent = MagicMock()
        replacement_agent = MagicMock()
        runner._running_agents["sess"] = replacement_agent

        assert runner._should_emit_long_running_notification(
            "sess", original_agent, executor_task=None
        ) is False
