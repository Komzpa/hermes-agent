"""EP2 ``transform_kanban_goal`` seam tests.

Without a plugin the core goal behaviour is unchanged (default title+body,
default 2000-char judge limit); with a test plugin registered the transform,
block and limit directives work. Behaviour tests only.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_cli import goals
from hermes_cli.goals import (
    DEFAULT_GOAL_LIMIT,
    HOOK_GOAL_TEXT_LIMIT,
    HookGoalText,
    kanban_goal_text,
)
from hermes_cli.plugins import VALID_HOOKS, get_plugin_manager


def _task(title="Do thing", body="Accept: done"):
    return SimpleNamespace(id="t1", title=title, body=body, goal_mode=True)


def _isolate_hooks(monkeypatch):
    manager = get_plugin_manager()
    saved = {name: list(callbacks) for name, callbacks in manager._hooks.items()}
    monkeypatch.setattr(manager, "_hooks", {name: list(cbs) for name, cbs in saved.items()})
    return manager


def test_hook_registered():
    assert "transform_kanban_goal" in VALID_HOOKS


def test_no_plugin_default_title_body(monkeypatch):
    manager = _isolate_hooks(monkeypatch)
    manager._hooks.pop("transform_kanban_goal", None)
    resolved = kanban_goal_text(_task(), board="b", phase="turn")
    assert resolved == "Do thing\n\nAccept: done"
    assert not isinstance(resolved, HookGoalText)


def test_str_replaces_goal_and_kwargs(monkeypatch):
    seen = {}

    def plugin(**kwargs):
        seen.update(kwargs)
        return "Hook-supplied acceptance context"

    manager = _isolate_hooks(monkeypatch)
    manager._hooks.setdefault("transform_kanban_goal", []).append(plugin)
    resolved = kanban_goal_text(_task(), board="b", phase="complete")
    assert resolved == "Hook-supplied acceptance context"
    assert isinstance(resolved, HookGoalText)
    assert seen["task_id"] == "t1"
    assert seen["board"] == "b"
    assert seen["phase"] == "complete"
    assert seen["goal"] == "Do thing\n\nAccept: done"


def test_block_directive(monkeypatch):
    manager = _isolate_hooks(monkeypatch)
    manager._hooks.setdefault("transform_kanban_goal", []).append(
        lambda **kwargs: {"action": "block", "reason": "needs recovery"})
    resolved = kanban_goal_text(_task(), board="b", phase="review")
    assert resolved == {"action": "block", "reason": "needs recovery"}


def test_none_falls_back(monkeypatch):
    manager = _isolate_hooks(monkeypatch)
    manager._hooks.setdefault("transform_kanban_goal", []).append(lambda **kwargs: None)
    assert kanban_goal_text(_task(), board="b", phase="turn") == "Do thing\n\nAccept: done"


def test_first_non_none_wins(monkeypatch):
    manager = _isolate_hooks(monkeypatch)
    manager._hooks.setdefault("transform_kanban_goal", []).extend([
        lambda **kwargs: None,
        lambda **kwargs: "first text",
        lambda **kwargs: "second text",
    ])
    assert kanban_goal_text(_task(), board="b", phase="turn") == "first text"


def test_raising_hook_fails_open(monkeypatch):
    def plugin(**kwargs):
        raise RuntimeError("boom")

    manager = _isolate_hooks(monkeypatch)
    manager._hooks.setdefault("transform_kanban_goal", []).append(plugin)
    assert kanban_goal_text(_task(), board="b", phase="turn") == "Do thing\n\nAccept: done"


def test_goal_limit_default_and_hook():
    long_goal = "g" * 5000
    long_response = "r" * 5000
    prompts = []

    def fake_call(call_llm, system_prompt, user_prompt, timeout):
        prompts.append(user_prompt)
        return '{"done": true, "reason": "ok"}'

    with patch.object(goals, "_call_goal_judge_llm", side_effect=fake_call):
        goals.judge_goal(long_goal, long_response)
        goals.judge_goal(long_goal, long_response, goal_limit=HOOK_GOAL_TEXT_LIMIT)
    assert len(long_goal) > DEFAULT_GOAL_LIMIT
    assert long_goal[:DEFAULT_GOAL_LIMIT] in prompts[0]
    assert long_goal not in prompts[0]
    assert long_goal in prompts[1]


def test_goal_gate_hook_block_rejects(monkeypatch):
    from tools import kanban_tools

    manager = _isolate_hooks(monkeypatch)
    manager._hooks.setdefault("transform_kanban_goal", []).append(
        lambda **kwargs: {"action": "block", "reason": "needs recovery"})
    monkeypatch.setattr(kanban_tools, "_goal_judge_available", lambda: True)
    try:
        kanban_tools._goal_gate("kanban_complete", _task(), "t1", "evidence")
    except kanban_tools._Reject as exc:
        assert "needs recovery" in str(exc)
    else:
        raise AssertionError("hook block did not reject the handoff")


def test_goal_gate_hook_text_uses_hook_limit(monkeypatch):
    from tools import kanban_tools

    manager = _isolate_hooks(monkeypatch)
    manager._hooks.setdefault("transform_kanban_goal", []).append(
        lambda **kwargs: "hook context")
    monkeypatch.setattr(kanban_tools, "_goal_judge_available", lambda: True)
    seen = {}

    def fake_judge(*args, **kwargs):
        seen.update(kwargs)
        return ("done", "ok", False, None, False)

    monkeypatch.setattr(kanban_tools, "judge_goal", fake_judge)
    kanban_tools._goal_gate("kanban_complete", _task(), "t1", "evidence")
    assert seen["goal"] == "hook context"
    assert seen["goal_limit"] == HOOK_GOAL_TEXT_LIMIT


def test_loop_hook_block_stops_loop(monkeypatch):
    manager = _isolate_hooks(monkeypatch)
    manager._hooks.setdefault("transform_kanban_goal", []).append(
        lambda **kwargs: {"action": "block", "reason": "stale evidence"})
    blocked = []
    result = goals.run_kanban_goal_loop(
        task_id="t1", goal_text="Do thing", run_turn=lambda p: "x",
        task_status_fn=lambda: "running", block_fn=blocked.append, max_turns=3,
        first_response="resp", board="b")
    assert result["outcome"] == "blocked_by_hook"
    assert blocked and "stale evidence" in blocked[0]


def test_loop_hook_text_judged_with_hook_limit(monkeypatch):
    manager = _isolate_hooks(monkeypatch)
    manager._hooks.setdefault("transform_kanban_goal", []).append(
        lambda **kwargs: "hook context")
    seen = {}

    def fake_judge(goal, last_response, **kwargs):
        seen["goal"] = goal
        seen["goal_limit"] = kwargs.get("goal_limit")
        return ("continue", "not yet", False, None, False)

    monkeypatch.setattr(goals, "judge_goal", fake_judge)
    blocked = []
    result = goals.run_kanban_goal_loop(
        task_id="t1", goal_text="Do thing", run_turn=lambda p: "x",
        task_status_fn=lambda: "running", block_fn=blocked.append, max_turns=1,
        first_response="resp", board="b")
    assert seen["goal"] == "hook context"
    assert seen["goal_limit"] == HOOK_GOAL_TEXT_LIMIT
    assert result["outcome"] == "blocked_budget"


def test_loop_no_plugin_uses_default_limit(monkeypatch):
    manager = _isolate_hooks(monkeypatch)
    manager._hooks.pop("transform_kanban_goal", None)
    seen = {}

    def fake_judge(goal, last_response, **kwargs):
        seen["goal"] = goal
        seen["goal_limit"] = kwargs.get("goal_limit")
        return ("continue", "not yet", False, None, False)

    monkeypatch.setattr(goals, "judge_goal", fake_judge)
    goals.run_kanban_goal_loop(
        task_id="t1", goal_text="Do thing", run_turn=lambda p: "x",
        task_status_fn=lambda: "running", block_fn=lambda m: None, max_turns=1,
        first_response="resp", board="b")
    assert seen["goal"] == "Do thing"
    assert seen["goal_limit"] == DEFAULT_GOAL_LIMIT
