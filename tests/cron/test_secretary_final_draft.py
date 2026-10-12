"""Final-draft delivery acceptance for the proactive secretary.

Replays execution c7275df2 (job reemxy-proactive-secretary / 3719482db178,
Telegram msg 15447) against the verdict in ``cron.scheduler``:
- fixture A (failed extraction, partial fragment): must NOT send
  (``continue-internally``, mapped like ``blocked_acceptance``);
- fixtures B/C (fetch returned bodies): must deliver verbatim;
- a job without the opt-in sends unchanged.

Fixtures live outside the repo (private draft text stays in tasks-loop tmp);
skipped when absent so the suite stays portable.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron import incidents
import cron.scheduler as sched

FIXTURE_DIR = Path(
    "/home/kom/Dropbox/Reemxy/tasks-loop/tmp/secretary-final-acceptance-20261012")
JOB_ID = "3719482db178"


def _load(name):
    path = FIXTURE_DIR / name
    if not path.exists():
        pytest.skip(f"fixture absent: {path}")
    return json.loads(path.read_text())


def _job_from_fixture(fixture, *, opted_in=True, correction=False):
    job = {"id": JOB_ID, "name": "reemxy-proactive-secretary"}
    if opted_in:
        job["_final_draft_acceptance"] = {"final_draft": True}
    job["_final_draft_evidence"] = fixture.get("tool_evidence", [])
    if "comparison_inputs_present" in fixture:
        job["_comparison_inputs_present"] = fixture["comparison_inputs_present"]
    if correction or "imminent" in str(fixture.get("decision_progress", "")):
        job["_final_draft_correction"] = True
    return job


def _compose(job, draft, monkeypatch, tmp_path):
    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return sched._compose_run_delivery(
        job, success=True, error=None, final_response=draft, output_file=None)


def test_fixture_a_failed_run_does_not_send(monkeypatch, tmp_path):
    fixture = _load("fixture_a_failed_run.json")
    job = _job_from_fixture(fixture)
    content, *_ = _compose(job, fixture["draft"], monkeypatch, tmp_path)
    assert content == "", "partial fragment with failed extraction must not send"
    assert job.get("_final_draft_verdict") == "continue-internally"
    hold = job.get("final_draft_hold", "")
    assert hold.startswith("final_draft_held:continue-internally:")
    assert sched._classify_delivery_outcome(
        delivery_error=hold, should_deliver=False, unresolved_origin=False,
        normalized_deliver="local", incident_acked=False, success=True,
    ) == "blocked_acceptance"


def test_fixture_b_complete_comparison_delivers(monkeypatch, tmp_path):
    fixture = _load("fixture_b_complete_comparison.json")
    job = _job_from_fixture(fixture)
    content, *_ = _compose(job, fixture["draft"], monkeypatch, tmp_path)
    assert content == fixture["draft"]
    assert "final_draft_hold" not in job


def test_fixture_c_route_correction_delivers(monkeypatch, tmp_path):
    fixture = _load("fixture_c_route_correction.json")
    job = _job_from_fixture(fixture)
    content, *_ = _compose(job, fixture["draft"], monkeypatch, tmp_path)
    assert content == fixture["draft"]
    assert "final_draft_hold" not in job


def test_job_without_opt_in_sends_unchanged(monkeypatch, tmp_path):
    fixture = _load("fixture_a_failed_run.json")
    job = _job_from_fixture(fixture, opted_in=False)
    content, *_ = _compose(job, fixture["draft"], monkeypatch, tmp_path)
    assert content == fixture["draft"]
    assert "final_draft_hold" not in job


def test_material_correction_delivers_despite_evidence_gap(monkeypatch, tmp_path):
    fixture = _load("fixture_a_failed_run.json")
    job = _job_from_fixture(fixture, correction=True)
    content, *_ = _compose(job, fixture["draft"], monkeypatch, tmp_path)
    assert content == fixture["draft"]
    assert "final_draft_hold" not in job


def test_empty_draft_drops(monkeypatch, tmp_path):
    fixture = _load("fixture_b_complete_comparison.json")
    job = _job_from_fixture(fixture)
    content, *_ = _compose(job, "   \n  ", monkeypatch, tmp_path)
    assert content == ""
    assert job.get("_final_draft_verdict") == "drop"
