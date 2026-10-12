"""Regression tests for the leaked cron counter (gateway_state.active_agents stuck at 1).

Two real defects, one symptom:
A. ``tools/cronjob_tools._run_claimed_job`` registered its in-flight claim under
   the active cron-scope home but released it without ``home=``. Any scope
   change between register and release (delivery rebinds the profile scope)
   made the release miss the key, and the owner guard turned it into a no-op:
   the claim leaked permanently.
B. ``cron/scheduler.sweep_stale_inflight`` only reaped ``_running_job_ids``
   while ``get_running_job_ids()`` — the count the gateway shutdown drain
   reports as ``active_agents`` — is the union with ``_running_fire_owners``.
   An orphaned fire-owner entry pinned the count at >=1 with no sweeper able
   to reap it.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import cron.scheduler as sched
import tools.cronjob_tools as tools
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


class _Scope:
    """Bind the cron home scope the way ``_profile_cron_scope`` does for a tick."""

    def __init__(self, home):
        self._home = str(home)

    def __enter__(self):
        self._token = set_hermes_home_override(self._home)
        return self

    def __exit__(self, *_exc):
        reset_hermes_home_override(self._token)
        return False


@pytest.fixture(autouse=True)
def _clean_inflight():
    """Reset the in-memory running sets so tests are isolated."""
    sched._running_job_ids.clear()
    sched._running_fire_owners.clear()
    sched._running_since.clear()
    sched._running_futures.clear()
    sched._running_registration_owners.clear()
    sched._running_allowance_s.clear()
    sched._forced_releases.clear()
    sched._forced_release_count = 0
    yield
    sched._running_job_ids.clear()
    sched._running_fire_owners.clear()
    sched._running_since.clear()
    sched._running_futures.clear()
    sched._running_registration_owners.clear()
    sched._running_allowance_s.clear()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_claimed_run_releases_under_claiming_home(tmp_path):
    """(1) A job claimed under a non-default profile home completes via the
    cronjob_tools path; afterwards both registries are empty.

    RED without fix A: the release resolves the rebound (launch) home, misses
    the profile key, and the owner guard turns it into a no-op.
    """
    launch_home, profile_home = tmp_path / "launch", tmp_path / "secretary"
    launch_home.mkdir()
    profile_home.mkdir()
    job_id = "secretary-evening"
    leaked_tokens = []

    def _fake_run(job, **kwargs):
        # Whatever runs between register and release rebinds the home scope
        # (delivery resolves another profile) and never restores it.
        leaked_tokens.append(set_hermes_home_override(str(launch_home)))
        return True

    try:
        with _Scope(profile_home):
            with patch("cron.scheduler.run_one_job", side_effect=_fake_run):
                tools._run_claimed_job({"id": job_id, "name": job_id})
        assert sched._running_job_ids == set()
        assert sched._running_fire_owners == {}
        assert sched.get_running_job_ids() == frozenset()
    finally:
        for token in reversed(leaked_tokens):
            with contextlib.suppress(Exception):
                reset_hermes_home_override(token)
        sched.release_running_job(job_id, home=launch_home)
        sched.release_running_job(job_id, home=profile_home)


def test_sweep_reaps_terminal_fire_owner(tmp_path):
    """(2) The sweep reaps a planted fire-owner entry whose ledger row is terminal."""
    home = tmp_path / "profile"
    home.mkdir()
    job_id = "orphaned-fire"
    key = sched._inflight_key(job_id, home)
    sched._running_fire_owners[key] = {object(): ("owner-1", home.resolve())}
    row = {"status": "completed", "id": "exec-1", "claimed_at": _now_iso()}
    with _Scope(home), \
            patch("cron.executions.latest_executions", return_value={job_id: row}), \
            patch.object(sched, "mark_job_run") as mark:
        released = sched.sweep_stale_inflight([])
    assert released == [job_id]
    assert sched._running_fire_owners == {}
    assert sched.get_running_job_ids() == frozenset()
    mark.assert_not_called()  # ledger-terminal must not clobber the real outcome
    assert sched.get_inflight_guard_stats()["forced_releases"] == 1


def test_sweep_keeps_live_fire_owner_and_claim(tmp_path):
    """(3) Negative control: a still-running fire-owner entry (non-terminal
    ledger row) and a live claim keep the count >= 1; the sweep reaps neither."""
    home = tmp_path / "profile"
    home.mkdir()
    fire_id, claim_id = "live-fire", "live-claim"
    with _Scope(home):
        assert sched.try_register_running_job(claim_id) is True
    try:
        sched._running_fire_owners[sched._inflight_key(fire_id, home)] = {
            object(): ("owner-9", home.resolve())
        }
        rows = {fire_id: {"status": "running", "id": "exec-live", "claimed_at": _now_iso()}}
        with _Scope(home), \
                patch("cron.executions.latest_executions", return_value=rows), \
                patch.object(sched, "mark_job_run") as mark:
            released = sched.sweep_stale_inflight([])
        assert released == []
        assert sched.get_running_job_ids() == frozenset({claim_id, fire_id})
        mark.assert_not_called()
    finally:
        sched.release_running_job(claim_id, home=home)


def test_default_home_claimed_run_registers_and_releases(tmp_path):
    """(4) Neighbour: a default-home job registers and releases correctly
    through the same cronjob_tools path (no scope switch involved)."""
    home = tmp_path / "launch"
    home.mkdir()
    job_id = "default-home-job"
    with _Scope(home), patch("cron.scheduler.run_one_job", return_value=True):
        tools._run_claimed_job({"id": job_id, "name": job_id})
    assert sched._running_job_ids == set()
    assert sched._running_fire_owners == {}
    assert sched.get_running_job_ids() == frozenset()


# --- fire-boundary persistence of gateway_state.active_agents (updater busy gate) ---

import json  # noqa: E402
import logging  # noqa: E402


def _runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._restart_requested = False
    runner.adapters = {}
    return runner


def _persisted(tmp_path):
    from gateway.status import flush_runtime_status

    flush_runtime_status()
    path = tmp_path / "gateway_state.json"
    return json.loads(path.read_text()) if path.exists() else None


@pytest.fixture
def _clear_listener():
    yield
    sched.set_running_change_listener(None)


@pytest.mark.parametrize("home_name", ["launch", "secretary"])
def test_fire_start_and_end_persist_active_work_count(tmp_path, monkeypatch, _clear_listener, home_name):
    """(5) During a fire the persisted gateway_state active_agents is >= 1; after
    the fire ends it is 0. Default-home and secondary-profile fires both.

    RED without the production hook: nothing re-stamps active_agents at fire
    boundaries, so the file is absent (or stale) mid-run.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sched.set_running_change_listener(_runner()._persist_active_agents)
    home = tmp_path / home_name
    home.mkdir()
    job_id = f"counted-fire-{home_name}"
    seen = {}

    def _fake_run(job, **kwargs):
        seen["during"] = (_persisted(tmp_path) or {}).get("active_agents")
        return True

    with _Scope(home), patch("cron.scheduler.run_one_job", side_effect=_fake_run):
        tools._run_claimed_job({"id": job_id, "name": job_id})
    assert seen["during"] is not None and seen["during"] >= 1
    assert (_persisted(tmp_path) or {}).get("active_agents") == 0


