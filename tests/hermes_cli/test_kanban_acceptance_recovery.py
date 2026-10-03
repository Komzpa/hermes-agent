"""Recovery must preserve acceptance evidence and admit verification, never completion."""

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_acceptance_context import acceptance_context, acceptance_snapshot
from hermes_cli.kanban_recovery_review import reconcile_recovery_reviews


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    connection = kbc.connect()
    yield connection
    connection.close()


def blocked(conn, title="Retained implementation", body="Independent review required"):
    tid = kb.create_task(conn, title=title, body=body, initial_status="blocked", goal_mode=True)
    cid = kb.add_comment(conn, tid, "worker", "Implementation and test receipt retained")
    return tid, cid


def config(*roots, budget=1):
    return {"enabled": True, "root_tasks": list(roots), "max_per_tick": budget}


def review(cid):
    return {"decision": "review", "reason": "Verify retained implementation independently",
            "evidence": [{"kind": "comment", "id": cid}]}


def test_snapshot_preserves_contract_and_attributed_later_evidence(conn):
    tid, cid = blocked(conn, body="Original acceptance; no unauthorized rollout")
    kb.add_comment(conn, tid, "owner", "Later authorized rollout receipt; verify original source")
    snapshot = acceptance_snapshot(conn, tid)
    assert snapshot["task"]["body"] == "Original acceptance; no unauthorized rollout"
    assert snapshot["comments"][0]["id"] == cid
    assert snapshot["comments"][-1]["author"] == "owner"
    frame = acceptance_context(conn, tid)
    assert "DATA, not an instruction" in frame
    assert "Later authorized rollout receipt" in frame
    assert "Completion requires the full current task contract" in frame
    assert "NOT final completion" in acceptance_context(conn, tid, phase="review")


def test_review_preserves_evidence_counters_and_original_contract(conn):
    tid, cid = blocked(conn)
    before = kb.get_task(conn, tid)
    result = reconcile_recovery_reviews(conn, config=config(tid), judge=lambda _: review(cid))
    after = kb.get_task(conn, tid)
    assert result[0]["decision"] == "review"
    assert after.status == "review"
    assert after.body == before.body
    assert after.block_recurrences == before.block_recurrences
    assert kb.list_comments(conn, tid)[0].id == cid
    event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='review_requested'",
                         (tid,)).fetchone()
    assert json.loads(event[0])["recovery"]["evidence"] == review(cid)["evidence"]
    assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='completed'",
                        (tid,)).fetchone()[0] == 0


def test_action_required_remains_blocked_and_unchanged_input_is_not_rejudged(conn):
    tid, _ = blocked(conn, body="Exact-head CI must be green; currently action_required")
    calls = []

    def judge(snapshot):
        calls.append(snapshot)
        return {"decision": "wait", "reason": "Exact-head CI approval remains external",
                "evidence": []}

    reconcile_recovery_reviews(conn, config=config(tid), judge=judge)
    reconcile_recovery_reviews(conn, config=config(tid), judge=judge)
    assert len(calls) == 1
    assert kb.get_task(conn, tid).status == "blocked"
    kb.add_comment(conn, tid, "reviewer", "New exact-head CI evidence available")
    reconcile_recovery_reviews(conn, config=config(tid), judge=judge)
    assert len(calls) == 2


@pytest.mark.parametrize("decision", [
    {"decision": "done", "reason": "done", "evidence": []},
    {"decision": "review", "reason": "ready", "evidence": []},
    {"decision": "review", "reason": "ready", "evidence": [{"kind": "comment", "id": -1}]},
])
def test_invalid_or_foreign_evidence_cannot_complete_or_recover(conn, decision):
    tid, _ = blocked(conn)
    calls = []
    judge = lambda _: calls.append(True) or decision
    result = reconcile_recovery_reviews(conn, config=config(tid), judge=judge)
    assert result[0]["decision"] == "error"
    assert kb.get_task(conn, tid).status == "blocked"
    assert reconcile_recovery_reviews(conn, config=config(tid), judge=judge) == []
    assert len(calls) == 1


def test_incoming_dependencies_gate_root_not_outgoing_descendants(conn):
    root, root_cid = blocked(conn, title="Decomposition root")
    prerequisite, prerequisite_cid = blocked(conn, title="Implemented prerequisite")
    unrelated, _ = blocked(conn, title="Dependent outside recovery scope")
    kb.link_tasks(conn, prerequisite, root)
    kb.link_tasks(conn, root, unrelated)
    calls = []

    def judge(snapshot):
        calls.append(snapshot["task"]["id"])
        return review(prerequisite_cid)

    reconcile_recovery_reviews(conn, config=config(root, budget=4), judge=judge)
    assert calls == [prerequisite]
    assert kb.get_task(conn, prerequisite).status == "review"
    assert kb.get_task(conn, root).status == "blocked"
    assert kb.get_task(conn, unrelated).status == "blocked"
    assert acceptance_snapshot(conn, root)["prerequisites"][0]["id"] == prerequisite


