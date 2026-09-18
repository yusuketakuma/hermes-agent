"""Tests for the one-shot Hermes history importer."""

import importlib.util
import sqlite3
import sys
from pathlib import Path


def _load_importer():
    path = Path.home() / ".hermes" / "plugins" / "gbrain" / "import_hermes_history.py"
    spec = importlib.util.spec_from_file_location("hermes_gbrain_history_import_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_history_db(tmp_path):
    path = tmp_path / "state.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            display_name TEXT,
            origin_json TEXT,
            archived INTEGER DEFAULT 0,
            started_at REAL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            active INTEGER DEFAULT 1
        );
        """
    )
    con.executemany(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, 0, ?)",
        [
            ("safe-session", "cli", None, None, 10.0),
            ("private-session", "discord", "openclaw / #accounting", None, 20.0),
            ("internal-session", "cron", None, None, 30.0),
        ],
    )
    con.executemany(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        [
            (1, "safe-session", "user", "一般設計を保存する", None, None, 10.0),
            (2, "safe-session", "assistant", "", '[{"function":{"name":"recall"}}]', None, 11.0),
            (3, "safe-session", "tool", "秘密のツール結果", None, "recall", 12.0),
            (4, "safe-session", "assistant", "設計メモを保存しました", None, None, 13.0),
            (5, "private-session", "user", "請求書の明細を確認する", None, None, 20.0),
            (6, "private-session", "assistant", "確認しました", None, None, 21.0),
            (7, "internal-session", "user", "内部ジョブ", None, None, 30.0),
            (8, "internal-session", "assistant", "完了", None, None, 31.0),
        ],
    )
    con.commit()
    con.close()
    return path


def test_iter_history_turns_pairs_final_assistant_and_ignores_internal_sources(tmp_path):
    importer = _load_importer()
    path = _make_history_db(tmp_path)

    turns = list(importer.iter_history_turns(path))

    assert len(turns) == 2
    safe = next(turn for turn in turns if turn.session_id == "safe-session")
    assert safe.user_content == "一般設計を保存する"
    assert safe.assistant_content == "設計メモを保存しました"
    assert safe.tool_names == ("recall",)
    assert any(turn.session_id == "private-session" for turn in turns)
    assert all(turn.session_id != "internal-session" for turn in turns)


def test_import_history_applies_provider_privacy_boundary(tmp_path, monkeypatch):
    importer = _load_importer()
    path = _make_history_db(tmp_path)
    calls = []

    class FakeProvider:
        def initialize(self, session_id, **kwargs):
            self.session_id = session_id
            self.chat_name = kwargs.get("chat_name", "")

        def capture_turn(self, *args, **kwargs):
            calls.append((self.session_id, self.chat_name, args, kwargs))
            return True

    monkeypatch.setattr(importer, "GBrainMemoryProvider", FakeProvider)
    stats = importer.import_history(path, apply=True)

    assert stats.captured == 1
    assert stats.skipped_private == 1
    assert stats.skipped_sensitive == 0
    assert len(calls) == 1
    assert calls[0][0] == "safe-session"
    assert "秘密のツール結果" not in str(calls[0])
