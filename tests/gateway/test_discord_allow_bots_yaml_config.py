"""config.yaml `discord.allow_bots` must reach the adapter's bot gate.

Invariant: every Discord allowlist gate configurable in `config.yaml` resolves through
`PlatformConfig.extra` even when no `DISCORD_*` env var is set. `allow_bots` regressed
because `_get_allow_bots()` read env only, so a YAML-configured value was silently
ignored and bot-to-bot handoffs stayed blocked.
"""

import importlib
import sys
import types

import pytest


@pytest.fixture
def adapter_mod():
    sys.modules.pop("plugins.platforms.discord.adapter", None)
    return importlib.import_module("plugins.platforms.discord.adapter")


def _seed(adapter_mod, yaml_cfg, discord_cfg):
    return adapter_mod._apply_yaml_config(yaml_cfg, discord_cfg)


def test_yaml_allow_bots_is_seeded_into_extra(adapter_mod, monkeypatch):
    monkeypatch.delenv("DISCORD_ALLOW_BOTS", raising=False)
    seeded = _seed(adapter_mod, {}, {"allow_bots": "mentions"})
    assert seeded is not None
    assert seeded.get("allow_bots") == "mentions"


def _fake_adapter(adapter_mod, extra, env_overrides=None):
    """A DiscordAdapter shell with only the gate-resolution state populated."""
    adapter = object.__new__(adapter_mod.DiscordAdapter)
    adapter.config = types.SimpleNamespace(extra=extra)
    snapshot = {key: "" for key in adapter_mod._GATE_ENV_KEYS}
    snapshot.update(env_overrides or {})
    adapter._gate_env_snapshot = snapshot
    return adapter


def test_adapter_reads_allow_bots_from_extra_without_env(adapter_mod, monkeypatch):
    monkeypatch.delenv("DISCORD_ALLOW_BOTS", raising=False)
    monkeypatch.setattr(adapter_mod, "_scoped_gate_env", lambda name, default="": default)

    adapter = _fake_adapter(adapter_mod, {"allow_bots": "mentions"})

    assert adapter._get_allow_bots() == "mentions"


def test_discord_bot_conversation_settings_reach_platform_extra(tmp_path, monkeypatch):
    from gateway.config import Platform, load_gateway_config

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "discord:\n"
        "  no_thread_channels: ['1548242914268807228']\n"
        "  bot_conversation:\n"
        "    channel_id: '1548242914268807228'\n"
        "  server_targets:\n"
        "    - guild_id: '1499166670739083458'\n"
        "      channel_ids: ['1548242914268807228']\n"
        "  channel_prompts:\n"
        "    '1548242914268807228': 'structured bot chat only'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    extra = load_gateway_config().platforms[Platform.DISCORD].extra

    assert extra["no_thread_channels"] == "1548242914268807228"
    assert extra["bot_conversation"]["channel_id"] == "1548242914268807228"
    assert extra["server_targets"][0]["guild_id"] == "1499166670739083458"
    assert extra["channel_prompts"]["1548242914268807228"] == "structured bot chat only"


def test_bot_conversation_admission_requires_live_hermes_identity(adapter_mod, monkeypatch):
    from types import SimpleNamespace
    from gateway.config import Platform

    own_user = SimpleNamespace(id="100", bot=True)
    live_peer = SimpleNamespace(id="200", bot=True)

    class _Dedup:
        def is_duplicate(self, _message_id):
            return False

        def contains(self, _message_id):
            return False

    def make_adapter():
        adapter = object.__new__(adapter_mod.DiscordAdapter)
        adapter._client = SimpleNamespace(user=own_user)
        adapter._owner_profile = "cto"
        adapter._dedup = _Dedup()
        adapter.config = SimpleNamespace(extra={
            "bot_conversation": {"channel_id": "1548242914268807228"},
        })
        adapter.gateway_runner = SimpleNamespace(
            adapters={Platform.DISCORD: SimpleNamespace(_client=SimpleNamespace(user=live_peer))},
            _profile_adapters={},
        )
        return adapter

    monkeypatch.setattr(adapter_mod, "_scoped_gate_env", lambda _name, default="": default)

    def message(author_id, channel_id="1548242914268807228", content=None, parent_id=None):
        author = SimpleNamespace(id=author_id, bot=True)
        channel = SimpleNamespace(id=channel_id, parent_id=parent_id)
        return SimpleNamespace(
            id=f"message-{author_id}-{channel_id}",
            author=author,
            type=adapter_mod.discord.MessageType.default,
            channel=channel,
            mentions=[own_user],
            content=content or (
                f"<@{own_user.id}> 🧭 HERMES-BOT-CHAT v1\n"
                "type=CHAT from=cos to=cto\n"
                "coordination=synthetic\n"
                "summary=structured handoff"
            ),
            guild=SimpleNamespace(id="1499166670739083458"),
        )

    adapter = make_adapter()
    monkeypatch.setattr(adapter, "_get_allow_bots", lambda: "mentions")
    monkeypatch.setattr(adapter, "_discord_bots_require_inline_mention", lambda: True)

    assert adapter._discord_message_admission(message("200"), claim=True) == (True, False)
    assert adapter._discord_message_admission(message("999"), claim=True) == (False, False)
    assert adapter._discord_message_admission(
        message("200", "1548342405038743774", parent_id="1548242914268807228"), claim=True
    ) == (False, False)


