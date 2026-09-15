"""Regression tests: the gateway must preserve a custom provider's
``request_overrides`` on per-turn agent config.

A ``custom_providers`` entry can carry an ``extra_body`` (e.g.
``chat_template_kwargs`` to toggle a local model's thinking).
``resolve_runtime_provider`` surfaces it as ``request_overrides`` on the
resolved runtime dict, but the gateway used to rebuild the runtime from a
fixed key whitelist that omitted it -- so the provider's configured
``extra_body`` never reached the model on the gateway path, and only
``/fast`` service-tier overrides survived.
"""

import pytest
from collections import OrderedDict
from types import SimpleNamespace
from threading import Lock
from unittest.mock import Mock

from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner


PROVIDER_OVERRIDES = {"extra_body": {"chat_template_kwargs": {"enable_thinking": True}}}


def _runtime_kwargs(**extra):
    base = {
        "api_key": "no-key-required",
        "base_url": "http://10.0.0.1:8000/v1",
        "provider": "custom",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
        "max_tokens": None,
    }
    base.update(extra)
    return base


def _runner(service_tier=None):
    runner = object.__new__(GatewayRunner)
    runner._service_tier = service_tier
    return runner


def test_provider_request_overrides_preserved_without_service_tier():
    """No /fast: the provider's extra_body must pass straight through."""
    runner = _runner(service_tier=None)
    rk = _runtime_kwargs(request_overrides=PROVIDER_OVERRIDES)
    route = runner._resolve_turn_agent_config("hi", "main", rk)
    assert route["request_overrides"] == PROVIDER_OVERRIDES
    # A copy, not an alias into runtime_kwargs.
    assert route["request_overrides"] is not rk["request_overrides"]


def test_provider_request_overrides_merged_under_fast_mode(monkeypatch):
    """/fast active: provider extra_body AND the service-tier marker both survive."""
    monkeypatch.setattr(
        "hermes_cli.models.resolve_fast_mode_overrides",
        lambda model_id, **_route: {"service_tier": "priority"},
    )
    runner = _runner(service_tier="priority")
    rk = _runtime_kwargs(request_overrides=PROVIDER_OVERRIDES)
    route = runner._resolve_turn_agent_config("hi", "main", rk)
    assert route["request_overrides"]["extra_body"] == PROVIDER_OVERRIDES["extra_body"]
    assert route["request_overrides"]["service_tier"] == "priority"


def test_no_provider_overrides_yields_empty():
    """Regression: absent provider overrides, behaviour is unchanged ({})."""
    runner = _runner(service_tier=None)
    route = runner._resolve_turn_agent_config("hi", "main", _runtime_kwargs())
    assert route["request_overrides"] == {}


def test_local_turn_policy_uses_loopback_provider_without_paid_fallback(monkeypatch):
    runner = _runner(service_tier=None)
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs_for_provider",
        lambda provider: {
            "api_key": "",
            "base_url": "http://127.0.0.1:11434/v1",
            "provider": "custom",
            "requested_provider": provider,
            "api_mode": "chat_completions",
            "request_overrides": {},
        },
    )
    route = runner._resolve_turn_agent_config(
        "chat",
        "openai/gpt-5.6-luna",
        _runtime_kwargs(),
        turn_policy={
            "route": "local",
            "provider": "custom:mac-mini-ollama",
            "model": "hermes-fallback:latest",
            "enabled_toolsets": ["bot_conversation"],
        },
    )
    assert route["model"] == "hermes-fallback:latest"
    assert route["runtime"]["base_url"] == "http://127.0.0.1:11434/v1"
    assert route["enabled_toolsets"] == ["bot_conversation"]
    assert route["allow_fallback"] is False


def test_local_turn_policy_rejects_non_loopback_endpoint(monkeypatch):
    runner = _runner(service_tier=None)
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs_for_provider",
        lambda _provider: {
            "base_url": "https://example.invalid/v1",
            "provider": "custom",
            "api_mode": "chat_completions",
        },
    )
    with pytest.raises(ValueError, match="loopback"):
        runner._resolve_turn_agent_config(
            "chat",
            "openai/gpt-5.6-luna",
            _runtime_kwargs(),
            turn_policy={
                "route": "local",
                "provider": "custom:mac-mini-ollama",
                "model": "hermes-fallback:latest",
                "enabled_toolsets": ["bot_conversation"],
            },
        )