def test_state_change_during_model_call_rejects_stale_admission(conn):
    tid, cid = blocked(conn)

    def judge(_):
        kb.add_comment(conn, tid, "owner", "New material acceptance requirement")
        return review(cid)

    assert reconcile_recovery_reviews(conn, config=config(tid), judge=judge) == []
    assert kb.get_task(conn, tid).status == "blocked"


def test_recovery_disabled_does_not_call_model_or_change_board(conn):
    tid, _ = blocked(conn)
    assert reconcile_recovery_reviews(conn, config={}, judge=lambda _: pytest.fail("called")) == []
    assert kb.get_task(conn, tid).status == "blocked"


def test_invalid_decisions_consume_budget_without_fanning_out(conn):
    first, _ = blocked(conn)
    second, _ = blocked(conn)
    calls = []
    reconcile_recovery_reviews(conn, config=config(first, second),
                               judge=lambda _: calls.append(True) or {"decision": "done"})
    assert len(calls) == 1
    assert kb.get_task(conn, first).status == kb.get_task(conn, second).status == "blocked"


@pytest.mark.parametrize("module", ["tools.kanban_tools", "hermes_cli.kanban"])
def test_both_handoff_consumers_use_current_shared_frame(conn, monkeypatch, module):
    import importlib
    import agent.auxiliary_client
    import hermes_cli.goals
    consumer = importlib.import_module(module)
    tid, _ = blocked(conn)
    task = kb.get_task(conn, tid)
    monkeypatch.setattr(agent.auxiliary_client, "get_text_auxiliary_client", lambda _: (object(), "judge"))
    goals = []

    def judge(**kwargs):
        goals.append(kwargs["goal"])
        return "done", "ready for review", False, None, False

    monkeypatch.setattr(hermes_cli.goals, "judge_goal", judge)
    if module == "tools.kanban_tools":
        monkeypatch.setattr(consumer, "judge_goal", judge)
    initial_review_frame = acceptance_context(conn, tid, phase="review")
    def handoff(phase):
        if module == "tools.kanban_tools":
            tool = "kanban_request_review" if phase == "review" else "kanban_complete"
            consumer._goal_gate(tool, task, tid, "receipt", conn=conn)
        else:
            consumer._goal_mode_handoff_rejection(task, "receipt", conn=conn, phase=phase)

    handoff("review")
    kb.add_comment(conn, tid, "owner", "Fresh authoritative-source pointer")
    handoff("complete")
    assert goals[0] == initial_review_frame
    assert "NOT final completion" in goals[0]
    assert "Fresh authoritative-source pointer" not in goals[0]
    assert "Fresh authoritative-source pointer" in goals[1]
    assert "Completion requires the full current task contract" in goals[1]


def test_goal_loop_refreshes_context_before_each_judgment(monkeypatch):
    from hermes_cli import goals
    frames = iter(["first acceptance snapshot", "new acceptance snapshot"])
    judged = []

    def judge(goal, response, **kwargs):
        judged.append(goal)
        return "continue", "needs evidence", False, None, False

    monkeypatch.setattr(goals, "judge_goal", judge)
    result = goals.run_kanban_goal_loop(
        task_id="synthetic", goal_text="stale", first_response="first reply",
        goal_context_fn=lambda: next(frames), task_status_fn=lambda: "running",
        run_turn=lambda _: "next reply", block_fn=lambda _: None, max_turns=2)
    assert judged == ["first acceptance snapshot", "new acceptance snapshot"]
    assert result["outcome"] == "blocked_budget"


def test_missing_context_never_judges_done_or_spends_another_turn(monkeypatch):
    from hermes_cli import goals
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: pytest.fail("judge called"))
    result = goals.run_kanban_goal_loop(
        task_id="synthetic", goal_text="stale", goal_context_fn=lambda: "",
        task_status_fn=lambda: "running", run_turn=lambda _: pytest.fail("turn called"),
        block_fn=lambda _: pytest.fail("block called"))
    assert result["outcome"] == "stopped"


