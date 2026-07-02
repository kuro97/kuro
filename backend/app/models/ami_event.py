"""ORM-модель журнала AMI-событий. Таблица создаётся миграцией 0006."""

from datetime import datetime

from sqlalchemy import BigInteger, Integer, String, Text, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AmiEvent(Base):
    """Сырое AMI-событие звонка. pending → done | failed."""

    __tablename__ = "ami_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # cdr | new_call | hangup (значения из process_call_event)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # uniqueid звонка — для дебага и корреляции (может быть None у некоторых событий)
    uniqueid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Сырой словарь события как пришёл в process_call_event
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # pending | done | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
