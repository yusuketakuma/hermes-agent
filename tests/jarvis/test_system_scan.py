import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = Path(__file__).parents[3] / "scripts" / "jarvis" / "system_scan.py"
spec = importlib.util.spec_from_file_location("hermes_system_scan", SCRIPT)
scan = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(scan)


def _make_fixture(root: Path) -> None:
    tools = ", ".join(scan.SAFE_GBRAIN_TOOLS)
    for name in scan.EXPECTED_PROFILES:
        home = root if name == "default" else root / "profiles" / name
        (home / "runtime").mkdir(parents=True, exist_ok=True)
        (home / "runtime" / "active_sessions.json").write_text(
            '{"entries": []}\n', encoding="utf-8"
        )
        (home / "SOUL.md").write_text(scan.SUPPORT_MARKER, encoding="utf-8")
        (home / "config.yaml").write_text(
            "mcp_servers:\n"
            "  gbrain:\n"
            f"    tools:\n      include: [{tools}]\n"
            "discord:\n"
            "  bot_conversation:\n"
            "    channel_id: '1548242914268807228'\n",
            encoding="utf-8",
        )
        conn = sqlite3.connect(home / "state.db")
        try:
            conn.execute("CREATE TABLE sessions (started_at INTEGER)")
            conn.execute("INSERT INTO sessions VALUES (strftime('%s','now'))")
            conn.commit()
        finally:
            conn.close()

    (root / "config.yaml").write_text(
        "mcp_servers:\n"
        "  gbrain:\n"
        f"    tools:\n      include: [{tools}]\n"
        "discord:\n"
        "  bot_conversation:\n"
        "    channel_id: '1548242914268807228'\n"
        "profile_routes:\n"
        + "".join(f"  - profile: {name}\n" for name in scan.EXPECTED_PROFILES),
        encoding="utf-8",
    )
    (root / "kanban.db").touch()
    conn = sqlite3.connect(root / "kanban.db")
    try:
        conn.execute("CREATE TABLE tasks (status TEXT)")
        conn.execute("CREATE TABLE task_runs (status TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('done')")
        conn.commit()
    finally:
        conn.close()
    (root / "cron").mkdir()
    (root / "cron" / "jobs.json").write_text('{"jobs": []}\n', encoding="utf-8")
    (root / "state" / "bot-conversation").mkdir(parents=True)
    (root / "state" / "bot-conversation" / "ledger.json").write_text(
        '{"sent": {}, "conversations": {}, "candidates": {}}\n', encoding="utf-8"
    )


def test_scan_is_idle_once_and_deduplicates_hour(tmp_path: Path) -> None:
    _make_fixture(tmp_path)
    now = datetime(2026, 9, 14, 0, 5, tzinfo=timezone.utc)

    first = scan.run(tmp_path, now)
    assert first["wakeAgent"] is True
    assert first["scan"]["gate"] == "idle"
    assert first["scan"]["checks"]["profiles"]["gbrain_ready"] == 10

    second = scan.run(tmp_path, now.replace(minute=55))
    assert second == {"wakeAgent": False, "reason": "duplicate_slot", "slot": "2026-09-14T00:00Z"}
    state = json.loads((tmp_path / "jarvis/state/system_scan.json").read_text())
    assert len(state["history"]) == 1


def test_scan_skips_busy_and_unknown_fail_closed(tmp_path: Path, monkeypatch) -> None:
    _make_fixture(tmp_path)
    busy_home = tmp_path / "profiles" / "ceo" / "runtime" / "active_sessions.json"
    busy_home.write_text('{"entries": [{"pid": 1}]}\n', encoding="utf-8")
    monkeypatch.setattr(scan, "_pid_state", lambda entry: "live")

    busy = scan.run(tmp_path, datetime(2026, 9, 14, 1, 5, tzinfo=timezone.utc))
    assert busy["wakeAgent"] is False
    assert busy["reason"] == "busy"

    busy_home.write_text("not-json\n", encoding="utf-8")
    unknown = scan.run(tmp_path, datetime(2026, 9, 14, 2, 5, tzinfo=timezone.utc))
    assert unknown["wakeAgent"] is False
    assert unknown["reason"] == "unknown"
    serialized = json.dumps(unknown, ensure_ascii=False)
    assert "not-json" not in serialized
