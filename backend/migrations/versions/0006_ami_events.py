"""ami events journal

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-02
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Персистентный журнал сырых AMI-событий: защита от потери звонков при
    # рестарте/краше процесса между приёмом Cdr и commit.
    op.execute("""
        CREATE TABLE IF NOT EXISTS ami_events (
            id           BIGSERIAL PRIMARY KEY,
            event_type   VARCHAR(32)  NOT NULL,
            uniqueid     VARCHAR(64),
            payload      JSONB        NOT NULL,
            status       VARCHAR(16)  NOT NULL DEFAULT 'pending',
            received_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
            processed_at TIMESTAMPTZ,
            attempts     INTEGER      NOT NULL DEFAULT 0,
            last_error   TEXT
        )
    """)
    # Частичный индекс: replay при старте берёт только незавершённые события.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_ami_events_pending
        ON ami_events (received_at)
        WHERE status IN ('pending', 'failed')
    """)
    # Индекс для ретеншна done-событий по времени.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_ami_events_status_received
        ON ami_events (status, received_at)
    """)
    # Индекс по uniqueid для корреляции/дебага.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_ami_events_uniqueid
        ON ami_events (uniqueid)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ami_events")