def test_local_run_skips_normal_provider_resolution(monkeypatch):
    runner = _runner(service_tier=None)
    runner._provider_routing = {}
    normal_resolution = Mock(side_effect=AssertionError("paid provider must not be resolved"))
    runner._resolve_session_agent_runtime = normal_resolution
    runner._resolve_session_reasoning_config = lambda **_kwargs: {}
    runner._resolve_session_service_tier = lambda **_kwargs: None
    route_calls = []

    def resolve_local_route(message, model, runtime_kwargs, *, turn_policy):
        route_calls.append((message, model, runtime_kwargs, turn_policy))
        raise RuntimeError("local provider unavailable")

    runner._resolve_turn_agent_config = resolve_local_route
    ctx = SimpleNamespace(
        source=SimpleNamespace(platform=SimpleNamespace(value="discord")),
        session_key="session", user_config={}, message="chat",
        turn_policy={
            "route": "local", "provider": "custom:mac-mini-ollama",
            "model": "hermes-fallback:latest", "enabled_toolsets": ["bot_conversation"],
        },
    )
    turn_runner = TurnRunner(runner, ctx)
    turn_runner._combined_ephemeral_prompt = lambda: ""
    turn_runner._setup_stream_consumer = lambda _platform: (None, None, None, False)

    result = turn_runner.run_sync()

    assert normal_resolution.call_count == 0
    assert route_calls == [(
        "chat", "hermes-fallback:latest", {}, ctx.turn_policy,
    )]
    assert result["failure_reason"] == "local_route_unavailable"


def test_local_turn_evicts_cached_agent_before_rebuild():
    agent = SimpleNamespace(
        _fallback_chain=[{"provider": "paid", "model": "paid-model"}],
        _fallback_model={"provider": "paid", "model": "paid-model"},
        _fallback_index=1,
        _fallback_activated=True,
    )
    runner = SimpleNamespace(
        _agent_config_signature=lambda *args, **kwargs: "sig",
        _extract_cache_busting_config=lambda _config: {},
        _refresh_fallback_model=Mock(side_effect=AssertionError("local route refreshed fallback")),
        _agent_cache=OrderedDict({"session": (agent, "sig", 0, "session-id")}),
        _agent_cache_lock=Lock(),
        _init_cached_agent_for_turn=Mock(),
        _release_evicted_agent_soft=Mock(),
        _spawn_release_thread=Mock(),
        _enforce_agent_cache_cap=Mock(),
    )
    ctx = SimpleNamespace(
        session_key="session", session_id="session-id", enabled_toolsets=["bot_conversation"],
        source=SimpleNamespace(user_id=None, user_id_alt=None), user_config={},
    )
    turn_runner = TurnRunner(runner, ctx)
    turn_runner._skip_context_files = lambda _platform: ()
    turn_runner._cached_sid_is_dead = lambda _lock, _cache: (None, False)
    turn_runner._current_message_count = lambda: 0
    fresh_agent = object()
    turn_runner._build_fresh_agent = lambda *args, **kwargs: fresh_agent

    resolved, reused = turn_runner._resolve_turn_agent(
        {
            "model": "hermes-fallback:latest",
            "runtime": {"provider": "custom", "base_url": "http://127.0.0.1:11434/v1"},
            "allow_fallback": False,
        },
        "discord", "", 10, {}, {},
    )

    assert resolved is fresh_agent
    assert reused is False
    assert runner._agent_cache["session"][0] is fresh_agent
    assert runner._init_cached_agent_for_turn.call_count == 0
    runner._refresh_fallback_model.assert_not_called()
    runner._spawn_release_thread.assert_called_once()


def test_normal_turn_evicts_signature_mismatch_before_rebuild():
    old_agent = object()
    fresh_agent = object()
    runner = SimpleNamespace(
        _agent_config_signature=lambda *args, **kwargs: "new-sig",
        _extract_cache_busting_config=lambda _config: {},
        _refresh_fallback_model=Mock(),
        _agent_cache=OrderedDict({"session": (old_agent, "old-sig", 0, "session-id")}),
        _agent_cache_lock=Lock(),
        _init_cached_agent_for_turn=Mock(),
        _release_evicted_agent_soft=Mock(),
        _spawn_release_thread=Mock(),
        _enforce_agent_cache_cap=Mock(),
    )
    ctx = SimpleNamespace(
        session_key="session", session_id="session-id", enabled_toolsets=[], source=SimpleNamespace(
            user_id=None, user_id_alt=None,
        ), user_config={}, _interrupt_depth=0,
    )
    turn_runner = TurnRunner(runner, ctx)
    turn_runner._skip_context_files = lambda _platform: ()
    turn_runner._cached_sid_is_dead = lambda _lock, _cache: (None, False)
    turn_runner._current_message_count = lambda: 0
    turn_runner._build_fresh_agent = lambda *args, **kwargs: fresh_agent

    resolved, reused = turn_runner._resolve_turn_agent(
        {"model": "new-model", "runtime": {"provider": "custom"}},
        "discord", "", 10, {}, {},
    )

    assert resolved is fresh_agent
    assert reused is False
    assert runner._agent_cache["session"][0] is fresh_agent
    runner._init_cached_agent_for_turn.assert_not_called()
    runner._spawn_release_thread.assert_called_once()


