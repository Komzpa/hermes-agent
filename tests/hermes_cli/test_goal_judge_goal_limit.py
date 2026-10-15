"""Regression: kanban goal-mode judging must see the full task contract.

``judge_goal`` truncates the goal text to 2000 chars by default. A kanban
card carries its acceptance criteria in title+body, so criteria past the
cutoff were invisible to the judge. Kanban call sites derive a bounded
larger budget via ``kanban_goal_limit()`` instead of a global bump.
"""

from __future__ import annotations

from unittest.mock import patch

from hermes_cli import goals
from hermes_cli.goals import judge_goal, kanban_goal_limit


def _capture_prompt(monkeypatch_capture: dict, reply: str = '{"verdict": "continue", "reason": "not done"}'):
    class _Msg:
        def __init__(self, content: str):
            self.content = content

    class _Choice:
        def __init__(self, content: str):
            self.message = _Msg(content)

    class _Resp:
        def __init__(self, content: str):
            self.choices = [_Choice(content)]

    def fake_call_llm(*args, **kwargs):
        monkeypatch_capture["messages"] = kwargs.get("messages") or []
        return _Resp(reply)

    return patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm)


def _user_prompt(captured: dict) -> str:
    return next(m["content"] for m in captured["messages"] if m["role"] == "user")


def _long_card_goal(marker: str = "MANDATORY_FINAL_ACCEPTANCE_CRITERION") -> str:
    # Title + filler body pushing the acceptance criterion past the 2000-char cutoff.
    return "Fix the widget\n\n" + "background context. " * 150 + f"\nAcceptance: {marker}"


def test_default_limit_truncates_long_goal():
    """Documents the pre-fix behaviour: the default budget hides late criteria."""
    goal = _long_card_goal()
    assert len(goal) > 2000
    captured: dict = {}
    with _capture_prompt(captured):
        judge_goal(goal, "did some work")
    assert "MANDATORY_FINAL_ACCEPTANCE_CRITERION" not in _user_prompt(captured)


def test_kanban_limit_keeps_full_contract():
    """Kanban-derived budget: the judge sees the whole title+body contract."""
    goal = _long_card_goal()
    captured: dict = {}
    with _capture_prompt(captured):
        judge_goal(goal, "did some work", goal_limit=kanban_goal_limit(goal))
    assert "MANDATORY_FINAL_ACCEPTANCE_CRITERION" in _user_prompt(captured)


def test_kanban_goal_limit_bounds():
    assert kanban_goal_limit("short") == goals._JUDGE_GOAL_LIMIT_CHARS
    assert kanban_goal_limit("x" * 5000) == 5000
    assert kanban_goal_limit("x" * 100_000) == goals._JUDGE_KANBAN_GOAL_LIMIT_CHARS


def test_goal_limit_invalid_falls_back_to_default():
    captured: dict = {}
    with _capture_prompt(captured):
        judge_goal("do the thing", "substantive response to evaluate", goal_limit=0)
    assert "Goal:\ndo the thing" in _user_prompt(captured)
