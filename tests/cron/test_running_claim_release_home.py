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