def test_local_agent_build_failure_is_contained_by_local_route(monkeypatch):
    runner = _runner(service_tier=None)
    runner._provider_routing = {}
    runner._resolve_session_agent_runtime = Mock(
        side_effect=AssertionError("paid provider must not be resolved")
    )
    runner._resolve_session_reasoning_config = lambda **_kwargs: {}
    runner._resolve_session_service_tier = lambda **_kwargs: None
    runner._resolve_turn_agent_config = lambda *args, **kwargs: {
        "model": "hermes-fallback:latest", "runtime": {}, "allow_fallback": False,
    }
    runner._agent_config_signature = lambda *args, **kwargs: "sig"
    runner._extract_cache_busting_config = lambda _config: {}
    runner._agent_cache = OrderedDict()
    runner._agent_cache_lock = Lock()
    runner._session_db = None
    runner._prefill_messages = None
    runner._refresh_fallback_model = Mock(
        side_effect=AssertionError("local route refreshed fallback")
    )
    agent_factory = Mock(side_effect=RuntimeError("local agent unavailable"))
    ctx = SimpleNamespace(
        source=SimpleNamespace(
            platform=SimpleNamespace(value="discord"),
            user_id=None, user_id_alt=None, user_name=None,
            chat_id="chat", chat_name=None, chat_type="group", thread_id=None,
        ),
        session_key="session", session_id="session-id", user_config={}, message="chat",
        enabled_toolsets=["bot_conversation"], disabled_toolsets=None,
        AIAgent=agent_factory,
        turn_policy={
            "route": "local", "provider": "custom:mac-mini-ollama",
            "model": "hermes-fallback:latest", "enabled_toolsets": ["bot_conversation"],
        },
    )
    turn_runner = TurnRunner(runner, ctx)
    turn_runner._combined_ephemeral_prompt = lambda: ""
    turn_runner._setup_stream_consumer = lambda _platform: (None, None, None, False)

    result = turn_runner.run_sync()

    assert result["failure_reason"] == "local_route_unavailable"
    runner._resolve_session_agent_runtime.assert_not_called()
    agent_factory.assert_called_once()
    assert agent_factory.call_args.kwargs["fallback_model"] is None
    runner._refresh_fallback_model.assert_not_called()


@pytest.mark.asyncio
async def test_local_turn_skips_paid_session_hygiene(monkeypatch):
    runner = object.__new__(GatewayRunner)
    monkeypatch.setattr(
        runner, "_hmwa_hygiene_settings",
        lambda *_args, **_kwargs: pytest.fail("paid hygiene resolution reached"),
        raising=False,
    )
    history = [{"role": "user", "content": "chat"}] * 4

    result = await runner._hmwa_run_session_hygiene(
        None, None, None, "session", history, "quick", 1,
        turn_policy={"route": "local"},
    )

    assert result is history


def test_resolve_runtime_agent_kwargs_carries_request_overrides(monkeypatch):
    """The module-level runtime resolver must not drop request_overrides."""
    import gateway.run as gateway_run

    fake_runtime = {
        "api_key": "k",
        "base_url": "http://10.0.0.1:8000/v1",
        "provider": "custom",
        "api_mode": "chat_completions",
        "request_overrides": PROVIDER_OVERRIDES,
    }
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda *a, **k: dict(fake_runtime),
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider._get_model_config", lambda: {}
    )
    rk = gateway_run._resolve_runtime_agent_kwargs()
    assert rk["request_overrides"] == PROVIDER_OVERRIDES


# --- /model session-override follow-up: request_overrides must survive a switch ---

def test_session_override_applies_request_overrides():
    """A /model switch to a custom provider carries its extra_body into runtime."""
    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {
        "sess1": {
            "model": "thinkmodel",
            "provider": "custom",
            "api_key": "k",
            "base_url": "http://10.0.0.1:8000/v1",
            "api_mode": "chat_completions",
            "request_overrides": PROVIDER_OVERRIDES,
        }
    }
    rk = _runtime_kwargs()  # default resolution carried no overrides
    model, out = runner._apply_session_model_override("sess1", "oldmodel", rk)
    assert model == "thinkmodel"
    assert out["request_overrides"] == PROVIDER_OVERRIDES


def test_session_override_clears_stale_request_overrides():
    """Switching to a provider with no overrides clears a stale value."""
    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {
        "sess1": {
            "model": "plain",
            "provider": "openrouter",
            "api_key": "k",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
            "request_overrides": None,
        }
    }
    rk = _runtime_kwargs(request_overrides=PROVIDER_OVERRIDES)  # stale, from default
    _, out = runner._apply_session_model_override("sess1", "old", rk)
    assert out.get("request_overrides") is None


def test_session_override_absent_is_noop():
    """No override for the session leaves runtime_kwargs untouched."""
    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    rk = _runtime_kwargs(request_overrides=PROVIDER_OVERRIDES)
    model, out = runner._apply_session_model_override("nope", "keepme", rk)
    assert model == "keepme"
    assert out["request_overrides"] == PROVIDER_OVERRIDES
