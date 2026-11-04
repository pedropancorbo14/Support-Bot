"""Cliente LLM falso y determinista para que los tests no dependan de Ollama."""

import hashlib
import math
from collections.abc import AsyncIterator, Sequence

from app.llm import ChatMessage, ChatResult, LlmError, Usage


class FakeLlmClient:
    def __init__(
        self,
        replies: Sequence[str] = ("Respuesta de prueba.",),
        *,
        dimensions: int = 768,
        fail: bool = False,
        healthy: bool = True,
    ) -> None:
        self.replies = list(replies)
        self.dimensions = dimensions
        self.fail = fail
        self.healthy = healthy
        self.calls: list[list[ChatMessage]] = []
        self.embedded: list[str] = []

    def _next_reply(self, messages: Sequence[ChatMessage]) -> str:
        if self.fail:
            raise LlmError("fallo simulado")
        self.calls.append(list(messages))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    async def chat(self, messages: Sequence[ChatMessage]) -> ChatResult:
        reply = self._next_reply(messages)
        return ChatResult(content=reply, usage=self._usage(messages, reply))

    async def chat_stream(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str | Usage]:
        reply = self._next_reply(messages)
        first, *rest = reply.split(" ")
        yield first
        for word in rest:
            yield " " + word
        yield self._usage(messages, reply)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self.fail:
            raise LlmError("fallo simulado")
        self.embedded.extend(texts)
        return [self._vector(text) for text in texts]

    async def ping(self) -> bool:
        return self.healthy

    async def aclose(self) -> None:
        pass

    def _vector(self, text: str) -> list[float]:
        """Bolsa de palabras con hashing: textos con palabras en común son similares."""
        vector = [0.0] * self.dimensions
        for word in text.lower().split():
            index = int(hashlib.sha256(word.encode()).hexdigest(), 16) % self.dimensions
            vector[index] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    @staticmethod
    def _usage(messages: Sequence[ChatMessage], reply: str) -> Usage:
        prompt = sum(len(m["content"].split()) for m in messages)
        return Usage(prompt_tokens=prompt, completion_tokens=len(reply.split()))
