"""Tests for the user-installed GBrain memory provider."""

from types import SimpleNamespace

import pytest


@pytest.fixture
def provider_module():
    import importlib.util
    from pathlib import Path

    path = Path.home() / ".hermes" / "plugins" / "gbrain" / "__init__.py"
    spec = importlib.util.spec_from_file_location("hermes_gbrain_test_provider", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_sanitize_capture_text_redacts_personal_data_and_secrets(provider_module):
    raw = (
        "設計メモ: 週次の売上集計を自動化する。"
        "連絡先 user@example.com、電話 090-1234-5678、郵便番号 100-0001。"
        "OPENAI_API_KEY=sk-test-1234567890abcdef"
    )

    sanitized = provider_module.sanitize_capture_text(raw)

    assert "週次の売上集計を自動化する" in sanitized
    assert "user@example.com" not in sanitized
    assert "090-1234-5678" not in sanitized
    assert "100-0001" not in sanitized
    assert "sk-test-1234567890abcdef" not in sanitized


def test_sanitize_capture_text_redacts_honorific_names(provider_module):
    sanitized = provider_module.sanitize_capture_text("山田太郎さんへの設計確認が完了した")

    assert "山田太郎さん" not in sanitized
    assert "設計確認が完了した" in sanitized


def test_prepare_turn_drops_medical_and_record_data(provider_module):
    assert provider_module.prepare_turn(
        "患者の処方内容と住所を確認して請求する", "対応しました"
    ) is None


def test_sync_turn_writes_a_hashed_conversation_page(provider_module, monkeypatch, tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'{"status":"inserted"}', stderr=b"")

    monkeypatch.setattr(provider_module.subprocess, "run", fake_run)
    provider = provider_module.GBrainMemoryProvider()
    provider.initialize(
        "session-with-sensitive-id",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
    )
    provider._binary = "/usr/local/bin/gbrain"

    provider.sync_turn(
        "請求管理の設計を更新する。連絡先 user@example.com",
        "設計を保存しました。",
        session_id="session-with-sensitive-id",
    )

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:4] == ["/usr/local/bin/gbrain", "capture", "--stdin", "--type"]
    assert args[4] == "conversation"
    assert "--slug" in args
    slug = args[args.index("--slug") + 1]
    assert slug.startswith("conversations/sessions/")
    assert "session-with-sensitive-id" not in slug
    body = kwargs["input"].decode("utf-8")
    assert "請求管理の設計を更新する" in body
    assert "user@example.com" not in body


def test_sync_turn_is_disabled_for_non_primary_context(provider_module, monkeypatch, tmp_path):
    called = False

    def fake_run(*args, **kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(provider_module.subprocess, "run", fake_run)
    provider = provider_module.GBrainMemoryProvider()
    provider.initialize(
        "session",
        hermes_home=str(tmp_path),
        platform="cron",
        agent_context="cron",
    )
    provider.sync_turn("safe design note", "safe response")

    assert called is False


def test_sync_turn_is_disabled_for_private_channel(provider_module, monkeypatch, tmp_path):
    called = False

    def fake_run(*args, **kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(provider_module.subprocess, "run", fake_run)
    provider = provider_module.GBrainMemoryProvider()
    provider.initialize(
        "session",
        hermes_home=str(tmp_path),
        platform="discord",
        chat_name="accounting",
        agent_context="primary",
    )
    provider.sync_turn("安全な設計メモ", "安全な応答")

    assert called is False


def test_capture_turn_preserves_historical_timestamp_and_tool_names(
    provider_module, monkeypatch, tmp_path
):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'{"status":"inserted"}', stderr=b"")

    monkeypatch.setattr(provider_module.subprocess, "run", fake_run)
    provider = provider_module.GBrainMemoryProvider()
    provider.initialize(
        "historical-session",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
    )
    provider._binary = "/usr/local/bin/gbrain"

    captured = provider.capture_turn(
        "安全な設計メモを保存する",
        "設計メモを保存しました。",
        session_id="historical-session",
        tool_names=("recall",),
        event_timestamp=1735689600,
    )

    assert captured is True
    body = calls[0][1]["input"].decode("utf-8")
    assert "- event_at: 2025-01-01T00:00:00Z" in body
    assert "recall" in body
