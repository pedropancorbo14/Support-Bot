import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import get_settings


class Base(DeclarativeBase):
    pass


class ClientApp(Base):
    """Una app cliente que integra el bot. Todo lo demás cuelga de ella."""

    __tablename__ = "apps"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    webhook_url: Mapped[str | None] = mapped_column(String(2000))
    # En claro porque hace falta para firmar (HMAC); solo se muestra al crearlo.
    webhook_secret: Mapped[str | None] = mapped_column(String(100))
    # Secreto compartido con la app para firmar los JWT de sus usuarios (HS256).
    jwt_secret: Mapped[str] = mapped_column(String(100))
    # Orígenes desde los que se puede usar el widget (CORS), p. ej. https://app.com
    allowed_origins: Mapped[list[str]] = mapped_column(ARRAY(String(255)), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("app_id", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500))
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index(
            "ix_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_pending", "id", postgresql_where="embedding IS NULL"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Redundante con document.app_id a propósito: filtra la búsqueda vectorial sin join.
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int]
    section: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    token_count: Mapped[int]
    # NULL hasta que el worker genera el embedding: la propia tabla hace de cola.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(get_settings().embedding_dimensions)
    )

    document: Mapped[Document] = relationship(back_populates="chunks")


class ConversationKind(StrEnum):
    BOT = "bot"
    HUMAN = "human"


class ConversationStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class EscalatedBy(StrEnum):
    MODEL = "model"
    USER = "user"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"  # el bot
    AGENT = "agent"  # una persona del equipo de soporte


class MessageFlag(StrEnum):
    ESCALATED = "escalated"
    NO_CONTEXT = "no_context"
    LLM_ERROR = "llm_error"


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_app_user", "app_id", "external_user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"))
    kind: Mapped[ConversationKind] = mapped_column(String(10))
    status: Mapped[ConversationStatus] = mapped_column(String(10), default=ConversationStatus.OPEN)
    # Conversación humana -> conversación del bot de la que procede.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    # Datos del usuario final (vienen del JWT; el servicio no gestiona usuarios).
    external_user_id: Mapped[str] = mapped_column(String(255))
    user_name: Mapped[str | None] = mapped_column(String(255))
    user_plan: Mapped[str | None] = mapped_column(String(100))
    user_locale: Mapped[str | None] = mapped_column(String(20))
    # Resumen de los mensajes antiguos que ya no caben en el historial literal.
    summary: Mapped[str | None] = mapped_column(Text)
    summary_until_message_id: Mapped[int | None] = mapped_column(BigInteger)
    escalation_reason: Mapped[str | None] = mapped_column(Text)
    escalated_by: Mapped[EscalatedBy | None] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", order_by="Message.id", passive_deletes=True
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (CheckConstraint("rating IN (-1, 1)", name="ck_messages_rating"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[MessageRole] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)  # lo que ve el usuario
    raw_content: Mapped[str | None] = mapped_column(Text)  # salida literal del modelo
    author_name: Mapped[str | None] = mapped_column(String(255))  # agente humano
    prompt_tokens: Mapped[int | None]
    completion_tokens: Mapped[int | None]
    latency_ms: Mapped[int | None]
    ttft_ms: Mapped[int | None]
    # [{"chunk_id", "document", "section", "score"}]
    retrieved_chunks: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    # Documentación tal y como se inyectó en el prompt (los documentos pueden cambiar).
    context: Mapped[str | None] = mapped_column(Text)
    flags: Mapped[list[str]] = mapped_column(ARRAY(String(20)), default=list)
    rating: Mapped[int | None] = mapped_column(SmallInteger)  # 1 = 👍, -1 = 👎
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class WebhookDelivery(Base):
    """Cada envío de webhook y sus intentos. También es la cola del worker."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        Index(
            "ix_webhook_deliveries_pending",
            "next_attempt_at",
            postgresql_where="status = 'pending'",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), index=True)
    event: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[DeliveryStatus] = mapped_column(String(10), default=DeliveryStatus.PENDING)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_status_code: Mapped[int | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
