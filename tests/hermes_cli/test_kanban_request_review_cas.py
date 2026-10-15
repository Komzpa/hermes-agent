"""``request_review`` origin CAS: ``expected_status`` and ``expected_event_id``.

Defaults keep the original contract: only ``running``/``ready`` may move to
``review``. ``expected_status`` admits one more origin (``ready``, ``blocked``,
``todo`` or ``triage``), and only while the task holds no claim.
``expected_event_id`` refuses the transition once the task's event log has
moved. Both run under the same CAS as before and must work inside an enclosing
``write_txn``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0]


def _max_event_id(conn, tid):
    return conn.execute(
        "SELECT MAX(id) FROM task_events WHERE task_id = ?", (tid,),
    ).fetchone()[0]


def _set_origin(conn, tid, status, *, claimed=False):
    conn.execute(
        "UPDATE tasks SET status = ?, claim_lock = ?, worker_pid = ? WHERE id = ?",
        (status, "lock-x" if claimed else None, 4242 if claimed else None, tid),
    )


def test_default_refuses_blocked_origin(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="blocked", assignee="worker")
        _set_origin(conn, tid, "blocked")
        assert kb.request_review(conn, tid, reviewer="reviewer") is False
        assert _status(conn, tid) == "blocked"


def test_default_refuses_triage_origin(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="triage", assignee="worker")
        _set_origin(conn, tid, "triage")
        assert kb.request_review(conn, tid, reviewer="reviewer") is False
        assert _status(conn, tid) == "triage"


def test_expected_status_blocked_admits_unclaimed_blocked(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="blocked", assignee="worker")
        _set_origin(conn, tid, "blocked")
        ok = kb.request_review(
            conn, tid, reviewer="reviewer", expected_status="blocked",
        )
        assert ok is True
        assert _status(conn, tid) == "review"
        rr = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'review_requested'",
            (tid,),
        ).fetchone()[0]
        assert rr == 1


def test_expected_status_triage_admits_unclaimed_triage(kanban_home: Path) -> None:
    """Plugin recovery reconcile admits cards from ``triage`` (fork RECOVERY_HEAD behaviour)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="triage", assignee="worker")
        _set_origin(conn, tid, "triage")
        ok = kb.request_review(
            conn, tid, reviewer="reviewer", expected_status="triage",
        )
        assert ok is True
        assert _status(conn, tid) == "review"


def test_expected_status_triage_refuses_claimed_triage(kanban_home: Path) -> None:
    """CAS miss: a claimed triage card is refused and stays in triage."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="claimed triage", assignee="worker")
        _set_origin(conn, tid, "triage", claimed=True)
        ok = kb.request_review(
            conn, tid, reviewer="reviewer", expected_status="triage",
        )
        assert ok is False
        assert _status(conn, tid) == "triage"
        assert _events(conn, tid, "review_requested") == 0


def test_expected_status_blocked_refuses_claimed_blocked(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="claimed", assignee="worker")
        _set_origin(conn, tid, "blocked", claimed=True)
        ok = kb.request_review(
            conn, tid, reviewer="reviewer", expected_status="blocked",
        )
        assert ok is False
        assert _status(conn, tid) == "blocked"


def test_expected_status_todo_admits_unclaimed_todo(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="todo", assignee="worker")
        _set_origin(conn, tid, "todo")
        assert kb.request_review(
            conn, tid, reviewer="reviewer", expected_status="todo",
        ) is True
        assert _status(conn, tid) == "review"


def test_expected_status_does_not_admit_other_origins(kanban_home: Path) -> None:
    """CAS miss: origin status differs from ``expected_status``."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="done", assignee="worker")
        _set_origin(conn, tid, "done")
        assert kb.request_review(
            conn, tid, reviewer="reviewer", expected_status="blocked",
        ) is False
        assert _status(conn, tid) == "done"


def test_expected_event_id_stale_refuses(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="moved", assignee="worker")
        _set_origin(conn, tid, "blocked")
        seen = _max_event_id(conn, tid)
        kb._append_event(conn, tid, "note", {"why": "log moved"})
        ok = kb.request_review(
            conn, tid, reviewer="reviewer",
            expected_status="blocked", expected_event_id=seen,
        )
        assert ok is False
        assert _status(conn, tid) == "blocked"


def test_expected_event_id_current_admits(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="stable", assignee="worker")
        _set_origin(conn, tid, "blocked")
        kb._append_event(conn, tid, "note", {"why": "before"})
        seen = _max_event_id(conn, tid)
        ok = kb.request_review(
            conn, tid, reviewer="reviewer",
            expected_status="blocked", expected_event_id=seen,
        )
        assert ok is True
        assert _status(conn, tid) == "review"


def test_request_review_works_inside_outer_write_txn(kanban_home: Path) -> None:
    """``request_review`` opts into nesting, so an outer ``write_txn`` is safe."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="nested", assignee="worker")
        _set_origin(conn, tid, "blocked")
        with kbc.write_txn(conn):
            ok = kb.request_review(
                conn, tid, reviewer="reviewer", expected_status="blocked",
            )
            assert ok is True
            assert _status(conn, tid) == "review"
        assert _status(conn, tid) == "review"


def test_nested_request_review_rolls_back_with_outer_txn(kanban_home: Path) -> None:
    """Inside an outer txn the review write is durable only if the outer commits."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rollback", assignee="worker")
        _set_origin(conn, tid, "blocked")
        conn.commit()
        with pytest.raises(RuntimeError, match="rollback-me"):
            with kbc.write_txn(conn):
                assert kb.request_review(
                    conn, tid, reviewer="reviewer", expected_status="blocked",
                ) is True
                raise RuntimeError("rollback-me")
        assert _status(conn, tid) == "blocked"


def _events(conn, tid, kind):
    return conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = ?", (tid, kind),
    ).fetchone()[0]
