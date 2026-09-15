"""Bounded authority and exactly-once creation for agent coordination."""

from __future__ import annotations

from pathlib import Path
import threading

import pytest


@pytest.fixture
def kanban_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    path = kbc.init_db(tmp_path / "kanban.db")
    conn = kbc.connect(path)
    try:
        yield conn, path
    finally:
        conn.close()


def test_root_scope_is_required_for_agent_children(kanban_db):
    conn, _ = kanban_db
    from hermes_cli import kanban_db as kb

    root = kb.create_task(conn, title="root", assignee="planner")
    assert kb.get_task(conn, root).execution_scope["max_children"] == 0
    with pytest.raises(ValueError, match="allowed_assignees"):
        kb.create_task(conn, title="child", assignee="engineer", creator_task_id=root)


def test_assignment_cannot_widen_scope_and_triage_binding_is_one_time(kanban_db):
    conn, _ = kanban_db
    from hermes_cli import kanban_db as kb

    scoped = kb.create_task(
        conn, title="scoped", assignee="planner",
        execution_scope={"allowed_assignees": ["planner", "engineer"]},
    )
    with pytest.raises(RuntimeError, match="outside execution_scope"):
        kb.assign_task(conn, scoped, "finance")
    assert kb.get_task(conn, scoped).assignee == "planner"

    triage = kb.create_task(conn, title="triage", triage=True)
    assert kb.specify_triage_task(conn, triage, assignee="engineer")
    assert kb.get_task(conn, triage).execution_scope["allowed_assignees"] == ["engineer"]


def test_dispatcher_worker_cannot_use_operator_scope_rebind(kanban_db, monkeypatch):
    conn, _ = kanban_db
    from hermes_cli import kanban_db as kb

    task_id = kb.create_task(
        conn, title="handoff", assignee="planner",
        execution_scope={"allowed_assignees": ["planner", "engineer"]},
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    with pytest.raises(PermissionError, match="cannot widen execution_scope"):
        kb.assign_task(conn, task_id, "finance", allow_scope_rebind=True)
    task = kb.get_task(conn, task_id)
    assert task.assignee == "planner"
    assert task.execution_scope["allowed_assignees"] == ["planner", "engineer"]


def test_dispatcher_worker_cannot_handoff_review_outside_scope(kanban_db, monkeypatch):
    conn, _ = kanban_db
    from hermes_cli import kanban_db as kb

    task_id = kb.create_task(
        conn, title="review handoff", assignee="planner",
        execution_scope={"allowed_assignees": ["planner", "engineer"]},
    )
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    ok, reason = kb.request_review(
        conn, task_id, summary="ready", reviewer="finance",
        expected_run_id=claimed.current_run_id, with_reason=True,
    )
    assert ok is False
    assert "outside execution_scope" in reason
    task = kb.get_task(conn, task_id)
    assert task.assignee == "planner"
    assert task.status == "running"


def test_legacy_scope_less_task_is_not_autonomously_spawned(kanban_db, monkeypatch):
    conn, _ = kanban_db
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    task_id = kb.create_task(conn, title="legacy", assignee="engineer")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET execution_scope = NULL WHERE id = ?", (task_id,))
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    result = kbd.dispatch_once(conn, spawn_fn=lambda *_args, **_kwargs: pytest.fail("spawned"))

    assert result.spawned == []
    assert kb.get_task(conn, task_id).status == "blocked"
    assert any(event.kind == "gave_up" for event in kb.list_events(conn, task_id))


def test_malformed_scope_is_not_autonomously_spawned(kanban_db, monkeypatch):
    conn, _ = kanban_db
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    task_id = kb.create_task(conn, title="malformed", assignee="engineer")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET execution_scope = ? WHERE id = ?",
            ('{"version": 1, "allowed_assignees": "engineer"}', task_id),
        )
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    result = kbd.dispatch_once(conn, spawn_fn=lambda *_args, **_kwargs: pytest.fail("spawned"))

    assert result.spawned == []
    assert kb.get_task(conn, task_id).status == "blocked"


def test_child_scope_cannot_widen_authority(kanban_db):
    conn, _ = kanban_db
    from hermes_cli import kanban_db as kb

    root = kb.create_task(
        conn, title="root", assignee="planner",
        execution_scope={
            "allowed_assignees": ["planner", "engineer"],
            "allowed_workspace_kinds": ["scratch"],
            "max_children": 1, "max_descendants": 2,
            "max_runtime_seconds": 30, "max_retries": 2,
        },
    )
    assert kb.get_task(conn, root).max_runtime_seconds == 30
    assert kb.get_task(conn, root).max_retries == 2
    child = kb.create_task(conn, title="child", assignee="engineer", creator_task_id=root)
    assert kb.get_task(conn, child).max_runtime_seconds == 30
    assert kb.get_task(conn, child).max_retries == 2
    assert kb.get_task(conn, child).creator_task_id == root
    with pytest.raises(ValueError, match="assignee"):
        kb.create_task(conn, title="outside", assignee="finance", creator_task_id=root)
    with pytest.raises(ValueError, match="max_children"):
        kb.create_task(conn, title="too many", assignee="engineer", creator_task_id=root)


