"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-20

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.UniqueConstraint("username"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_bin",
    )
    op.create_index("ix_admin_users_username", "admin_users", ["username"], unique=True)
    op.create_table(
        "panels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("base_url", sa.String(length=300), nullable=False),
        sa.Column("secure_path", sa.String(length=120), nullable=False),
        sa.Column("admin_email", sa.String(length=200), nullable=False),
        sa.Column("admin_password_enc", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("last_ok_at", sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_bin",
    )
    op.create_table(
        "partners",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("balance_toman", sa.Integer().with_variant(sa.BigInteger(), "mysql"), nullable=False),
        sa.Column("banned", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.CheckConstraint("balance_toman >= 0", name="ck_partner_toman_nonneg"),
        sa.UniqueConstraint("username"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_bin",
    )
    op.create_index("ix_partners_username", "partners", ["username"], unique=True)
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("panel_id", sa.Integer(), sa.ForeignKey("panels.id"), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("email_domain", sa.String(length=200), nullable=False),
        sa.Column("price_per_gb", sa.Integer().with_variant(sa.BigInteger(), "mysql"), nullable=False),
        sa.Column("price_per_day", sa.Integer().with_variant(sa.BigInteger(), "mysql"), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.UniqueConstraint("panel_id", "plan_id", name="uq_product_panel_plan"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_bin",
    )
    op.create_index("ix_products_panel_id", "products", ["panel_id"])
    op.create_table(
        "partner_product_credits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("partner_id", sa.Integer(), sa.ForeignKey("partners.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("credit_gb", sa.Numeric(18, 6), nullable=False),
        sa.Column("credit_days", sa.Integer(), nullable=False),
        sa.CheckConstraint("credit_gb >= 0", name="ck_credit_gb_nonneg"),
        sa.CheckConstraint("credit_days >= 0", name="ck_credit_days_nonneg"),
        sa.UniqueConstraint("partner_id", "product_id", name="uq_partner_product_credit"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_bin",
    )
    op.create_index("ix_partner_product_credits_partner_id", "partner_product_credits", ["partner_id"])
    op.create_index("ix_partner_product_credits_product_id", "partner_product_credits", ["product_id"])
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("partner_id", sa.Integer(), sa.ForeignKey("partners.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("v2_user_id", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("traffic_cap_gb", sa.Numeric(18, 6), nullable=False),
        sa.Column("expired_at", sa.BigInteger(), nullable=False),
        sa.Column("banned", sa.Boolean(), nullable=False),
        sa.Column("blocked_by_partner_ban", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.CheckConstraint("traffic_cap_gb >= 0", name="ck_cap_nonneg"),
        sa.UniqueConstraint("product_id", "username", name="uq_subscription_product_username"),
        sa.UniqueConstraint("product_id", "v2_user_id", name="uq_subscription_product_v2_user"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_bin",
    )
    op.create_index("ix_subscriptions_partner_id", "subscriptions", ["partner_id"])
    op.create_index("ix_subscriptions_product_id", "subscriptions", ["product_id"])
    op.create_table(
        "wallet_ledgers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("partner_id", sa.Integer(), sa.ForeignKey("partners.id"), nullable=False),
        sa.Column("asset", sa.String(length=16), nullable=False),
        sa.Column("amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=True),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("subscriptions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_bin",
    )
    op.create_index("ix_wallet_ledgers_partner_id", "wallet_ledgers", ["partner_id"])
    op.create_index("ix_wallet_ledgers_created_at", "wallet_ledgers", ["created_at"])


def downgrade() -> None:
    op.drop_table("wallet_ledgers")
    op.drop_table("subscriptions")
    op.drop_table("partner_product_credits")
    op.drop_table("products")
    op.drop_table("partners")
    op.drop_table("panels")
    op.drop_table("admin_users")
