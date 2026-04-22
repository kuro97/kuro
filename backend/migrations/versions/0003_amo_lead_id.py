"""amo_lead_id on calls

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Добавляем колонку для хранения id лида в AMO CRM
    op.add_column(
        "calls",
        sa.Column("amo_lead_id", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_calls_amo_lead_id", "calls", ["amo_lead_id"])


def downgrade() -> None:
    op.drop_index("ix_calls_amo_lead_id", table_name="calls")
    op.drop_column("calls", "amo_lead_id")
