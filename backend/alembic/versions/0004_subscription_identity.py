"""subscription panel identity

Revision ID: 0004_subscription_identity
Revises: 0003_operation_lease
Create Date: 2026-11-20

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_subscription_identity"
down_revision: Union[str, None] = "0003_operation_lease"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("subscriptions", sa.Column("email_domain", sa.String(length=200), nullable=True))
    op.add_column("subscriptions", sa.Column("plan_id", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE subscriptions
        SET email_domain = (
                SELECT products.email_domain FROM products WHERE products.id = subscriptions.product_id
            ),
            plan_id = (
                SELECT products.plan_id FROM products WHERE products.id = subscriptions.product_id
            )
        WHERE email_domain IS NULL OR plan_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "plan_id")
    op.drop_column("subscriptions", "email_domain")
