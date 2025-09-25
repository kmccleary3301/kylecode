import types

import pytest

from agentic_coder_prototype.provider_routing import provider_router
from agentic_coder_prototype.provider_runtime import (
    ProviderRuntimeContext,
    ProviderRuntimeError,
    provider_registry,
)


class _DummySessionState:
    def get_provider_metadata(self, key: str, default=None):
        return default


def _anthropic_tool_schema():
    return [
        {
            "name": "fetch_data",
            "description": "Fetch data",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        }
    ]


def test_anthropic_runtime_stream_success(monkeypatch):
    descriptor, model = provider_router.get_runtime_descriptor("anthropic/claude-3-opus")
    runtime = provider_registry.create_runtime(descriptor)

    final_message = types.SimpleNamespace(
        content=[
            {"type": "text", "text": "Hello"},
            {
                "type": "tool_use",
                "id": "call-1",
                "name": "fetch_data",
                "input": {"foo": "bar"},
            },
            {"type": "thinking", "text": "analysis"},
        ],
        stop_reason="end_turn",
        model=model,
    )

    final_usage = {"input_tokens": 12, "output_tokens": 34}

    class FakeStream:
        def __iter__(self):
            return iter(())

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get_final_message(self):
            return final_message

        def get_final_usage(self):
            return final_usage

    class FakeMessages:
        def stream(self, **kwargs):
            assert kwargs["model"] == model
            return FakeStream()

        def create(self, **kwargs):
            raise AssertionError("create should not be called when streaming succeeds")

    class FakeAnthropic:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setattr(
        "agentic_coder_prototype.provider_runtime.Anthropic",
        FakeAnthropic,
    )

    client = runtime.create_client("fake-key")
    context = ProviderRuntimeContext(
        session_state=_DummySessionState(),
        agent_config={},
        stream=True,
    )

    result = runtime.invoke(
        client=client,
        model=model,
        messages=[{"role": "user", "content": "Hi"}],
        tools=_anthropic_tool_schema(),
        stream=True,
        context=context,
    )

    assert result.messages[0].content == "Hello"
    assert result.messages[0].tool_calls[0].name == "fetch_data"
    assert result.reasoning_summaries == ["analysis"]
    assert result.usage == {"input_tokens": 12, "output_tokens": 34}
    assert result.metadata["usage"]["output_tokens"] == 34


def test_anthropic_runtime_stream_error(monkeypatch):
    descriptor, model = provider_router.get_runtime_descriptor("anthropic/claude-3-opus")
    runtime = provider_registry.create_runtime(descriptor)

    class FakeMessages:
        def stream(self, **kwargs):
            raise RuntimeError("stream disabled")

        def create(self, **kwargs):
            raise AssertionError("create should not be reached in this test")

    class FakeAnthropic:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setattr(
        "agentic_coder_prototype.provider_runtime.Anthropic",
        FakeAnthropic,
    )

    client = runtime.create_client("fake-key")
    context = ProviderRuntimeContext(
        session_state=_DummySessionState(),
        agent_config={},
        stream=True,
    )

    with pytest.raises(ProviderRuntimeError):
        runtime.invoke(
            client=client,
            model=model,
            messages=[{"role": "user", "content": "Hi"}],
            tools=_anthropic_tool_schema(),
            stream=True,
            context=context,
        )