def test_bot_conversation_admission_rejects_unstructured_or_mismatched_envelopes(adapter_mod, monkeypatch):
    from types import SimpleNamespace
    from gateway.config import Platform

    own_user = SimpleNamespace(id="100", bot=True)
    live_peer = SimpleNamespace(id="200", bot=True)

    class _Dedup:
        def is_duplicate(self, _message_id):
            return False

        def contains(self, _message_id):
            return False

    adapter = object.__new__(adapter_mod.DiscordAdapter)
    adapter._client = SimpleNamespace(user=own_user)
    adapter._owner_profile = "cto"
    adapter._dedup = _Dedup()
    adapter.config = SimpleNamespace(extra={
        "bot_conversation": {"channel_id": "1548242914268807228"},
    })
    adapter.gateway_runner = SimpleNamespace(
        adapters={Platform.DISCORD: SimpleNamespace(_client=SimpleNamespace(user=live_peer))},
        _profile_adapters={},
    )
    monkeypatch.setattr(adapter, "_get_allow_bots", lambda: "mentions")
    monkeypatch.setattr(adapter, "_discord_bots_require_inline_mention", lambda: True)

    def message(content):
        return SimpleNamespace(
            id="message-1", author=live_peer,
            type=adapter_mod.discord.MessageType.default,
            channel=SimpleNamespace(id="1548242914268807228", parent_id=None),
            mentions=[own_user], content=content,
            guild=SimpleNamespace(id="1499166670739083458"),
        )

    valid = (
        "<@100> 🧭 HERMES-BOT-CHAT v1\n"
        "type=CHAT from=cos to=cto\n"
        "coordination=synthetic\nsummary=hello"
    )
    assert adapter._discord_message_admission(message("<@100> raw text"), claim=True) == (False, False)
    assert adapter._discord_message_admission(
        message(valid.replace("from=cos", "from=cfo")), claim=True
    ) == (False, False)
    assert adapter._discord_message_admission(
        message(valid.replace("to=cto", "to=cro")), claim=True
    ) == (False, False)
    assert adapter._discord_message_admission(
        message(valid + "\nextra=<@300>"), claim=True
    ) == (False, False)
    assert adapter._discord_message_admission(
        message(valid.replace("<@100>", "<@100> <@100>")), claim=True
    ) == (False, False)
    assert adapter._discord_message_admission(
        message(valid + " @everyone"), claim=True
    ) == (False, False)
    assert adapter._discord_message_admission(
        message(valid.replace("summary=hello", "summary=hello <@&300>")), claim=True
    ) == (False, False)
    assert adapter._discord_message_admission(
        message(valid.replace("summary=hello", "summary=" + "x" * 241)), claim=True
    ) == (False, False)
    assert adapter._discord_message_admission(
        message(valid), claim=True
    ) == (True, False)


