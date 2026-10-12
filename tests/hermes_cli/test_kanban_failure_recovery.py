"""First-failure continuation admission uses the existing structured run history."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


def close_attempt(conn, task_id, outcome):
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    kb._end_run(conn, task_id, outcome=outcome, summary="Verified item A; step B remains",
                error="An arbitrary message that contains no failure classification",
                expected_run_id=claimed.current_run_id)
    return claimed.current_run_id


def recovery_section(context):
    return next((s for s in context.split("## ") if s.startswith("Recovery before execution\n")), None)


@pytest.mark.parametrize("outcome", ["timed_out", "crashed", "spawn_failed", "reclaimed", "gave_up", "blocked"])
def test_first_failed_run_enters_recovery_with_exact_receipts(board, monkeypatch, outcome):
    task_id = kb.create_task(board, title="Synthetic packing task", assignee="worker", max_iterations=7)
    run_id = close_attempt(board, task_id, outcome)
    calls = []
    original = kb.list_runs

    def read_runs(conn, tid):
        calls.append(tid)
        return original(conn, tid)

    monkeypatch.setattr(kb, "list_runs", read_runs)
    context = kb.build_worker_context(board, task_id)
    section = recovery_section(context)
    assert section is not None
    assert str(run_id) in section and outcome in section
    assert "Verified item A; step B remains" in context
    assert calls == [task_id]
    assert len(section) < 1800


@pytest.mark.parametrize("outcome", [None, "completed", "review_requested", "dependency_wait"])
def test_fresh_or_normal_handoff_does_not_enter_recovery(board, outcome):
    task_id = kb.create_task(board, title="Adjacent packing task", assignee="worker")
    if outcome:
        close_attempt(board, task_id, outcome)
    assert recovery_section(kb.build_worker_context(board, task_id)) is None
