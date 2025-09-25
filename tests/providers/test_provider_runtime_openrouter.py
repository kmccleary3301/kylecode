import types

from agentic_coder_prototype.provider_routing import provider_router
from agentic_coder_prototype.provider_runtime import provider_registry


class _FakeCompletions:
    def create(self, **kwargs):
        return types.SimpleNamespace(choices=[], model=kwargs.get("model"))

    def stream(self, **kwargs):  # pragma: no cover - not used in test
        raise RuntimeError("streaming not required for test")


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


def test_openrouter_runtime_uses_openai_client(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        provider_router.providers["openrouter"],
        "default_headers",
        {
            "HTTP-Referer": "https://example.com",
            "X-Title": "KyleCode",
        },
        raising=False,
    )

    descriptor, model = provider_router.get_runtime_descriptor("openrouter/openai/gpt-4o-mini")
    runtime = provider_registry.create_runtime(descriptor)

    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.chat = _FakeChat()
            self.responses = types.SimpleNamespace(
                create=lambda **_: (_ for _ in ()).throw(RuntimeError("not used")),
                stream=lambda **_: (_ for _ in ()).throw(RuntimeError("not used")),
            )

    monkeypatch.setattr(
        "agentic_coder_prototype.provider_runtime.OpenAI",
        FakeOpenAI,
    )

    client_config = provider_router.create_client_config("openrouter/openai/gpt-4o-mini")
    client = runtime.create_client(
        client_config["api_key"],
        base_url=client_config.get("base_url"),
        default_headers=client_config.get("default_headers"),
    )

    assert captured["api_key"] == "test-key"
    assert captured["base_url"] == "https://openrouter.ai/api/v1"
    assert captured["default_headers"]["HTTP-Referer"] == "https://example.com"
    # Ensure the fake client exposes chat completions entrypoint
    assert hasattr(client.chat, "completions")
    assert client.chat.completions.create(model=model, messages=[]) is not None
