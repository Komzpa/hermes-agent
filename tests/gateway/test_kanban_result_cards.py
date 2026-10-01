"""Exercise the real notifier against a disposable canonical ingest receiver."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from tools.kanban_tools import _handle_complete


class Adapter:
    def __init__(self):
        self.sent = []
        self.documents = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append(text)

    def extract_local_files(self, text):
        from gateway.platforms.base import BasePlatformAdapter
        return BasePlatformAdapter.extract_local_files(text)

    async def send_document(self, chat_id, file_path, metadata=None):
        from pathlib import Path
        self.documents.append(Path(file_path).read_bytes())


@pytest.fixture
def receiver(monkeypatch):
    receipts = []
    cards = {}
    state = {"status": 204}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            card = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            receipts.append((self.path, self.headers["Authorization"], card))
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
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {
        "kanban": {"result_cards": {"ingest_url": f"http://127.0.0.1:{server.server_port}/v1/ingest"}}
    })
    yield receipts, cards, state
    server.shutdown()
    server.server_close()
    thread.join()


def complete(tmp_path, monkeypatch, metadata, summary, title="Research storage options", artifacts=None):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title=title, assignee="researcher")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        args = {"task_id": tid, "summary": summary, "metadata": dict(metadata)}
        if "output_kind" in args["metadata"]:
            args["output_kind"] = args["metadata"].pop("output_kind")
        if artifacts is not None:
            args["artifacts"] = artifacts
        response = json.loads(_handle_complete(args))
        assert "error" not in response, response
        return tid
    finally:
        conn.close()


def tick(monkeypatch, adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    real_sleep = asyncio.sleep

    async def sleep(delay):
        if delay != 5:
            runner._running = False
            await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    asyncio.run(runner._kanban_notifier_watcher(interval=1))


@pytest.mark.parametrize("output_kind", ["research_result", "proactive_brief"])
def test_receipt_cuts_over_full_result_and_replay_is_idempotent(tmp_path, monkeypatch, receiver, output_kind):
    summary = "Use option B: it preserves data during power failure.\n" + "Measured latency was 4 ms. " * 30
    tid = complete(tmp_path, monkeypatch, {"output_kind": output_kind}, summary)
    adapter = Adapter()
    tick(monkeypatch, adapter)
    receipts, cards, _ = receiver
    assert adapter.sent == []
    assert len(cards) == 1
    first = receipts[0][2]
    assert first["summary"] == summary
    assert first["kind"] == output_kind
    assert first["title"] == "Research storage options"
    assert first["external_id"].startswith(f"kanban:default:{tid}:result:")
    assert receipts[0][:2] == ("/v1/ingest", "Bearer test-source-token")
    conn = kb.connect()
    try:
        conn.execute("UPDATE kanban_notify_subs SET last_event_id=0 WHERE task_id=?", (tid,))
        conn.commit()
    finally:
        conn.close()
    tick(monkeypatch, adapter)
    assert receipts[1][2] == first
    assert len(cards) == 1
    assert adapter.sent == []


@pytest.mark.parametrize("status", [200, 202, 400, 401, 500])
def test_only_committed_receipt_suppresses_telegram(tmp_path, monkeypatch, receiver, status):
    complete(tmp_path, monkeypatch, {"output_kind": "research_result"}, "Option B passed the durability comparison.")
    receiver[2]["status"] = status
    adapter = Adapter()
    tick(monkeypatch, adapter)
    assert len(adapter.sent) == 1
    assert "Option B passed" in adapter.sent[0]
    assert receiver[1] == {}


@pytest.mark.parametrize("metadata,summary", [
    ({}, "The implementation passed its integration check."),
    ({"output_kind": "research_result", "urgent": True}, "Disk failure requires immediate intervention."),
    ({"output_kind": "proactive_brief", "approval_required": True}, "Approve the proposed purchase before ordering."),
    ({"output_kind": "research_result"}, "Research storage options"),
    ({"output_kind": "research_result"}, " "),
])
def test_non_result_urgent_approval_and_empty_outputs_keep_telegram(tmp_path, monkeypatch, receiver, metadata, summary):
    complete(tmp_path, monkeypatch, metadata, summary)
    adapter = Adapter()
    tick(monkeypatch, adapter)
    assert len(adapter.sent) == 1
    assert receiver[0] == []


def test_missing_token_keeps_telegram(tmp_path, monkeypatch, receiver):
    complete(tmp_path, monkeypatch, {"output_kind": "research_result"}, "Option B passed the durability comparison.")
    monkeypatch.delenv("LITTERBOX_SOURCE_TOKEN")
    adapter = Adapter()
    tick(monkeypatch, adapter)
    assert len(adapter.sent) == 1
    assert receiver[0] == []


def test_receiver_rejected_probe_keeps_telegram(tmp_path, monkeypatch, receiver):
    complete(tmp_path, monkeypatch, {"output_kind": "research_result"}, "CHANNEL OK")
    receiver[2]["status"] = 400
    adapter = Adapter()
    tick(monkeypatch, adapter)
    assert len(adapter.sent) == 1
    assert receiver[1] == {}


def test_approval_block_still_delivers_without_ingest(tmp_path, monkeypatch, receiver):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "approval.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Purchase approval", assignee="researcher")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.block_task(conn, tid, reason="Approve the purchase", kind="needs_input")
    finally:
        conn.close()
    adapter = Adapter()
    tick(monkeypatch, adapter)
    assert any("blocked" in text and "Approve the purchase" in text for text in adapter.sent)
    assert receiver[0] == []


@pytest.mark.parametrize("explicit", [True, False])
def test_artifact_result_cuts_over_with_files(tmp_path, monkeypatch, receiver, explicit):
    import base64
    artifact = tmp_path / "comparison.csv"
    content = b"option,latency_ms\nB,4\n"
    artifact.write_bytes(content)
    summary = "Option B won the comparison."
    if not explicit:
        summary += f" Deliverable: {artifact}"
    complete(tmp_path, monkeypatch, {"output_kind": "research_result"}, summary,
             artifacts=[str(artifact)] if explicit else None)
    adapter = Adapter()
    tick(monkeypatch, adapter)
    receipts, cards, _ = receiver
    assert adapter.sent == []
    assert adapter.documents == []
    assert len(cards) == 1
    posted = receipts[0][2]
    assert len(posted["files"]) == 1
    assert posted["files"][0]["name"] == "comparison.csv"
    assert base64.b64decode(posted["files"][0]["data"]) == content


def test_missing_artifact_preserves_telegram(tmp_path, monkeypatch, receiver):
    complete(tmp_path, monkeypatch, {"output_kind": "research_result"},
             "Option B won the comparison.",
             artifacts=[str(tmp_path / "gone.csv")])
    adapter = Adapter()
    tick(monkeypatch, adapter)
    assert receiver[0] == []
    assert len(adapter.sent) == 1


def test_oversize_artifact_preserves_telegram(tmp_path, monkeypatch, receiver, monkeypatch2=None):
    import gateway.kanban_watchers as kw
    artifact = tmp_path / "big.bin"
    artifact.write_bytes(b"x" * 32)
    summary = "Option B won the comparison."
    complete(tmp_path, monkeypatch, {"output_kind": "research_result"}, summary,
             artifacts=[str(artifact)])
    monkeypatch.setattr(kw, "_RESULT_CARD_MAX_BYTES", 4)
    adapter = Adapter()
    tick(monkeypatch, adapter)
    assert receiver[0] == []
    assert len(adapter.sent) == 1
    assert adapter.documents == [b"x" * 32]


def test_artifact_non204_preserves_telegram_upload(tmp_path, monkeypatch, receiver):
    artifact = tmp_path / "comparison.csv"
    content = b"option,latency_ms\nB,4\n"
    artifact.write_bytes(content)
    complete(tmp_path, monkeypatch, {"output_kind": "research_result"},
             "Option B won the comparison.", artifacts=[str(artifact)])
    receiver[2]["status"] = 500
    adapter = Adapter()
    tick(monkeypatch, adapter)
    assert len(adapter.sent) == 1
    assert adapter.documents == [content]
    assert receiver[1] == {}
