"""public accounts: email verification, email tokens, webhook secrets

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Accounts that existed before verification was introduced are treated as verified.
    op.execute("UPDATE users SET email_verified_at = created_at")

    op.create_table(
        "email_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "purpose IN ('verify_email', 'reset_password')",
            name=op.f("ck_email_tokens_purpose_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_email_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_email_tokens_token_hash")),
    )
    op.create_index(op.f("ix_email_tokens_user_id"), "email_tokens", ["user_id"], unique=False)

    op.add_column("projects", sa.Column("webhook_secret", sa.String(length=64), nullable=True))

    # NO ACTION is checked at the end of the statement, so deleting a project can cascade to its
    # queues and jobs together; deleting a queue that still has jobs is still rejected.
    for table in ("jobs", "schedules"):
        name = f"fk_{table}_queue_id_queues"
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, "queues", ["queue_id"], ["id"], ondelete="NO ACTION")


def downgrade() -> None:
    for table in ("jobs", "schedules"):
        name = f"fk_{table}_queue_id_queues"
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, "queues", ["queue_id"], ["id"], ondelete="RESTRICT")
    op.drop_column("projects", "webhook_secret")
    op.drop_index(op.f("ix_email_tokens_user_id"), table_name="email_tokens")
    op.drop_table("email_tokens")
    op.drop_column("users", "email_verified_at")