def test_fire_owner_lifecycle_persists_count_and_endures_run_failure(tmp_path, monkeypatch, _clear_listener):
    """(6) run_one_job's fire-owner start and its finally-release each persist the
    count — on the normal path and when the run body raises."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sched.set_running_change_listener(_runner()._persist_active_agents)
    home = tmp_path / "profile"
    home.mkdir()
    job = {"id": "fire-owner-job", "name": "fire-owner-job", "fire_claim": {"by": "owner-1"}}
    seen = {}

    def _hb(job, run):
        seen["during"] = (_persisted(tmp_path) or {}).get("active_agents")
        return True

    def _hb_raises(job, run):
        seen["during_raise"] = (_persisted(tmp_path) or {}).get("active_agents")
        raise RuntimeError("job crashed")

    patches = (
        patch("cron.scheduler.create_execution", return_value={"id": "exec-1"}),
        patch("cron.scheduler.note_cron_execution"),
        patch("cron.scheduler.finish_execution"),
        patch("cron.scheduler._launch_external_cron_worker", return_value=False),
    )
    with _Scope(home):
        for p in patches:
            p.start()
        try:
            with patch("cron.scheduler._run_with_fire_claim_heartbeat", side_effect=_hb):
                assert sched.run_one_job(job) is True
            assert seen["during"] is not None and seen["during"] >= 1
            assert (_persisted(tmp_path) or {}).get("active_agents") == 0
            with patch("cron.scheduler._run_with_fire_claim_heartbeat", side_effect=_hb_raises):
                with pytest.raises(RuntimeError):
                    sched.run_one_job(job)
        finally:
            for p in reversed(patches):
                p.stop()
    assert seen["during_raise"] is not None and seen["during_raise"] >= 1
    assert (_persisted(tmp_path) or {}).get("active_agents") == 0
    assert sched.get_running_job_ids() == frozenset()


def test_sweep_forced_release_persists_count(tmp_path, monkeypatch, _clear_listener):
    """(7) The sweep's forced release of an orphaned fire-owner also re-stamps the count."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sched.set_running_change_listener(_runner()._persist_active_agents)
    home = tmp_path / "profile"
    home.mkdir()
    job_id = "orphaned-fire"
    key = sched._inflight_key(job_id, home)
    sched._running_fire_owners[key] = {object(): ("owner-1", home.resolve())}
    row = {"status": "completed", "id": "exec-1", "claimed_at": _now_iso()}
    with _Scope(home), \
            patch("cron.executions.latest_executions", return_value={job_id: row}), \
            patch.object(sched, "mark_job_run"):
        released = sched.sweep_stale_inflight([])
    assert released == [job_id]
    assert (_persisted(tmp_path) or {}).get("active_agents") == 0


def test_listener_failure_never_breaks_the_cron_run(tmp_path, monkeypatch, _clear_listener, caplog):
    """(8) A writer failure in the hook is logged and swallowed: the run completes."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def _boom():
        raise RuntimeError("gateway writer down")

    sched.set_running_change_listener(_boom)
    home = tmp_path / "launch"
    home.mkdir()
    job_id = "listener-failure-job"
    with caplog.at_level(logging.DEBUG), \
            _Scope(home), patch("cron.scheduler.run_one_job", return_value=True):
        result = tools._run_claimed_job({"id": job_id, "name": job_id})
    assert result["claimed"] is True and result["error"] is None
    assert sched.get_running_job_ids() == frozenset()
    assert "non-fatal" in caplog.text
