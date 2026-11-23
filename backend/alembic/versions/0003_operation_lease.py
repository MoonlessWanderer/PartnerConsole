"""operation lease

Revision ID: 0003_operation_lease
Revises: 0002_panel_operations
Create Date: 2026-11-20

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_operation_lease"
down_revision: Union[str, None] = "0002_panel_operations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("panel_operations", sa.Column("lease_token", sa.String(length=32), nullable=True))
    op.add_column("panel_operations", sa.Column("lease_until", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("panel_operations", "lease_until")
    op.drop_column("panel_operations", "lease_token")
