"""Abstracción del proveedor de LLM.

`LlmClient` es el contrato que usa el resto del servicio. `OpenAICompatibleClient`
lo implementa contra cualquier API compatible con OpenAI (Ollama en producción).
"""

import json
import math
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, Self, TypedDict

import httpx

from app.config import Settings


class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True, slots=True)
class ChatResult:
    content: str
    usage: Usage


def estimate_tokens(text: str) -> int:
    """Estimación barata (≈4 caracteres por token).

    Sirve para trocear y presupuestar el prompt. Los tokens reales de cada respuesta se
    registran a partir del `usage` que devuelve el proveedor.
    """
    return math.ceil(len(text) / 4)


class LlmError(Exception):
    """Fallo del proveedor: timeout, error HTTP o respuesta inválida."""


class LlmClient(Protocol):
    async def chat(self, messages: Sequence[ChatMessage]) -> ChatResult: ...

    def chat_stream(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str | Usage]:
        """Emite fragmentos de texto y, al final, un `Usage` si el proveedor lo informa."""
        ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def ping(self) -> bool: ...

    async def aclose(self) -> None: ...


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        base_url: str,
        chat_model: str,
        embedding_model: str,
        temperature: float,
        timeout: float,
        api_key: str | None = None,
        reasoning_effort: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            timeout=timeout,
            transport=transport,
        )
        self._chat_model = chat_model
        self._embedding_model = embedding_model
        self._temperature = temperature
        self._reasoning_effort = reasoning_effort or None

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            base_url=settings.llm_base_url,
            chat_model=settings.llm_chat_model,
            embedding_model=settings.llm_embedding_model,
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout,
            api_key=settings.llm_api_key.get_secret_value() if settings.llm_api_key else None,
            reasoning_effort=settings.llm_reasoning_effort,
        )

    def _chat_body(self, messages: Sequence[ChatMessage], *, stream: bool) -> dict:
        body: dict = {
            "model": self._chat_model,
            "messages": list(messages),
            "temperature": self._temperature,
            "stream": stream,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        if self._reasoning_effort:
            body["reasoning_effort"] = self._reasoning_effort
        return body

    async def chat(self, messages: Sequence[ChatMessage]) -> ChatResult:
        data = await self._post_json("chat/completions", self._chat_body(messages, stream=False))
        try:
            content = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage") or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError(f"Respuesta de chat inválida: {data!r}") from exc
        return ChatResult(content=content, usage=_usage(usage))

    async def chat_stream(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str | Usage]:
        body = self._chat_body(messages, stream=True)
        try:
            async with self._http.stream("POST", "chat/completions", json=body) as response:
                if response.is_error:
                    await response.aread()
                    raise LlmError(f"HTTP {response.status_code}: {response.text[:500]}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line.removeprefix("data:").strip()
                    if payload == "[DONE]":
                        break
                    chunk = json.loads(payload)
                    for choice in chunk.get("choices") or []:
                        # Solo `content`: el razonamiento de modelos "thinking" llega en otro campo.
                        if text := (choice.get("delta") or {}).get("content"):
                            yield text
                    if usage := chunk.get("usage"):
                        yield _usage(usage)
        except httpx.HTTPError as exc:
            raise LlmError(f"Error de transporte: {exc!r}") from exc
        except json.JSONDecodeError as exc:
            raise LlmError("Fragmento de streaming inválido") from exc

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        data = await self._post_json(
            "embeddings", {"model": self._embedding_model, "input": list(texts)}
        )
        try:
            items = sorted(data["data"], key=lambda item: item["index"])
            vectors = [item["embedding"] for item in items]
        except (KeyError, TypeError) as exc:
            raise LlmError(f"Respuesta de embeddings inválida: {data!r}") from exc
        if len(vectors) != len(texts):
            raise LlmError(f"Se esperaban {len(texts)} embeddings y llegaron {len(vectors)}")
        return vectors

    async def ping(self) -> bool:
        try:
            response = await self._http.get("models")
        except httpx.HTTPError:
            return False
        return response.is_success

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post_json(self, path: str, body: dict) -> dict:
        try:
            response = await self._http.post(path, json=body)
        except httpx.HTTPError as exc:
            raise LlmError(f"Error de transporte: {exc!r}") from exc
        if response.is_error:
            raise LlmError(f"HTTP {response.status_code}: {response.text[:500]}")
        try:
            return response.json()
        except ValueError as exc:
            raise LlmError("La respuesta no es JSON") from exc


def _usage(raw: dict) -> Usage:
    return Usage(
        prompt_tokens=int(raw.get("prompt_tokens") or 0),
        completion_tokens=int(raw.get("completion_tokens") or 0),
    )
