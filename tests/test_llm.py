import json

import httpx
import pytest

from app.config import Settings
from app.llm import LlmError, OpenAICompatibleClient, Usage

pytestmark = pytest.mark.anyio


def make_client(handler, **overrides) -> OpenAICompatibleClient:
    options = {
        "base_url": "http://ollama:11434/v1",
        "chat_model": "qwen3:8b",
        "embedding_model": "nomic-embed-text",
        "temperature": 0.3,
        "timeout": 5,
    } | overrides
    return OpenAICompatibleClient(**options, transport=httpx.MockTransport(handler))


async def test_chat_sends_model_and_parses_response() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "Hola"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            },
        )

    client = make_client(handler, api_key="secret", reasoning_effort="none")
    result = await client.chat([{"role": "user", "content": "hola"}])

    assert result.content == "Hola"
    assert result.usage == Usage(prompt_tokens=12, completion_tokens=3)
    assert seen["url"] == "http://ollama:11434/v1/chat/completions"
    assert seen["auth"] == "Bearer secret"
    assert seen["body"] == {
        "model": "qwen3:8b",
        "messages": [{"role": "user", "content": "hola"}],
        "temperature": 0.3,
        "stream": False,
        "reasoning_effort": "none",
    }


async def test_chat_without_api_key_sends_no_authorization() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    result = await make_client(handler).chat([{"role": "user", "content": "x"}])

    assert result.usage == Usage(prompt_tokens=0, completion_tokens=0)


async def test_chat_stream_yields_content_and_usage_ignoring_reasoning() -> None:
    events = [
        {"choices": [{"delta": {"reasoning": "pensando..."}}]},
        {"choices": [{"delta": {"content": "Ho"}}]},
        {"choices": [{"delta": {"content": "la"}}]},
        {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}},
    ]
    sse = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    chunks = [c async for c in make_client(handler).chat_stream([{"role": "user", "content": "x"}])]

    assert chunks == ["Ho", "la", Usage(prompt_tokens=7, completion_tokens=2)]


async def test_chat_stream_raises_on_http_error() -> None:
    client = make_client(lambda request: httpx.Response(500, text="boom"))

    with pytest.raises(LlmError, match="HTTP 500"):
        async for _ in client.chat_stream([{"role": "user", "content": "x"}]):
            pass


async def test_embed_returns_vectors_in_input_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"model": "nomic-embed-text", "input": ["a", "b"]}
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            },
        )

    vectors = await make_client(handler).embed(["a", "b"])

    assert vectors == [[1.0, 0.0], [0.0, 1.0]]


async def test_embed_empty_input_skips_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no debería llamar al proveedor")

    assert await make_client(handler).embed([]) == []


async def test_timeout_is_wrapped_in_llm_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    with pytest.raises(LlmError, match="transporte"):
        await make_client(handler).chat([{"role": "user", "content": "x"}])


async def test_ping() -> None:
    assert await make_client(lambda r: httpx.Response(200, json={"data": []})).ping() is True
    assert await make_client(lambda r: httpx.Response(503)).ping() is False


def test_from_settings_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("LLM_CHAT_MODEL", "gemma4:12b")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.1")

    settings = Settings(_env_file=None)
    client = OpenAICompatibleClient.from_settings(settings)

    assert settings.llm_chat_model == "gemma4:12b"
    assert settings.llm_temperature == 0.1
    assert str(client._http.base_url) == "http://llm.internal/v1/"
