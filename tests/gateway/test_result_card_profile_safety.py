"""Profile-safety gates on the result-card cutover: who may suppress Telegram.

A secondary-profile subscription must keep its native Telegram delivery and
never POST to the ingest endpoint from a primary-profile notifier; a disabled
result-card configuration must never read artifact bytes at all.
"""
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

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            card = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            receipts.append((self.path, self.headers["Authorization"], card))
            self.send_response(204)
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
    yield receipts
    server.shutdown()
    server.server_close()
    thread.join()


def complete(tmp_path, monkeypatch, *, notifier_profile=None, artifacts=None):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Research storage options", assignee="researcher")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                          notifier_profile=notifier_profile)
        args = {"task_id": tid, "summary": "Option B won the comparison.",
                "metadata": {"output_kind": "research_result"}}
        if artifacts is not None:
            args["artifacts"] = artifacts
        response = json.loads(_handle_complete(args))
        assert "error" not in response, response
        return tid
    finally:
        conn.close()


def tick(monkeypatch, adapter, *, notifier_profile, profile_adapters=None):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner._kanban_notifier_profile = notifier_profile
    runner._primary_profile_name = notifier_profile
    if profile_adapters is not None:
        runner._profile_adapters = profile_adapters
    real_sleep = asyncio.sleep

    async def sleep(delay):
        if delay != 5:
            runner._running = False
            await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    asyncio.run(runner._kanban_notifier_watcher(interval=1))


def test_secondary_profile_sub_keeps_native_telegram_and_never_posts(tmp_path, monkeypatch, receiver):
    adapter = Adapter()
    complete(tmp_path, monkeypatch, notifier_profile="secondary-x")
    tick(monkeypatch, adapter, notifier_profile="primary-x",
         profile_adapters={"secondary-x": {Platform.TELEGRAM: adapter}})
    assert receiver == []
    assert len(adapter.sent) == 1
    assert adapter.documents == []


def test_primary_profile_sub_still_cuts_over_on_204(tmp_path, monkeypatch, receiver):
    adapter = Adapter()
    complete(tmp_path, monkeypatch, notifier_profile="primary-x")
    tick(monkeypatch, adapter, notifier_profile="primary-x",
         profile_adapters={"secondary-x": {Platform.TELEGRAM: Adapter()}})
    assert len(receiver) == 1
    assert adapter.sent == []


def test_disabled_ingest_never_reads_artifact_files(tmp_path, monkeypatch):
    artifact = tmp_path / "big.bin"
    content = b"x" * (9 * 1024 * 1024)
    artifact.write_bytes(content)
    monkeypatch.setenv("LITTERBOX_SOURCE_TOKEN", "test-source-token")
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    encode_calls = []

    def recording_encoder(paths):
        encode_calls.append(list(paths))
        return None

    monkeypatch.setattr("gateway.result_ingest.encode_result_card_files", recording_encoder)
    adapter = Adapter()
    complete(tmp_path, monkeypatch, artifacts=[str(artifact)])
    tick(monkeypatch, adapter, notifier_profile="primary-x")
    # The disabled lane must bail before any artifact byte is read.
    assert encode_calls == []
    assert len(adapter.sent) == 1
    assert len(adapter.documents) == 1
    assert adapter.documents[0] == content