def test_bot_conversation_admission_accepts_japanese_labels_and_rejects_alias_duplicates(
    adapter_mod, monkeypatch
):
    from types import SimpleNamespace
    from gateway.config import Platform

    own_user = SimpleNamespace(id="100", bot=True)
    live_peer = SimpleNamespace(id="200", bot=True)

    class _Dedup:
        def is_duplicate(self, _message_id):
            return False

        def contains(self, _message_id):
            return False

    adapter = object.__new__(adapter_mod.DiscordAdapter)
    adapter._client = SimpleNamespace(user=own_user)
    adapter._owner_profile = "cto"
    adapter._dedup = _Dedup()
    adapter.config = SimpleNamespace(extra={
        "bot_conversation": {"channel_id": "1548242914268807228"},
    })
    adapter.gateway_runner = SimpleNamespace(
        adapters={Platform.DISCORD: SimpleNamespace(_client=SimpleNamespace(user=live_peer))},
        _profile_adapters={},
    )
    monkeypatch.setattr(adapter, "_get_allow_bots", lambda: "mentions")
    monkeypatch.setattr(adapter, "_discord_bots_require_inline_mention", lambda: True)

    def message(content):
        return SimpleNamespace(
            id="message-japanese", author=live_peer,
            type=adapter_mod.discord.MessageType.default,
            channel=SimpleNamespace(id="1548242914268807228", parent_id=None),
            mentions=[own_user], content=content,
            guild=SimpleNamespace(id="1499166670739083458"),
        )

    valid = (
        "<@100> 🧭 HERMES-BOT-CHAT v1\n"
        "種別=CHAT 送信元=cos 宛先=cto\n"
        "話題ID=synthetic\n"
        "要約=わかりやすい共有です。"
    )
    assert adapter._discord_message_admission(message(valid), claim=True) == (True, False)
    assert adapter._discord_message_admission(
        message(valid + "\nsummary=duplicate"), claim=True
    ) == (False, False)


def test_internal_bot_sender_allowlists_only_the_envelope_recipient(adapter_mod):
    valid = "<@100> 🧭 HERMES-BOT-CHAT v1\ntype=CHAT from=cos to=cto\ncoordination=x\nsummary=hello"
    assert adapter_mod._standalone_bot_conversation_allowed_mentions(valid) == {
        "parse": [], "users": ["100"], "roles": [], "replied_user": False,
    }
    assert adapter_mod._standalone_bot_conversation_allowed_mentions(valid + " @everyone") is None
    assert adapter_mod._standalone_bot_conversation_allowed_mentions(
        valid.replace("<@100>", "<@100> <@200>")
    ) is None


def test_bot_conversation_admission_rejects_attachments_and_reply_targets(adapter_mod, monkeypatch):
    from types import SimpleNamespace
    from gateway.config import Platform

    own_user = SimpleNamespace(id="100", bot=True)
    live_peer = SimpleNamespace(id="200", bot=True)

    class _Dedup:
        def is_duplicate(self, _message_id):
            return False

        def contains(self, _message_id):
            return False

    adapter = object.__new__(adapter_mod.DiscordAdapter)
    adapter._client = SimpleNamespace(user=own_user)
    adapter._owner_profile = "cto"
    adapter._dedup = _Dedup()
    adapter.config = SimpleNamespace(extra={
        "bot_conversation": {"channel_id": "1548242914268807228"},
    })
    adapter.gateway_runner = SimpleNamespace(
        adapters={Platform.DISCORD: SimpleNamespace(_client=SimpleNamespace(user=live_peer))},
        _profile_adapters={},
    )
    monkeypatch.setattr(adapter, "_get_allow_bots", lambda: "mentions")
    monkeypatch.setattr(adapter, "_discord_bots_require_inline_mention", lambda: True)
    content = (
        "<@100> 🧭 HERMES-BOT-CHAT v1\n"
        "type=CHAT from=cos to=cto\n"
        "coordination=synthetic\nsummary=hello"
    )

    def message(**extra):
        message_content = extra.pop("content", content)
        return SimpleNamespace(
            id="message-attachments", author=live_peer,
            type=adapter_mod.discord.MessageType.default,
            channel=SimpleNamespace(id="1548242914268807228", parent_id=None),
            mentions=[own_user], content=message_content,
            guild=SimpleNamespace(id="1499166670739083458"), **extra,
        )

    assert adapter._discord_message_admission(message(attachments=[SimpleNamespace()]), claim=True) == (False, False)
    assert adapter._discord_message_admission(
        message(message_snapshots=[SimpleNamespace()]), claim=True
    ) == (False, False)
    assert adapter._discord_message_admission(
        message(reference=SimpleNamespace(message_id="source")), claim=True
    ) == (False, False)
    assert adapter._discord_message_admission(
        message(embeds=[SimpleNamespace()]), claim=True
    ) == (False, False)
    assert adapter._discord_message_admission(
        message(content=content + "\nsource_refs=" + "x" * 141), claim=True
    ) == (False, False)
