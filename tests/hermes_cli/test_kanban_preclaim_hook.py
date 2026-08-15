"""Fail-closed pre-claim dispatch gate tests."""

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.plugins import VALID_HOOKS, get_plugin_manager


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def hook_registry():
    manager = get_plugin_manager()
    saved = {name: list(callbacks) for name, callbacks in manager._hooks.items()}
    try:
        yield manager
    finally:
        manager._hooks = saved


def test_preclaim_hook_is_registered():
    assert "pre_kanban_task_claim" in VALID_HOOKS


def test_veto_leaves_task_unclaimed_and_does_not_spawn(
    kanban_home, all_assignees_spawnable, hook_registry,
):
    hook_registry._hooks.setdefault("pre_kanban_task_claim", []).append(
        lambda **kwargs: {"action": "skip", "reason": "provider_auth_unavailable"}
    )
    spawned = []
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="guard me", assignee="worker")
        result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: spawned.append(args))
        task = kb.get_task(conn, task_id)
    finally:
        conn.close()

    assert result.preclaim_guarded == [(task_id, "provider_auth_unavailable")]
    assert spawned == []
    assert task.status == "ready"
    assert task.claim_lock is None


def test_raising_preclaim_hook_fails_closed(
    kanban_home, all_assignees_spawnable, hook_registry,
):
    def broken(**kwargs):
        raise RuntimeError("auth probe crashed")

    hook_registry._hooks.setdefault("pre_kanban_task_claim", []).append(broken)
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="guard me", assignee="worker")
        result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: 123)
        task = kb.get_task(conn, task_id)
    finally:
        conn.close()

    assert result.preclaim_guarded == [(task_id, "preclaim_hook_error")]
    assert task.status == "ready"
    assert task.claim_lock is None


def test_allow_preserves_normal_dispatch(
    kanban_home, all_assignees_spawnable, hook_registry,
):
    hook_registry._hooks.setdefault("pre_kanban_task_claim", []).append(
        lambda task_id, assignee: {"action": "allow"}
    )
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="go", assignee="worker")
        result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: 123)
    finally:
        conn.close()

    assert [row[0] for row in result.spawned] == [task_id]
    assert result.preclaim_guarded == []
