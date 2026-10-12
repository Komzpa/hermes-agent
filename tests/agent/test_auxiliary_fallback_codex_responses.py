"""Regression guard for the named custom Responses fallback."""

from unittest.mock import MagicMock, patch

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI


@pytest.mark.parametrize("status", ["failed", "cancelled", "incomplete", "completed"])
def test_auxiliary_preserves_terminal_responses_status(status):
    """A proxy failure/partial SSE response must not become a successful summary."""
    from agent.auxiliary_client import CodexAuxiliaryClient

    output = [] if status in {"failed", "cancelled"} else [{
        "type": "message", "id": "msg_fixture", "role": "assistant",
        "status": "completed", "content": [{"type": "output_text", "text": "Retained decisions."}],
    }]
    terminal = {
        "id": "resp_fixture", "status": status, "output": output,
        "error": {"code": "upstream_error", "message": "native upstream response body failed"},
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
    }
    events = [{"type": "response.output_item.done", "item": item} for item in output]
    event_type = "response.failed" if status == "cancelled" else "response." + status
    events.append({"type": event_type, "response": terminal})
    body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    with OpenAI(api_key="fixture", base_url="https://relay.test/backend-api/codex",
                http_client=httpx.Client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, text=body,
                        headers={"content-type": "text/event-stream"})))) as client:
        auxiliary = CodexAuxiliaryClient(client, "summary-model")
        kwargs = {"messages": [{"role": "user", "content": "Summarize the retained exchange."}]}
        if status in {"failed", "cancelled"}:
            with pytest.raises(RuntimeError, match="native upstream response body failed"):
                auxiliary.chat.completions.create(**kwargs)
        else:
            response = auxiliary.chat.completions.create(**kwargs)
            assert response.choices[0].message.content
            assert response.choices[0].finish_reason == ("length" if status == "incomplete" else "stop")


def test_empty_summary_identifies_actual_auxiliary_route(monkeypatch):
    from agent import context_compressor as mod

    compressor = mod.ContextCompressor(model="main-model", provider="main-provider")

    def empty_summary(**kwargs):
        kwargs["route_info"].update(provider="custom:summary-relay", model="summary-model")
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=None), finish_reason="stop")])

    monkeypatch.setattr(mod, "call_llm", empty_summary)
    with pytest.raises(RuntimeError, match="provider=custom:summary-relay model=summary-model"):
        compressor._call_summary_llm("fixture", mod.time.monotonic())


def test_transport_failure_uses_named_custom_responses_client(tmp_path, monkeypatch):
    """A bare runtime custom class must recover its named Responses route."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "model:\n"
        "  provider: custom:relay\n"
        "  default: fallback-model\n"
        "providers:\n"
        "  relay:\n"
        "    base_url: http://relay.test/backend-api/codex\n"
        "    key_env: RELAY_API_KEY\n"
        "    api_mode: codex_responses\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("RELAY_API_KEY", "test-key")

    from agent import auxiliary_client as mod

    class APIConnectionError(Exception):
        pass

    primary = MagicMock()
    primary.base_url = "http://primary.test/v1"
    primary.chat.completions.create.side_effect = APIConnectionError("transport down")
    captured = []

    def record_fallback(client, model, label, **_kwargs):
        captured.append((client, model, label))
        return {"fallback": True}

    mod.clear_runtime_main()
    mod.set_runtime_main(
        "custom", "fallback-model",
        base_url="http://relay.test/backend-api/codex",
        api_key="test-key",
    )
    try:
        with (
            patch.object(
                mod, "_resolve_task_provider_model",
                return_value=("primary", "primary-model", None, None, None),
            ),
            patch.object(mod, "_get_cached_client", return_value=(primary, "primary-model")),
            patch.object(mod, "_try_configured_fallback_chain", return_value=(None, None, "")),
            patch.object(mod, "_call_fallback_candidate_sync", side_effect=record_fallback),
        ):
            assert mod.call_llm(
                task="compression", messages=[{"role": "user", "content": "compress"}]
            ) == {"fallback": True}
    finally:
        mod.clear_runtime_main()

    client, model, label = captured.pop()
    assert isinstance(client, mod.CodexAuxiliaryClient)
    assert model == "fallback-model"
    assert label == "main-agent(custom:relay)"
    assert str(client.base_url).rstrip("/") == "http://relay.test/backend-api/codex"