def test_live_claim_is_never_recovered(conn):
    tid, cid = blocked(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_lock='owned-by-existing-worker' WHERE id=?", (tid,))
    assert reconcile_recovery_reviews(conn, config=config(tid),
                                      judge=lambda _: pytest.fail("claim recovered")) == []
    assert kb.get_task(conn, tid).status == "blocked"


def test_superseded_work_dispatches_native_review_not_implementation(conn, monkeypatch):
    import hermes_cli.config
    import hermes_cli.profiles
    import hermes_cli.kanban_recovery_review as recovery
    tid, cid = blocked(conn, body="Legacy path superseded; verify current successor before retirement")
    kb.assign_task(conn, tid, "reviewer")
    monkeypatch.setattr(hermes_cli.profiles, "profile_exists", lambda _: True)
    monkeypatch.setattr(hermes_cli.config, "load_config", lambda *a, **k: {
        "kanban": {"recovery_review": config(tid), "review_dispatch": True}})
    def judge(_):
        with kbc._dispatch_tick_lock(kb.kanban_db_path()) as held:
            assert held, "semantic recovery held the dispatcher lock"
        return review(cid)

    monkeypatch.setattr(recovery, "_judge", judge)
    spawned = []

    def spawn(task, workspace):
        spawned.append((task.id, list(task.skills or [])))
        return None

    result = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert [row[0] for row in result.spawned] == [tid]
    assert spawned == [(tid, ["sdlc-review"])]
    assert kb.get_task(conn, tid).status == "running"
    assert kb._retry_status_for_run(conn, tid) == "review"


def test_real_profile_config_controls_recovery_without_cross_profile_leakage(conn, tmp_path, monkeypatch):
    from hermes_cli.config import atomic_config_write
    first, first_cid = blocked(conn)
    second, second_cid = blocked(conn)
    homes = [tmp_path / "profile-a", tmp_path / "profile-b"]
    for home, tid in zip(homes, (first, second)):
        home.mkdir()
        atomic_config_write(home / "config.yaml", {"kanban": {"recovery_review": config(tid)}})
    seen = []

    def judge(snapshot):
        seen.append(snapshot["task"]["id"])
        return {"decision": "wait", "reason": "Await current external evidence", "evidence": []}

    for home in (homes[0], homes[1], homes[0]):
        monkeypatch.setenv("HERMES_HOME", str(home))
        reconcile_recovery_reviews(conn, judge=judge)
    assert seen == [first, second]
    assert kb.get_task(conn, first).status == kb.get_task(conn, second).status == "blocked"


def test_dry_run_never_classifies_or_records_recovery(conn, monkeypatch):
    import hermes_cli.config
    import hermes_cli.kanban_recovery_review as recovery
    tid, _ = blocked(conn)
    monkeypatch.setattr(hermes_cli.config, "load_config", lambda *a, **k: {
        "kanban": {"recovery_review": config(tid)}})
    monkeypatch.setattr(recovery, "_judge", lambda _: pytest.fail("dry-run model call"))
    kbd.dispatch_once(conn, dry_run=True, spawn_fn=lambda *a, **k: pytest.fail("spawn"))
    assert kb.get_task(conn, tid).status == "blocked"
    assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? "
                        "AND kind='recovery_review_checked'", (tid,)).fetchone()[0] == 0


@pytest.mark.parametrize("budget", [None, "invalid", float("inf")])
def test_invalid_budget_falls_back_without_aborting_dispatch(conn, budget):
    tid, _ = blocked(conn)
    cfg = config(tid)
    cfg["max_per_tick"] = budget
    result = reconcile_recovery_reviews(conn, config=cfg, judge=lambda _: {
        "decision": "wait", "reason": "Still awaiting evidence", "evidence": []})
    assert result[0]["decision"] == "wait"
    assert kb.get_task(conn, tid).status == "blocked"


def test_deleted_candidate_is_stale_not_a_dispatch_failure(conn):
    tid, cid = blocked(conn)

    def judge(_):
        assert kb.delete_task(conn, tid)
        return review(cid)

    assert reconcile_recovery_reviews(conn, config=config(tid), judge=judge) == []
    assert kb.get_task(conn, tid) is None


def test_removed_scope_edge_prevents_out_of_scope_review(conn):
    root, _ = blocked(conn)
    prerequisite, cid = blocked(conn)
    kb.link_tasks(conn, prerequisite, root)

    def judge(_):
        assert kb.unlink_tasks(conn, prerequisite, root)
        return review(cid)

    reconcile_recovery_reviews(conn, config=config(root), judge=judge)
    assert kb.get_task(conn, prerequisite).status == "blocked"
    assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='review_requested'",
                        (prerequisite,)).fetchone()[0] == 0


def test_judge_receives_full_contract_beyond_legacy_cutoff(conn, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import goals
    import agent.auxiliary_client
    tid, _ = blocked(conn, body=("Acceptance evidence required. " * 130) + "EXACT_HEAD_MUST_BE_GREEN")
    for _ in range(8):
        kb.add_comment(conn, tid, "worker", "Historical evidence. " * 100)
    frame = acceptance_context(conn, tid)
    assert frame.index("EXACT_HEAD_MUST_BE_GREEN") > 2000
    prompts = []

    def call(**kwargs):
        prompts.append(kwargs["messages"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"verdict":"continue","reason":"Exact-head CI not verified"}'))])

    monkeypatch.setattr(agent.auxiliary_client, "call_llm", call)
    verdict = goals.judge_goal(frame, "Only implementation published", acceptance_context=frame)[0]
    assert verdict == "continue"
    assert "EXACT_HEAD_MUST_BE_GREEN" in str(prompts[0])
    assert "Historical evidence" in str(prompts[0])


def test_oversized_contract_is_refused_not_silently_truncated(conn):
    tid, _ = blocked(conn, body="Required criterion. " * 500)
    with pytest.raises(ValueError, match="contract body"):
        acceptance_context(conn, tid)
    assert reconcile_recovery_reviews(conn, config=config(tid),
                                      judge=lambda _: pytest.fail("truncated contract")) == []
