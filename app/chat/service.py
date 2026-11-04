"""Conversaciones: respuesta del bot, escalado y chat con agentes humanos."""

import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy import any_, case, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.escalation import EscalationFilter
from app.chat.prompt import (
    EndUser,
    HistoryMessage,
    SystemPrompt,
    build_messages,
    split_history,
    summarize,
    transcript,
)
from app.config import Settings
from app.db import get_session
from app.llm import LlmClient, LlmError, Usage
from app.models import (
    ClientApp,
    Conversation,
    ConversationKind,
    ConversationStatus,
    EscalatedBy,
    Message,
    MessageFlag,
    MessageRole,
)
from app.rag.retriever import RetrievedChunk, format_context, retrieve
from app.webhooks import EVENT_ESCALATED, EVENT_MESSAGE_CREATED, enqueue

logger = logging.getLogger(__name__)

LLM_ERROR_REPLY = (
    "Lo siento, ahora mismo no puedo responder. Si quieres, puedo pasarte con una persona "
    "del equipo de soporte."
)
USER_REQUESTED_HUMAN = "El usuario ha pedido hablar con una persona"


@dataclass(frozen=True, slots=True)
class Token:
    text: str


@dataclass(frozen=True, slots=True)
class Completed:
    message_id: int
    escalated: bool
    human_conversation_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class Failed:
    """El LLM falló: se muestra `message` y se ofrece hablar con una persona."""

    message_id: int
    message: str


ReplyEvent = Token | Completed | Failed


class ConversationStateError(Exception):
    """La operación no es válida para el tipo o el estado de la conversación."""


