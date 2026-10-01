"""Provider fallback must not select a backend that cannot read this request."""

from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _make_agent(fallback_model):
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            provider="openai-codex",
            base_url="https://chatgpt.com/backend-api/codex",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


def _mock_client():
    client = MagicMock()
    client.base_url = "https://fallback.example/v1"
    client.api_key = "fallback-key"
    return client


def test_fallback_skips_context_too_small_for_current_request():
    agent = _make_agent(
        [
            {"provider": "small", "model": "small-model"},
            {"provider": "large", "model": "large-model"},
        ]
    )
    # This is the preflight estimate for the already assembled wire request,
    # not a prior-session aggregate. It must fit before an unavailable primary
    # can redirect the retry to a smaller backend.
    agent._active_request_tokens = 120_000

    with (
        patch(
            "agent.model_metadata.get_model_context_length",
            # The selected fallback resolves its context once more when its
            # compressor is rebound after activation.
            side_effect=[65_536, 262_144, 262_144],
        ),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_client(), "resolved-model"),
        ) as resolve,
    ):
        assert agent._try_activate_fallback() is True

    assert agent.model == "large-model"
    assert agent.provider == "large"
    assert resolve.call_count == 1
