"""panel operations

Revision ID: 0002_panel_operations
Revises: 0001_initial
Create Date: 2026-11-20

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from alembic import op

revision: str = "0002_panel_operations"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "panel_operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("partner_id", sa.Integer(), sa.ForeignKey("partners.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("subscriptions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("username", sa.String(length=32), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("payload", sa.Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_bin",
    )
    op.create_index("ix_panel_operations_partner_id", "panel_operations", ["partner_id"])
    op.create_index("ix_panel_operations_product_id", "panel_operations", ["product_id"])
    op.create_index("ix_panel_operations_subscription_id", "panel_operations", ["subscription_id"])
    op.create_index("ix_panel_operations_status", "panel_operations", ["status"])
    if op.get_bind().dialect.name == "mysql":
        # NULL exempts history; FK columns remain ordinary index key parts.
        op.create_index(
            "uq_panel_op_pending_create_mysql", "panel_operations",
            ["product_id", "username", sa.text("(CASE WHEN status = 'pending' AND kind = 'create' THEN 1 ELSE NULL END)")],
            unique=True,
        )
        op.create_index(
            "uq_panel_op_pending_sub_mysql", "panel_operations",
            ["subscription_id", sa.text("(CASE WHEN status = 'pending' THEN 1 ELSE NULL END)")],
            unique=True,
        )
    else:
        op.create_index(
            "uq_panel_op_pending_create",
            "panel_operations",
            ["product_id", "username"],
            unique=True,
            sqlite_where=sa.text("status = 'pending' AND kind = 'create'"),
            postgresql_where=sa.text("status = 'pending' AND kind = 'create'"),
        )
        op.create_index(
            "uq_panel_op_pending_sub",
            "panel_operations",
            ["subscription_id"],
            unique=True,
            sqlite_where=sa.text("status = 'pending' AND subscription_id IS NOT NULL"),
            postgresql_where=sa.text("status = 'pending' AND subscription_id IS NOT NULL"),
        )



def downgrade() -> None:
    suffix = "_mysql" if op.get_bind().dialect.name == "mysql" else ""
    op.drop_index("uq_panel_op_pending_sub" + suffix, table_name="panel_operations")
    op.drop_index("uq_panel_op_pending_create" + suffix, table_name="panel_operations")
    op.drop_index("ix_panel_operations_status", table_name="panel_operations")
    op.drop_index("ix_panel_operations_subscription_id", table_name="panel_operations")
    op.drop_index("ix_panel_operations_product_id", table_name="panel_operations")
    op.drop_index("ix_panel_operations_partner_id", table_name="panel_operations")
    op.drop_table("panel_operations")
