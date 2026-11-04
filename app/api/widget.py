"""API del widget (`/v1`). Autenticación con el JWT del usuario final.

Los endpoints en streaming abren su propia sesión de base de datos dentro del generador:
la sesión de la petición se cierra en cuanto el endpoint devuelve la respuesta.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.chat.service import (
    ChatService,
    Completed,
    ConversationStateError,
    Failed,
    Token,
    get_chat_service,
)
from app.models import ClientApp, Conversation, ConversationKind, ConversationStatus, Message
from app.ratelimit import RateLimitedUser

router = APIRouter(prefix="/v1", tags=["widget"])
static_router = APIRouter(tags=["widget"])

WIDGET_JS = Path(__file__).resolve().parents[2] / "static" / "widget.js"
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
SSE_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {"content": {"text/event-stream": {}}, "description": "Stream de eventos SSE."}
}

Chat = Annotated[ChatService, Depends(get_chat_service)]


class WidgetMessage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str = Field(description="`user`, `assistant` (bot) o `agent` (persona).")
    content: str
    author_name: str | None
    rating: int | None = Field(description="1 = 👍, -1 = 👎.")
    created_at: datetime


class WidgetConversation(BaseModel):
    id: uuid.UUID
    kind: ConversationKind
    status: ConversationStatus
    messages: list[WidgetMessage]


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class FeedbackIn(BaseModel):
    rating: Literal["up", "down"]


class EscalationOut(BaseModel):
    human_conversation_id: uuid.UUID


def sse(event: str, data: dict[str, Any], event_id: int | None = None) -> str:
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _message_payload(message: Message) -> dict[str, Any]:
    return WidgetMessage.model_validate(message).model_dump(mode="json")


async def _user_conversation(
    chat: ChatService, identity: RateLimitedUser, conversation_id: uuid.UUID
) -> Conversation:
    try:
        return await chat.get_user_conversation(
            identity.app.id, identity.user.user_id, conversation_id
        )
    except LookupError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="Conversación no encontrada"
        ) from None


@router.post("/conversations")
async def open_conversation(identity: RateLimitedUser, chat: Chat) -> WidgetConversation:
    """Conversación actual del usuario (la humana si hay una abierta) o una nueva con el bot,
    con su historial."""
    conversation = await chat.start_conversation(identity.app, identity.user)
    messages = await chat.messages(conversation)
    return WidgetConversation(
        id=conversation.id,
        kind=conversation.kind,
        status=conversation.status,
        messages=[WidgetMessage.model_validate(m) for m in messages],
    )


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: uuid.UUID,
    identity: RateLimitedUser,
    chat: Chat,
    after: Annotated[int, Query(ge=0, description="Solo mensajes con id mayor.")] = 0,
) -> list[WidgetMessage]:
    conversation = await _user_conversation(chat, identity, conversation_id)
    return [WidgetMessage.model_validate(m) for m in await chat.messages(conversation, after)]


@router.post(
    "/conversations/{conversation_id}/messages",
    responses={
        **SSE_RESPONSE,
        201: {"model": WidgetMessage, "description": "Mensaje guardado (conversación humana)."},
    },
)
async def send_message(
    conversation_id: uuid.UUID,
    body: MessageIn,
    identity: RateLimitedUser,
    chat: Chat,
    request: Request,
) -> Response:
    """Envía un mensaje.

    - Conversación con el bot: responde en **SSE** con eventos `token` (`{text}`), `done`
      (`{message_id, escalated, human_conversation_id}`) o `error`
      (`{message_id, message, offer_human: true}`).
    - Conversación humana: guarda el mensaje, avisa a la app por webhook y devuelve `201`.
      La respuesta del agente llega por `GET /v1/conversations/{id}/events`.
    """
    conversation = await _user_conversation(chat, identity, conversation_id)
    if conversation.status != ConversationStatus.OPEN:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="La conversación está cerrada")
    if conversation.kind == ConversationKind.HUMAN:
        message = await chat.post_user_message(conversation, body.content)
        return Response(
            content=WidgetMessage.model_validate(message).model_dump_json(),
            status_code=status.HTTP_201_CREATED,
            media_type="application/json",
        )
    return StreamingResponse(
        _reply_stream(request, identity.app, conversation_id, body.content),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def _reply_stream(
    request: Request, client_app: ClientApp, conversation_id: uuid.UUID, content: str
) -> AsyncIterator[str]:
    state = request.app.state
    async with state.sessionmaker() as session:
        chat = ChatService(session, state.llm, state.settings, state.system_prompt)
        conversation = await chat.get_conversation(client_app.id, conversation_id)
        try:
            async for event in chat.reply(client_app, conversation, content):
                match event:
                    case Token(text):
                        yield sse("token", {"text": text})
                    case Completed(message_id, escalated, human_id):
                        yield sse(
                            "done",
                            {
                                "message_id": message_id,
                                "escalated": escalated,
                                "human_conversation_id": str(human_id) if human_id else None,
                            },
                        )
                    case Failed(message_id, message):
                        yield sse(
                            "error",
                            {"message_id": message_id, "message": message, "offer_human": True},
                        )
        except ConversationStateError as exc:  # otra pestaña la cerró mientras tanto
            yield sse("error", {"message_id": None, "message": str(exc), "offer_human": False})


@router.post("/conversations/{conversation_id}/escalate", status_code=status.HTTP_201_CREATED)
async def escalate(
    conversation_id: uuid.UUID, identity: RateLimitedUser, chat: Chat
) -> EscalationOut:
    """Botón «hablar con una persona»: cierra la conversación del bot y abre una humana."""
    conversation = await _user_conversation(chat, identity, conversation_id)
    try:
        human = await chat.escalate(conversation)
    except ConversationStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return EscalationOut(human_conversation_id=human.id)


@router.post("/messages/{message_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def feedback(
    message_id: int, body: FeedbackIn, identity: RateLimitedUser, chat: Chat
) -> Response:
    """Valoración 👍/👎 de una respuesta del bot. Enviarla de nuevo la sustituye."""
    try:
        await chat.rate(
            identity.app.id, identity.user.user_id, message_id, 1 if body.rating == "up" else -1
        )
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Mensaje no encontrado") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/conversations/{conversation_id}/events", responses=SSE_RESPONSE)
async def events(
    conversation_id: uuid.UUID,
    identity: RateLimitedUser,
    chat: Chat,
    request: Request,
    after: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[int | None, Header(ge=0)] = None,
) -> StreamingResponse:
    """Mensajes nuevos de una conversación humana en tiempo real (SSE).

    Eventos: `message` (un `WidgetMessage`, con `id` SSE = id del mensaje) y `closed` (el
    agente cerró la conversación). El stream termina al cerrarse la conversación o al
    caducar el token: el widget renueva el token y se reconecta con `after` o
    `Last-Event-ID`.
    """
    conversation = await _user_conversation(chat, identity, conversation_id)
    if conversation.kind != ConversationKind.HUMAN:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Solo para conversaciones humanas")
    cursor = last_event_id if last_event_id is not None else (after or 0)
    return StreamingResponse(
        _event_stream(request, identity.app.id, conversation_id, cursor, identity.expires_at),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def _event_stream(
    request: Request,
    app_id: int,
    conversation_id: uuid.UUID,
    cursor: int,
    expires_at: datetime,
) -> AsyncIterator[str]:
    state = request.app.state
    settings = state.settings
    loop = asyncio.get_running_loop()
    last_beat = loop.time()
    while datetime.now(UTC) < expires_at and not await request.is_disconnected():
        # Sesión corta en cada sondeo: no retiene conexiones ni snapshots viejos.
        async with state.sessionmaker() as session:
            chat = ChatService(session, state.llm, settings, state.system_prompt)
            conversation = await chat.get_conversation(app_id, conversation_id)
            for message in await chat.messages(conversation, cursor):
                cursor = message.id
                yield sse("message", _message_payload(message), event_id=message.id)
            if conversation.status == ConversationStatus.CLOSED:
                yield sse("closed", {"conversation_id": str(conversation_id)})
                return
        if loop.time() - last_beat >= settings.sse_heartbeat_seconds:
            last_beat = loop.time()
            yield ": ping\n\n"  # mantiene viva la conexión a través de proxies
        await asyncio.sleep(settings.widget_poll_interval)


@static_router.get("/widget.js", include_in_schema=False)
async def widget_js() -> FileResponse:
    return FileResponse(
        WIDGET_JS,
        media_type="text/javascript",
        headers={"Cache-Control": "public, max-age=300"},
    )
