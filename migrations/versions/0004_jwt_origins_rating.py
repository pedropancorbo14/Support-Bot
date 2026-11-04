"""Secreto JWT y orígenes permitidos por app; valoración de mensajes.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-04
"""

import secrets
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("apps", sa.Column("jwt_secret", sa.String(100)))
    op.add_column(
        "apps",
        sa.Column("allowed_origins", ARRAY(sa.String(255)), nullable=False, server_default="{}"),
    )
    # Las apps existentes reciben un secreto nuevo; se puede consultar rotándolo con el CLI.
    connection = op.get_bind()
    for (app_id,) in connection.execute(sa.text("SELECT id FROM apps")):
        connection.execute(
            sa.text("UPDATE apps SET jwt_secret = :secret WHERE id = :id"),
            {"secret": secrets.token_urlsafe(48), "id": app_id},
        )
    op.alter_column("apps", "jwt_secret", nullable=False)

    op.add_column("messages", sa.Column("rating", sa.SmallInteger()))
    op.create_check_constraint("ck_messages_rating", "messages", "rating IN (-1, 1)")


def downgrade() -> None:
    op.drop_constraint("ck_messages_rating", "messages")
    op.drop_column("messages", "rating")
    op.drop_column("apps", "allowed_origins")
    op.drop_column("apps", "jwt_secret")
