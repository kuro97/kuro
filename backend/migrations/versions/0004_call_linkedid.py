"""call linkedid

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Добавляем linkedid для корреляции call-leg (A-leg, B-leg, очередь)
    op.add_column(
        "calls",
        sa.Column("linkedid", sa.String(64), nullable=True),
    )
    op.create_index("ix_calls_linkedid", "calls", ["linkedid"])


def downgrade() -> None:
    op.drop_index("ix_calls_linkedid", table_name="calls")
    op.drop_column("calls", "linkedid")
