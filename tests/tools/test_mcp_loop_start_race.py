"""Regression: the MCP loop thread may not have entered ``run_forever`` yet when the first
discovery pass schedules onto it (NousResearch/hermes-agent#140429).

``_ensure_mcp_loop()`` used to start the thread and return at once; ``_run_on_mcp_loop`` then
required ``loop.is_running()`` and raised "MCP event loop is not running" on a loaded host, which
killed Kanban managed workers at startup. A second ``_ensure_mcp_loop()`` in the same window also
replaced the created-but-not-yet-running loop with a fresh one.
"""

from __future__ import annotations

import threading
import time
import types
from unittest.mock import patch

import pytest

from tools import mcp_tool as _origin
from tools import mcp_tool_discovery as _discovery
from tools import mcp_tool_loop as _mcp_loop

_START_DELAY_S = 0.5


class _DelayedThread(threading.Thread):
    """A thread whose target starts late, as under a CPU-starved dispatcher tick."""

    def run(self):
        time.sleep(_START_DELAY_S)
        super().run()


@pytest.fixture
def delayed_loop_thread(monkeypatch):
    """Swap the loop module's ``threading`` for one whose Thread starts its target late."""
    started = []

    class _Recording(_DelayedThread):
        def start(self):
            started.append(self)
            super().start()

    fake_threading = types.SimpleNamespace(**{k: getattr(threading, k) for k in dir(threading)
                                              if not k.startswith("__")})
    fake_threading.Thread = _Recording
    monkeypatch.setattr(_mcp_loop, "threading", fake_threading)
    _mcp_loop._stop_mcp_loop()
    yield started
    _mcp_loop._stop_mcp_loop()


def test_run_on_loop_right_after_ensure_succeeds_when_thread_starts_late(delayed_loop_thread):
    _mcp_loop._ensure_mcp_loop()

    async def _answer():
        return 42

    assert _mcp_loop._run_on_mcp_loop(_answer, timeout=5) == 42
    assert len(delayed_loop_thread) == 1


def test_concurrent_ensure_does_not_replace_a_starting_loop(delayed_loop_thread):
    seen = []
    errors = []

    def _ensure():
        try:
            _mcp_loop._ensure_mcp_loop()
            seen.append(_origin._mcp_loop)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    workers = [threading.Thread(target=_ensure) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert not errors
    assert len(delayed_loop_thread) == 1, "a created-but-not-running loop was replaced"
    assert len({id(loop) for loop in seen}) == 1
    assert seen[0] is _origin._mcp_loop and seen[0].is_running()


def test_discovery_pass_survives_late_loop_thread(delayed_loop_thread):
    """The managed-worker path: discover_profile_mcp -> _register_mcp_servers -> discovery pass."""
    ran = []

    async def _fake_discover_all(new_servers):
        ran.append(dict(new_servers))

    servers = {"probe": {"command": "true"}}
    with patch.object(_discovery, "_select_new_servers", side_effect=lambda s: dict(s)), \
         patch.object(_discovery, "_register_lazy_from_cache", side_effect=lambda s: (s, 0, 0)), \
         patch.object(_discovery, "_discover_all", side_effect=_fake_discover_all), \
         patch.object(_discovery, "_log_summary"), \
         patch.object(_discovery._registration, "register_connected_into_current_scope", return_value=0), \
         patch.object(_discovery._registration, "_existing_tool_names", return_value=[]):
        assert _discovery._register_mcp_servers(servers) == []

    assert ran == [servers]


def test_start_wait_is_bounded(monkeypatch):
    """A loop thread that never starts must not hang the caller past the start budget."""

    class _NeverRuns(threading.Thread):
        def run(self):
            time.sleep(2)

    fake_threading = types.SimpleNamespace(**{k: getattr(threading, k) for k in dir(threading)
                                              if not k.startswith("__")})
    fake_threading.Thread = _NeverRuns
    monkeypatch.setattr(_mcp_loop, "threading", fake_threading)
    monkeypatch.setattr(_mcp_loop, "_MCP_LOOP_START_TIMEOUT", 0.2, raising=False)
    _mcp_loop._stop_mcp_loop()
    try:
        began = time.monotonic()
        _mcp_loop._ensure_mcp_loop()
        assert time.monotonic() - began < 1.5
        with pytest.raises(RuntimeError, match="MCP event loop is not running"):
            _mcp_loop._run_on_mcp_loop(lambda: None, timeout=1)
    finally:
        with _origin._lock:
            loop, _origin._mcp_loop, _origin._mcp_thread = _origin._mcp_loop, None, None
        if loop is not None and not loop.is_running():
            loop.close()
