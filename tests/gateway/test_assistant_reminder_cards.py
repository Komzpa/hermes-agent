"""Assistant-reminder producer -> shared ingest receipt oracle (R26/R27).

Exercises the canonical reminder lane without any live Telegram, production
cron, or device API:

- model-facing ``cronjob`` create with ``output_kind=assistant_reminder``
  persists the classification on the job (never by regex);
- the shared ``try_ingest_assistant_reminder`` builds a timed card with a
  stable ``external_id`` and POSTs it to a disposable source-auth receiver;
- a canonical 204 is the receipt (returns True); replay with the same fire
  upserts idempotently to the same ``external_id``;
- any non-204, missing config, urgent/approval, empty/title-duplicate, or
  non-reminder job returns False (caller must retain Telegram delivery).
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def receiver(monkeypatch):
    receipts = []
    cards = {}
    state = {"status": 204}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            card = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            receipts.append((self.path, self.headers.get("Authorization"), card))
            if state["status"] == 204:
                cards[card["external_id"]] = card
            self.send_response(state["status"])
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("LITTERBOX_SOURCE_TOKEN", "test-source-token")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"result_cards": {"ingest_url": f"http://127.0.0.1:{server.server_port}/v1/ingest"}}},
    )
    yield receipts, cards, state
    server.shutdown()
    server.server_close()
    thread.join()


def _reminder_job(**over):
    job = {
        "id": "abc123def456",
        "name": "Take pills",
        "output_kind": "assistant_reminder",
        "next_run_at": "2026-10-01T09:00:00+00:00",
        "fire_claim": {"scheduled_at": "2026-10-01T09:00:00+00:00"},
        "deliver": "telegram",
    }
    job.update(over)
    return job


def test_reminder_ingest_timed_card_and_replay_idempotent(receiver):
    from gateway.result_ingest import build_assistant_reminder_card, try_ingest_assistant_reminder

    receipts, cards, _state = receiver
    job = _reminder_job()
    content = "Take your pills now."

    card = build_assistant_reminder_card(job, content)
    assert card["timed"] is True
    assert card["kind"] == "reminder"
    assert card["external_id"] == "cron:abc123def456:reminder:2026-10-01T09:00:00+00:00"
    assert card["summary"] == content
    assert card["at"] == job["fire_claim"]["scheduled_at"]

    assert try_ingest_assistant_reminder(job, content) is True
    assert try_ingest_assistant_reminder(job, content) is True  # replay
    assert len(receipts) == 2
    assert receipts[0][2]["external_id"] == receipts[1][2]["external_id"]
    assert len(cards) == 1
    stored = next(iter(cards.values()))
    assert stored["timed"] is True
    assert stored["summary"] == content


@pytest.mark.parametrize("status", [200, 202, 400, 401, 500])
def test_only_204_is_receipt(receiver, status):
    from gateway.result_ingest import try_ingest_assistant_reminder

    receipts, cards, state = receiver
    state["status"] = status
    assert try_ingest_assistant_reminder(_reminder_job(), "Take your pills now.") is False
    assert cards == {}
    assert len(receipts) == 1


def test_urgent_approval_keep_telegram_without_ingest(receiver):
    from gateway.result_ingest import try_ingest_assistant_reminder

    receipts, _cards, _state = receiver
    assert try_ingest_assistant_reminder(_reminder_job(urgent=True), "Take pills now.") is False
    assert try_ingest_assistant_reminder(_reminder_job(approval_required=True), "Take pills now.") is False
    assert receipts == []


def test_non_reminder_and_empty_keep_telegram(receiver):
    from gateway.result_ingest import try_ingest_assistant_reminder

    receipts, _cards, _state = receiver
    assert try_ingest_assistant_reminder({"id": "x", "name": "Scout", "next_run_at": "t"}, "Some update.") is False
    assert try_ingest_assistant_reminder(_reminder_job(), "   ") is False
    assert try_ingest_assistant_reminder(_reminder_job(name="Take pills"), "take PILLS") is False
    assert receipts == []


def test_model_tool_classifies_reminder_explicitly(tmp_path, monkeypatch):
    from cron import jobs as cron_jobs
    from tools.cronjob_tools import cronjob

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    (tmp_path / "hermes-home").mkdir(parents=True, exist_ok=True)
    res = json.loads(cronjob(action="create", prompt="Remind me to take pills", schedule="in 30m", output_kind="assistant_reminder"))
    assert res["job"]["output_kind"] == "assistant_reminder"
    stored = cron_jobs.get_job(res["job"]["job_id"])
    assert stored.get("output_kind") == "assistant_reminder"

    res2 = json.loads(cronjob(action="create", prompt="Scout the docs", schedule="in 30m"))
    assert "output_kind" not in res2["job"]
    stored2 = cron_jobs.get_job(res2["job"]["job_id"])
    assert "output_kind" not in stored2


def test_model_tool_rejects_unknown_output_kind(tmp_path, monkeypatch):
    from tools.cronjob_tools import cronjob

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home2"))
    (tmp_path / "hermes-home2").mkdir(parents=True, exist_ok=True)
    res = json.loads(cronjob(action="create", prompt="Remind me", schedule="in 30m", output_kind="reminder"))
    assert "error" in res


def test_unclaimed_reminder_cannot_invent_time(receiver):
    from gateway.result_ingest import try_ingest_assistant_reminder

    receipts, cards, _state = receiver
    assert not try_ingest_assistant_reminder(_reminder_job(fire_claim=None), "Take pills now.")
    assert receipts == []
    assert cards == {}


def test_claim_preserves_due_occurrence_across_cursor_advance_and_retry(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from cron import jobs

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(jobs, "_hermes_now", lambda: now)
    job = jobs.create_job(prompt="Reminder", schedule="every 1h")
    due = (now - timedelta(seconds=1)).isoformat()
    jobs.update_job(job["id"], {"next_run_at": due})
    jobs.advance_next_runs([job["id"]])
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True, scheduled_at=due)
    assert claimed["next_run_at"] != due
    assert claimed["fire_claim"]["scheduled_at"] == due
    assert jobs.claim_job_for_fire(job["id"], return_job=True) is False
    now += timedelta(seconds=301)
    retried = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert retried["fire_claim"]["scheduled_at"] == due
    assert retried["fire_claim"]["by"] != claimed["fire_claim"]["by"]
