import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from gateway import result_ingest


class _Clock:
    value = datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz):
        assert tz is timezone.utc
        return cls.value


@pytest.fixture
def receiver(monkeypatch):
    receipts = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            receipts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
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
    yield receipts
    server.shutdown()
    server.server_close()
    thread.join()


def _manual_job(execution_id):
    return {
        "id": "job-7",
        "name": "Reminder",
        "output_kind": "assistant_reminder",
        "_scheduled_instant": None,
        "execution_id": execution_id,
    }


def test_manual_retry_identity_uses_execution_id_not_clock(monkeypatch, receiver):
    monkeypatch.setattr(result_ingest, "datetime", _Clock)

    assert result_ingest.try_ingest_assistant_reminder(_manual_job("ex1"), "Drink water") is True
    _Clock.value = datetime(2026, 10, 1, 13, 1, tzinfo=timezone.utc)
    assert result_ingest.try_ingest_assistant_reminder(_manual_job("ex1"), "Drink water") is True
    assert result_ingest.try_ingest_assistant_reminder(_manual_job("ex2"), "Drink water") is True

    assert [card["external_id"] for card in receiver] == [
        "cron:job-7:reminder:manual:ex1",
        "cron:job-7:reminder:manual:ex1",
        "cron:job-7:reminder:manual:ex2",
    ]
    assert receiver[0]["at"] == "2026-10-01T13:00:00+00:00"
    assert receiver[1]["at"] == "2026-10-01T13:01:00+00:00"


def test_scheduled_identity_remains_occurrence_based():
    job = _manual_job("ex1")
    job["_scheduled_instant"] = "2026-10-01T09:00:00+00:00"

    first = result_ingest.build_assistant_reminder_card(job, "Drink water")
    retry = result_ingest.build_assistant_reminder_card(job, "Drink water")
    next_occurrence = result_ingest.build_assistant_reminder_card(
        {**job, "_scheduled_instant": "2026-10-01T10:00:00+00:00"}, "Drink water"
    )

    assert first["external_id"] == "cron:job-7:reminder:2026-10-01T09:00:00+00:00"
    assert retry["external_id"] == first["external_id"]
    assert next_occurrence["external_id"] != first["external_id"]
