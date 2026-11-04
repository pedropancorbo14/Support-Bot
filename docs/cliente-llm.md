# Cliente LLM

`app/llm.py` define el contrato `LlmClient` y su implementación `OpenAICompatibleClient`.
El resto del servicio solo conoce el protocolo, así que cambiar de proveedor (Ollama,
vLLM, llama.cpp, OpenAI…) se reduce a cambiar `LLM_BASE_URL`, `LLM_API_KEY` y los modelos.

## Contrato

```python
class LlmClient(Protocol):
    async def chat(self, messages) -> ChatResult            # respuesta completa + Usage
    def chat_stream(self, messages) -> AsyncIterator[str | Usage]
    async def embed(self, texts) -> list[list[float]]
    async def ping(self) -> bool                            # usado por /health
    async def aclose(self) -> None
```

- `messages` es una lista de `{"role": "system" | "user" | "assistant", "content": str}`.
- `chat_stream` emite fragmentos de texto y, al final, un `Usage` con los tokens reales
  de prompt y respuesta (se pide con `stream_options.include_usage`).
- Cualquier fallo (timeout, HTTP de error, JSON inválido) se lanza como `LlmError`. Quien
  llama decide cómo degradar, por ejemplo con un mensaje amable y la oferta de un humano.

## Endpoints usados

| Operación | Endpoint |
|---|---|
| Chat | `POST {LLM_BASE_URL}/chat/completions` |
| Embeddings | `POST {LLM_BASE_URL}/embeddings` |
| Salud | `GET {LLM_BASE_URL}/models` |

## Modelos con razonamiento (qwen3)

Ollama envía el razonamiento de los modelos "thinking" en un campo distinto de `content`.
El cliente solo usa `content`, así que el razonamiento nunca llega al usuario. Para no
pagar su latencia, define `LLM_REASONING_EFFORT=none`.

## Tests

`tests/test_llm.py` prueba el cliente con `httpx.MockTransport`: cuerpo enviado, cabecera
de autorización, streaming SSE, orden de embeddings y errores. `tests/fakes.py` contiene
`FakeLlmClient`, un cliente determinista que el resto de tests inyecta con
`create_app(settings, llm=FakeLlmClient())`.
