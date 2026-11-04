"""Conversaciones, mensajes, webhooks.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=None if nullable else sa.func.now(),
        nullable=nullable,
    )


def upgrade() -> None:
    op.add_column("apps", sa.Column("webhook_url", sa.String(2000)))
    op.add_column("apps", sa.Column("webhook_secret", sa.String(100)))

    op.create_table(
        "conversations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "app_id", sa.Integer(), sa.ForeignKey("apps.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column(
            "parent_id", UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="SET NULL")
        ),
        sa.Column("external_user_id", sa.String(255), nullable=False),
        sa.Column("user_name", sa.String(255)),
        sa.Column("user_plan", sa.String(100)),
        sa.Column("user_locale", sa.String(20)),
        sa.Column("summary", sa.Text()),
        sa.Column("summary_until_message_id", sa.BigInteger()),
        sa.Column("escalation_reason", sa.Text()),
        sa.Column("escalated_by", sa.String(10)),
        _timestamp("created_at"),
        _timestamp("closed_at", nullable=True),
    )
    op.create_index("ix_conversations_app_user", "conversations", ["app_id", "external_user_id"])

    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("raw_content", sa.Text()),
        sa.Column("author_name", sa.String(255)),
        sa.Column("prompt_tokens", sa.Integer()),
        sa.Column("completion_tokens", sa.Integer()),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("ttft_ms", sa.Integer()),
        sa.Column("retrieved_chunks", JSONB()),
        sa.Column("flags", ARRAY(sa.String(20)), nullable=False, server_default="{}"),
        _timestamp("created_at"),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "app_id", sa.Integer(), sa.ForeignKey("apps.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("event", sa.String(50), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        _timestamp("next_attempt_at"),
        sa.Column("last_status_code", sa.Integer()),
        sa.Column("last_error", sa.Text()),
        _timestamp("created_at"),
        _timestamp("delivered_at", nullable=True),
    )
    op.create_index("ix_webhook_deliveries_app_id", "webhook_deliveries", ["app_id"])
    op.create_index(
        "ix_webhook_deliveries_pending",
        "webhook_deliveries",
        ["next_attempt_at"],
        postgresql_where="status = 'pending'",
    )


def downgrade() -> None:
    op.drop_table("webhook_deliveries")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_column("apps", "webhook_secret")
    op.drop_column("apps", "webhook_url")