def test_child_scope_cannot_drop_a_pinned_model_route(kanban_db):
    conn, _ = kanban_db
    from hermes_cli import kanban_db as kb

    root = kb.create_task(
        conn, title="pinned root", assignee="planner",
        model_override="model-a", provider_override="provider-a",
        execution_scope={
            "allowed_assignees": ["planner", "engineer"],
            "allowed_model_routes": [{"model": "model-a", "provider": "provider-a"}],
            "max_children": 2, "max_descendants": 2,
        },
    )
    child = kb.create_task(conn, title="inherited route", assignee="engineer", creator_task_id=root)
    assert kb.get_task(conn, child).execution_scope["allowed_model_routes"] == [
        {"model": "model-a", "provider": "provider-a"},
    ]
    with pytest.raises(ValueError, match="narrowing"):
        kb.create_task(
            conn, title="unrestricted child", assignee="engineer", creator_task_id=root,
            execution_scope={"allowed_model_routes": []},
        )


def test_idempotency_is_atomic_across_connections(kanban_db):
    _, path = kanban_db
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    barrier = threading.Barrier(2)
    ids, errors = [], []

    def create():
        conn = kbc.connect(path)
        try:
            barrier.wait()
            ids.append(kb.create_task(
                conn, title="same request", assignee="planner",
                idempotency_key="request-1",
            ))
        except Exception as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(ids) == 2 and ids[0] == ids[1]
    conn = kbc.connect(path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?", ("request-1",)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_child_dependencies_cannot_cross_coordination_roots(kanban_db):
    conn, _ = kanban_db
    from hermes_cli import kanban_db as kb

    root = kb.create_task(
        conn, title="root", assignee="planner",
        execution_scope={
            "allowed_assignees": ["planner", "engineer"],
            "max_children": 1, "max_descendants": 2,
        },
    )
    outside = kb.create_task(conn, title="outside", assignee="planner")
    with pytest.raises(ValueError, match="coordination root"):
        kb.create_task(
            conn, title="child", assignee="engineer", creator_task_id=root,
            parents=[outside],
        )


def test_child_idempotency_key_cannot_cross_coordination_roots(kanban_db):
    conn, _ = kanban_db
    from hermes_cli import kanban_db as kb

    roots = [
        kb.create_task(
            conn, title=f"root-{i}", assignee="planner",
            execution_scope={
                "allowed_assignees": ["planner", "engineer"],
                "max_children": 1, "max_descendants": 1,
            },
        )
        for i in range(2)
    ]
    kb.create_task(
        conn, title="child", assignee="engineer", creator_task_id=roots[0],
        idempotency_key="same-child-request",
    )
    with pytest.raises(ValueError, match="different coordination root"):
        kb.create_task(
            conn, title="other child", assignee="engineer", creator_task_id=roots[1],
            idempotency_key="same-child-request",
        )


def test_review_required_needs_configured_independent_receipt(kanban_db):
    conn, _ = kanban_db
    from hermes_cli import kanban_db as kb

    task_id = kb.create_task(
        conn, title="review me", assignee="engineer",
        execution_scope={
            "allowed_assignees": ["engineer", "cro"],
            "max_children": 0, "max_descendants": 0,
            "review_required": True, "reviewer": "cro",
        },
    )
    kb.claim_task(conn, task_id, claimer="host:engineer")
    run_id = kb.get_task(conn, task_id).current_run_id
    assert kb.request_review(
        conn, task_id, summary="implemented", expected_run_id=run_id,
    )
    kb.claim_review_task(conn, task_id, claimer="host:cro")
    review_run_id = kb.get_task(conn, task_id).current_run_id
    with pytest.raises(kb.ReviewGateError):
        kb.complete_task(conn, task_id, summary="not reviewed", metadata={}, expected_run_id=review_run_id)
    assert kb.complete_task(
        conn, task_id, summary="approved",
        metadata={"review_verdict": "pass", "review_scope_version": 1},
        expected_run_id=review_run_id,
    )


def test_explicit_review_handoff_records_its_reviewer_scope(kanban_db):
    conn, _ = kanban_db
    from hermes_cli import kanban_db as kb

    task_id = kb.create_task(
        conn, title="review boundary", assignee="engineer",
        execution_scope={"allowed_assignees": ["engineer", "cro"]},
    )
    assert kb.request_review(conn, task_id, summary="ready", reviewer="finance")
    task = kb.get_task(conn, task_id)
    assert task.status == "review"
    assert task.execution_scope["allowed_assignees"] == ["engineer", "cro", "finance"]


def test_manual_review_receipt_needs_configured_reviewer(kanban_db):
    conn, _ = kanban_db
    from hermes_cli import kanban_db as kb

    task_id = kb.create_task(
        conn, title="manual review", assignee="engineer",
        execution_scope={
            "allowed_assignees": ["engineer", "cro"],
            "max_children": 0, "max_descendants": 0,
            "review_required": True, "reviewer": "cro",
        },
    )
    assert kb.request_review(conn, task_id, summary="implemented", reviewer="cro")
    with pytest.raises(kb.ReviewGateError, match="metadata.reviewer"):
        kb.complete_task(
            conn, task_id, summary="approved",
            metadata={"review_verdict": "pass", "review_scope_version": 1},
        )
    assert kb.complete_task(
        conn, task_id, summary="approved",
        metadata={
            "review_verdict": "pass", "review_scope_version": 1, "reviewer": "cro",
        },
    )
