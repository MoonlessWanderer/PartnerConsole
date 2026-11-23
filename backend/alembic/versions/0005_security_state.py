"""Durable login throttles and token revocation; no existing data is modified."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0005_security_state"
down_revision = "0004_subscription_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "login_throttles",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("reset_at", sa.BigInteger(), nullable=False),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_bin",
    )
    op.create_index("ix_login_throttles_reset_at", "login_throttles", ["reset_at"])
    op.create_table(
        "revoked_tokens",
        sa.Column("jti", sa.String(32), primary_key=True),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_bin",
    )
    op.create_index("ix_revoked_tokens_expires_at", "revoked_tokens", ["expires_at"])


def downgrade():
    op.drop_index("ix_revoked_tokens_expires_at", table_name="revoked_tokens")
    op.drop_table("revoked_tokens")
    op.drop_index("ix_login_throttles_reset_at", table_name="login_throttles")
    op.drop_table("login_throttles")
