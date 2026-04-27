"""call amo fields

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Поля AMO CRM: город, воронка, статус, квалификация, победа, сумма сделки, время обновления
    op.execute("""
        ALTER TABLE calls
            ADD COLUMN amo_city VARCHAR(100),
            ADD COLUMN amo_pipeline_id BIGINT,
            ADD COLUMN amo_status_id BIGINT,
            ADD COLUMN amo_qualified BOOLEAN NOT NULL DEFAULT false,
            ADD COLUMN amo_won BOOLEAN NOT NULL DEFAULT false,
            ADD COLUMN amo_deal_amount BIGINT,
            ADD COLUMN amo_updated_at TIMESTAMPTZ
    """)
    # Индекс для быстрых запросов «оплаченных сделок за период»
    op.create_index("ix_calls_amo_won_started_at", "calls", ["amo_won", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_calls_amo_won_started_at", table_name="calls")
    op.execute("""
        ALTER TABLE calls
            DROP COLUMN amo_updated_at,
            DROP COLUMN amo_deal_amount,
            DROP COLUMN amo_won,
            DROP COLUMN amo_qualified,
            DROP COLUMN amo_status_id,
            DROP COLUMN amo_pipeline_id,
            DROP COLUMN amo_city
    """)
