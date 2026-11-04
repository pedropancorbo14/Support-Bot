"""Exportación de respuestas bien valoradas a JSONL en formato chat, para Unsloth.

Cada línea es una respuesta del bot valorada con 👍:

    {"messages": [
      {"role": "system", "content": "<system prompt + datos del usuario>"},
      {"role": "user", "content": "..."}, {"role": "assistant", "content": "..."},  # historial
      {"role": "user", "content": "## Documentación\\n<contexto>\\n\\n## Pregunta\\n<pregunta>"},
      {"role": "assistant", "content": "<respuesta, con [ESCALAR: …] si la hubo>"}
    ]}

El contexto RAG va en el último mensaje del usuario, tal y como se usó al responder.
"""

import json
from collections.abc import Iterator
from datetime import datetime
from typing import Any, TextIO

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.prompt import EndUser, SystemPrompt, user_section
from app.models import ClientApp, Conversation, Message, MessageFlag, MessageRole
from app.rag.retriever import NO_CONTEXT


def training_user_message(context: str, question: str) -> str:
    return f"## Documentación\n{context}\n\n## Pregunta\n{question}"


async def export_dataset(
    session: AsyncSession,
    client_app: ClientApp,
    system_prompt: SystemPrompt,
    output: TextIO,
    *,
    history_max_messages: int,
    since: datetime | None = None,
) -> int:
    """Escribe una línea JSONL por respuesta valorada con 👍. Devuelve cuántas escribió."""
    query = (
        select(Conversation)
        .where(
            Conversation.app_id == client_app.id,
            Conversation.id.in_(
                select(Message.conversation_id).where(
                    Message.role == MessageRole.ASSISTANT, Message.rating == 1
                )
            ),
        )
        .order_by(Conversation.created_at)
    )
    if since is not None:
        query = query.where(Conversation.created_at >= since)

    written = 0
    for conversation in await session.scalars(query):
        messages = list(
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.id)
            )
        )
        system = "\n\n".join(
            part
            for part in (
                system_prompt.render(app_name=client_app.name, user=_end_user(conversation)),
                user_section(_end_user(conversation)),
            )
            if part
        )
        for sample in _samples(system, messages, history_max_messages):
            output.write(json.dumps(sample, ensure_ascii=False) + "\n")
            written += 1
    return written


def _samples(
    system: str, messages: list[Message], history_max_messages: int
) -> Iterator[dict[str, Any]]:
    turns: list[Message] = []  # historial válido (sin errores del LLM)
    for index, message in enumerate(messages):
        is_good_answer = (
            message.role == MessageRole.ASSISTANT
            and message.rating == 1
            and MessageFlag.LLM_ERROR not in message.flags
            and index > 0
            and messages[index - 1].role == MessageRole.USER
        )
        if is_good_answer:
            question = messages[index - 1]
            previous = [m for m in turns if m is not question]
            history = previous[-history_max_messages:] if history_max_messages else []
            yield {
                "messages": [
                    {"role": "system", "content": system},
                    *({"role": m.role, "content": m.content} for m in history),
                    {
                        "role": "user",
                        "content": training_user_message(
                            message.context or NO_CONTEXT, question.content
                        ),
                    },
                    {
                        "role": "assistant",
                        "content": (message.raw_content or message.content).strip(),
                    },
                ]
            }
        if message.role in (MessageRole.USER, MessageRole.ASSISTANT) and (
            MessageFlag.LLM_ERROR not in message.flags
        ):
            turns.append(message)


def _end_user(conversation: Conversation) -> EndUser:
    return EndUser(
        user_id=conversation.external_user_id,
        name=conversation.user_name,
        plan=conversation.user_plan,
        locale=conversation.user_locale,
    )
