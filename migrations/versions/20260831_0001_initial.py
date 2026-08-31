"""initial production schema

Revision ID: 20260831_0001
Revises:
Create Date: 2026-08-31
"""

from alembic import op
import sqlalchemy as sa

revision = "20260831_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("idx_users_email", "users", ["email"])
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("refresh_token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("csrf_token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("replaced_by_session_id", sa.BigInteger(), sa.ForeignKey("auth_sessions.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("idx_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("idx_auth_sessions_refresh_hash", "auth_sessions", ["refresh_token_hash"])
    op.create_table(
        "conversations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("session_id", sa.Text(), nullable=False, unique=True),
        sa.Column("user_id", sa.Text()),
        sa.Column("name", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )
    op.create_index("idx_conversations_session_id", "conversations", ["session_id"])
    op.create_index("idx_conversations_created_at", "conversations", [sa.text("created_at DESC")])
    op.create_index("idx_conversations_user_id", "conversations", ["user_id"], postgresql_where=sa.text("user_id IS NOT NULL"))
    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("conversation_id", sa.BigInteger(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("idx_messages_conversation_id", "messages", ["conversation_id", "id"])
    op.create_index("idx_messages_created_at", "messages", [sa.text("created_at DESC")])
    op.create_table(
        "conversation_summaries",
        sa.Column("conversation_id", sa.BigInteger(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("response_json", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_idempotency_user_key"),
    )
    op.create_index("idx_idempotency_expires_at", "idempotency_keys", ["expires_at"])


def downgrade() -> None:
    op.drop_index("idx_idempotency_expires_at", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
    op.drop_table("conversation_summaries")
    op.drop_index("idx_messages_created_at", table_name="messages")
    op.drop_index("idx_messages_conversation_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("idx_conversations_user_id", table_name="conversations")
    op.drop_index("idx_conversations_created_at", table_name="conversations")
    op.drop_index("idx_conversations_session_id", table_name="conversations")
    op.drop_table("conversations")
    op.drop_index("idx_auth_sessions_refresh_hash", table_name="auth_sessions")
    op.drop_index("idx_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_index("idx_users_email", table_name="users")
    op.drop_table("users")
