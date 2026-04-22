"""phone_normalized on tracking_numbers + calls.project_id nullable

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) Добавляем phone_normalized как nullable — чтобы backfill прошёл без ошибок NOT NULL
    op.add_column(
        "tracking_numbers",
        sa.Column("phone_normalized", sa.String(15), nullable=True),
    )

    # 2) Backfill: убираем все нецифровые символы через regexp_replace,
    #    берём последние 10 цифр (right(..., 10)) если длина >= 10,
    #    иначе оставляем всё (для коротких extension-номеров)
    #    WHERE phone_normalized IS NULL — идемпотентность по данным
    op.execute(
        """
        UPDATE tracking_numbers
        SET phone_normalized = CASE
            WHEN length(regexp_replace(phone, '\\D', '', 'g')) >= 10
            THEN right(regexp_replace(phone, '\\D', '', 'g'), 10)
            ELSE regexp_replace(phone, '\\D', '', 'g')
        END
        WHERE phone_normalized IS NULL
        """
    )

    # 3) Делаем колонку NOT NULL — после backfill все строки уже заполнены
    op.alter_column("tracking_numbers", "phone_normalized", nullable=False)

    # 4) Уникальный индекс для быстрого матча при атрибуции звонков
    op.create_index(
        "ix_tracking_numbers_phone_normalized",
        "tracking_numbers",
        ["phone_normalized"],
        unique=True,
    )

    # 5) Разрешаем NULL для calls.project_id — звонок сохраняется даже без атрибуции
    op.alter_column("calls", "project_id", nullable=True)


def downgrade() -> None:
    # Возвращаем project_id в NOT NULL.
    # ВНИМАНИЕ: если в таблице есть строки с project_id IS NULL — downgrade упадёт.
    # Перед откатом нужно удалить или атрибутировать такие записи вручную.
    op.alter_column("calls", "project_id", nullable=False)

    # Удаляем уникальный индекс
    op.drop_index("ix_tracking_numbers_phone_normalized", table_name="tracking_numbers")

    # Удаляем колонку phone_normalized
    op.drop_column("tracking_numbers", "phone_normalized")