class ChatService:
    def __init__(
        self,
        session: AsyncSession,
        llm: LlmClient,
        settings: Settings,
        system_prompt: SystemPrompt,
    ) -> None:
        self._session = session
        self._llm = llm
        self._settings = settings
        self._system_prompt = system_prompt

    # -- Conversaciones -------------------------------------------------------------------

    async def get_conversation(self, app_id: int, conversation_id: uuid.UUID) -> Conversation:
        """Conversación de la app. Lanza `LookupError` si no existe o es de otra app."""
        conversation = await self._session.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.app_id == app_id
            )
        )
        if conversation is None:
            raise LookupError(conversation_id)
        return conversation

    async def start_conversation(self, client_app: ClientApp, user: EndUser) -> Conversation:
        """Devuelve la conversación abierta del usuario (la humana antes que la del bot) o
        crea una nueva con el bot."""
        conversation = await self._session.scalar(
            select(Conversation)
            .where(
                Conversation.app_id == client_app.id,
                Conversation.external_user_id == user.user_id,
                Conversation.status == ConversationStatus.OPEN,
            )
            .order_by(
                case((Conversation.kind == ConversationKind.HUMAN, 0), else_=1),
                Conversation.created_at.desc(),
            )
            .limit(1)
        )
        if conversation is None:
            conversation = Conversation(
                app_id=client_app.id,
                kind=ConversationKind.BOT,
                status=ConversationStatus.OPEN,
                external_user_id=user.user_id,
            )
            self._session.add(conversation)
        _apply_user(conversation, user)
        await self._session.commit()
        return conversation

    async def messages(self, conversation: Conversation) -> list[Message]:
        return list(
            await self._session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.id)
            )
        )

    # -- Bot ------------------------------------------------------------------------------

    async def reply(
        self, client_app: ClientApp, conversation: Conversation, question: str
    ) -> AsyncIterator[ReplyEvent]:
        """Responde con el bot en streaming y registra el mensaje con sus métricas."""
        _require(conversation, ConversationKind.BOT)
        started = time.perf_counter()
        history = await self._history(conversation)
        self._session.add(
            Message(conversation_id=conversation.id, role=MessageRole.USER, content=question)
        )
        await self._session.commit()

        older, recent = split_history(
            history,
            max_messages=self._settings.history_max_messages,
            max_tokens=self._settings.history_max_tokens,
        )
        await self._refresh_summary(conversation, older)

        escalation = EscalationFilter()
        raw: list[str] = []
        shown: list[str] = []
        usage: Usage | None = None
        ttft_ms: int | None = None
        chunks: list[RetrievedChunk] = []
        try:
            chunks = await retrieve(
                self._session,
                self._llm,
                app_id=client_app.id,
                query=question,
                settings=self._settings,
            )
            prompt = build_messages(
                system_prompt=self._system_prompt.render(
                    app_name=client_app.name, user=_end_user(conversation)
                ),
                user=_end_user(conversation),
                context=format_context(chunks),
                summary=conversation.summary,
                history=recent,
                question=question,
            )
            async for item in self._llm.chat_stream(prompt):
                if isinstance(item, Usage):
                    usage = item
                    continue
                raw.append(item)
                if text := escalation.feed(item):
                    ttft_ms = ttft_ms if ttft_ms is not None else _elapsed_ms(started)
                    shown.append(text)
                    yield Token(text)
            if tail := escalation.finish():
                shown.append(tail)
                yield Token(tail)
        except LlmError as exc:
            logger.warning("Fallo del LLM en la conversación %s: %s", conversation.id, exc)
            message = self._assistant_message(
                conversation,
                content=LLM_ERROR_REPLY,
                raw="".join(raw) or None,
                chunks=chunks,
                usage=usage,
                started=started,
                ttft_ms=ttft_ms,
                flags=[MessageFlag.LLM_ERROR],
            )
            await self._session.commit()
            yield Failed(message.id, LLM_ERROR_REPLY)
            return

        flags = [] if chunks else [MessageFlag.NO_CONTEXT]
        if escalation.escalated:
            flags.append(MessageFlag.ESCALATED)
        message = self._assistant_message(
            conversation,
            content="".join(shown).strip(),
            raw="".join(raw),
            chunks=chunks,
            usage=usage,
            started=started,
            ttft_ms=ttft_ms,
            flags=flags,
        )
        human = None
        if escalation.reason is not None:
            human = await self._escalate(conversation, escalation.reason, EscalatedBy.MODEL)
        await self._session.commit()
        yield Completed(message.id, human is not None, human.id if human else None)

    async def escalate(
        self, conversation: Conversation, reason: str = USER_REQUESTED_HUMAN
    ) -> Conversation:
        """El usuario pide una persona. Devuelve la nueva conversación humana."""
        _require(conversation, ConversationKind.BOT)
        human = await self._escalate(conversation, reason, EscalatedBy.USER)
        await self._session.commit()
        return human

    # -- Humano ---------------------------------------------------------------------------

    async def post_user_message(self, conversation: Conversation, content: str) -> Message:
        """Mensaje del usuario en una conversación humana; se avisa a la app por webhook."""
        _require(conversation, ConversationKind.HUMAN)
        message = Message(conversation_id=conversation.id, role=MessageRole.USER, content=content)
        self._session.add(message)
        await self._session.flush()
        enqueue(
            self._session,
            conversation.app_id,
            EVENT_MESSAGE_CREATED,
            {
                "conversation_id": str(conversation.id),
                "user_id": conversation.external_user_id,
                "message": {"id": message.id, "content": content},
                "conversation_url": self._conversation_url(conversation.id),
            },
        )
        await self._session.commit()
        return message

    async def post_agent_message(
        self, conversation: Conversation, content: str, author_name: str | None
    ) -> Message:
        _require(conversation, ConversationKind.HUMAN)
        message = Message(
            conversation_id=conversation.id,
            role=MessageRole.AGENT,
            content=content,
            author_name=author_name,
        )
        self._session.add(message)
        await self._session.commit()
        return message

    async def close(self, conversation: Conversation) -> None:
        """El agente cierra la conversación humana; el usuario puede volver con el bot."""
        _require(conversation, ConversationKind.HUMAN)
        _close(conversation)
        await self._session.commit()

    # -- Internos -------------------------------------------------------------------------

    async def _history(self, conversation: Conversation) -> list[HistoryMessage]:
        rows = await self._session.execute(
            select(Message.id, Message.role, Message.content)
            .where(
                Message.conversation_id == conversation.id,
                # Las disculpas por fallo del LLM no aportan nada al modelo.
                not_(MessageFlag.LLM_ERROR.value == any_(Message.flags)),
            )
            .order_by(Message.id)
        )
        return [HistoryMessage(row.id, row.role, row.content) for row in rows]

    async def _refresh_summary(
        self, conversation: Conversation, older: list[HistoryMessage]
    ) -> None:
        """Incorpora al resumen los mensajes antiguos que aún no lo estaban."""
        watermark = conversation.summary_until_message_id or 0
        pending = [m for m in older if m.id > watermark]
        if not pending:
            return
        try:
            conversation.summary = await summarize(
                self._llm,
                transcript([(m.role, m.content) for m in pending]),
                previous=conversation.summary,
            )
        except LlmError as exc:
            logger.warning("No se pudo actualizar el resumen de %s: %s", conversation.id, exc)
            return
        conversation.summary_until_message_id = pending[-1].id
        await self._session.commit()

    async def _escalate(
        self, conversation: Conversation, reason: str, by: EscalatedBy
    ) -> Conversation:
        _close(conversation)
        conversation.escalation_reason = reason
        conversation.escalated_by = by
        human = Conversation(
            id=uuid.uuid4(),
            app_id=conversation.app_id,
            kind=ConversationKind.HUMAN,
            status=ConversationStatus.OPEN,
            parent_id=conversation.id,
            external_user_id=conversation.external_user_id,
            user_name=conversation.user_name,
            user_plan=conversation.user_plan,
            user_locale=conversation.user_locale,
        )
        self._session.add(human)
        enqueue(
            self._session,
            conversation.app_id,
            EVENT_ESCALATED,
            {
                "conversation_id": str(conversation.id),
                "human_conversation_id": str(human.id),
                "user": {
                    "id": conversation.external_user_id,
                    "name": conversation.user_name,
                    "plan": conversation.user_plan,
                    "locale": conversation.user_locale,
                },
                "reason": reason,
                "escalated_by": by,
                "summary": None,  # lo rellena el worker antes de enviar
                "conversation_url": self._conversation_url(conversation.id),
                "human_conversation_url": self._conversation_url(human.id),
            },
        )
        await self._session.flush()
        return human

    def _assistant_message(
        self,
        conversation: Conversation,
        *,
        content: str,
        raw: str | None,
        chunks: list[RetrievedChunk],
        usage: Usage | None,
        started: float,
        ttft_ms: int | None,
        flags: list[MessageFlag],
    ) -> Message:
        message = Message(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content=content,
            raw_content=raw,
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
            latency_ms=_elapsed_ms(started),
            ttft_ms=ttft_ms,
            retrieved_chunks=[_chunk_record(chunk) for chunk in chunks],
            flags=list(flags),
        )
        self._session.add(message)
        return message

    def _conversation_url(self, conversation_id: uuid.UUID) -> str:
        return f"{self._settings.public_base_url.rstrip('/')}/admin/conversations/{conversation_id}"


def get_chat_service(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> ChatService:
    state = request.app.state
    return ChatService(session, state.llm, state.settings, state.system_prompt)


def _require(conversation: Conversation, kind: ConversationKind) -> None:
    if conversation.kind != kind:
        raise ConversationStateError(f"La conversación no es de tipo {kind}")
    if conversation.status != ConversationStatus.OPEN:
        raise ConversationStateError("La conversación está cerrada")


def _close(conversation: Conversation) -> None:
    conversation.status = ConversationStatus.CLOSED
    conversation.closed_at = datetime.now(UTC)


def _apply_user(conversation: Conversation, user: EndUser) -> None:
    conversation.user_name = user.name
    conversation.user_plan = user.plan
    conversation.user_locale = user.locale


def _end_user(conversation: Conversation) -> EndUser:
    return EndUser(
        user_id=conversation.external_user_id,
        name=conversation.user_name,
        plan=conversation.user_plan,
        locale=conversation.user_locale,
    )


def _chunk_record(chunk: RetrievedChunk) -> dict[str, Any]:
    return {
        "chunk_id": chunk.chunk_id,
        "document": chunk.document_title,
        "section": chunk.section,
        "score": round(chunk.score, 4),
    }


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
