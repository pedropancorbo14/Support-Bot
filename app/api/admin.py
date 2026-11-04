import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.auth import CurrentApp
from app.chat.service import ChatService, ConversationStateError, get_chat_service
from app.db import DbSession
from app.models import Conversation, ConversationKind, ConversationStatus, Message
from app.rag.ingest import IngestStatus, delete_document, upsert_document

router = APIRouter(prefix="/admin", tags=["admin"])

ExternalId = Annotated[str, Path(min_length=1, max_length=255)]


class DocumentIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, description="Markdown o texto plano.")


class DocumentOut(BaseModel):
    external_id: str
    status: IngestStatus
    chunks: int
    pending_embeddings: int = Field(description="Chunks a la espera de que el worker los embeba.")


@router.put("/documents/{external_id}")
async def put_document(
    external_id: ExternalId,
    body: DocumentIn,
    client_app: CurrentApp,
    request: Request,
    session: DbSession,
) -> DocumentOut:
    """Crea o actualiza un documento de la app. Idempotente: si no cambia, no hace nada."""
    result = await upsert_document(
        session,
        client_app,
        external_id=external_id,
        title=body.title,
        content=body.content,
        settings=request.app.state.settings,
    )
    await session.commit()
    return DocumentOut(
        external_id=external_id,
        status=result.status,
        chunks=result.chunks,
        pending_embeddings=result.pending_embeddings,
    )


@router.delete("/documents/{external_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_document(
    external_id: ExternalId,
    client_app: CurrentApp,
    session: DbSession,
) -> Response:
    if not await delete_document(session, client_app, external_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Documento no encontrado")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# -- Conversaciones ---------------------------------------------------------------------

Chat = Annotated[ChatService, Depends(get_chat_service)]


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: str
    raw_content: str | None
    author_name: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int | None
    ttft_ms: int | None
    retrieved_chunks: list[dict[str, Any]] | None
    flags: list[str]
    created_at: datetime


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: ConversationKind
    status: ConversationStatus
    parent_id: uuid.UUID | None = Field(description="Conversación del bot de la que procede.")
    external_user_id: str
    user_name: str | None
    user_plan: str | None
    user_locale: str | None
    escalation_reason: str | None
    escalated_by: str | None
    created_at: datetime
    closed_at: datetime | None


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


class AgentMessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)
    author_name: str | None = Field(default=None, max_length=255)


async def _conversation(chat: ChatService, app_id: int, conversation_id: uuid.UUID):
    try:
        return await chat.get_conversation(app_id, conversation_id)
    except LookupError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="Conversación no encontrada"
        ) from None


@router.get("/conversations")
async def list_conversations(
    client_app: CurrentApp,
    session: DbSession,
    user_id: str | None = None,
    kind: ConversationKind | None = None,
    conversation_status: Annotated[ConversationStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ConversationOut]:
    """Conversaciones de la app, de la más reciente a la más antigua."""
    query = select(Conversation).where(Conversation.app_id == client_app.id)
    if user_id is not None:
        query = query.where(Conversation.external_user_id == user_id)
    if kind is not None:
        query = query.where(Conversation.kind == kind)
    if conversation_status is not None:
        query = query.where(Conversation.status == conversation_status)
    rows = await session.scalars(
        query.order_by(Conversation.created_at.desc()).limit(limit).offset(offset)
    )
    return [ConversationOut.model_validate(row) for row in rows]


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: uuid.UUID, client_app: CurrentApp, chat: Chat
) -> ConversationDetail:
    """Conversación completa con todos sus mensajes y métricas."""
    conversation = await _conversation(chat, client_app.id, conversation_id)
    messages = await chat.messages(conversation)
    return ConversationDetail.model_validate(
        {
            **ConversationOut.model_validate(conversation).model_dump(),
            "messages": [MessageOut.model_validate(m) for m in messages],
        }
    )


@router.post("/conversations/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
async def post_agent_message(
    conversation_id: uuid.UUID, body: AgentMessageIn, client_app: CurrentApp, chat: Chat
) -> MessageOut:
    """Respuesta de un agente humano en una conversación escalada."""
    conversation = await _conversation(chat, client_app.id, conversation_id)
    try:
        message: Message = await chat.post_agent_message(
            conversation, body.content, body.author_name
        )
    except ConversationStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return MessageOut.model_validate(message)


@router.post("/conversations/{conversation_id}/close", status_code=status.HTTP_204_NO_CONTENT)
async def close_conversation(
    conversation_id: uuid.UUID, client_app: CurrentApp, chat: Chat
) -> Response:
    """El agente cierra la conversación humana. El usuario vuelve a hablar con el bot."""
    conversation = await _conversation(chat, client_app.id, conversation_id)
    try:
        await chat.close(conversation)
    except ConversationStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
