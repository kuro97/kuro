import uuid
from datetime import datetime

from sqlalchemy import String, Integer, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Call(Base):
    """Запись о звонке. Обогащается данными сессии для атрибуции."""

    __tablename__ = "calls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # project_id разрешает NULL: звонок сохраняется даже если атрибуция провалилась
    # (номер не найден в tracking_numbers). Это предотвращает silent data loss при IntegrityError.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("visitor_sessions.id"), nullable=True
    )

    # Asterisk CDR
    uniqueid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    caller_number: Mapped[str] = mapped_column(String(20), index=True)
    tracking_did: Mapped[str] = mapped_column(String(20), index=True)
    target_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    answered_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Тайминги
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration: Mapped[int] = mapped_column(Integer, default=0)
    billsec: Mapped[int] = mapped_column(Integer, default=0)

    # Статус
    disposition: Mapped[str] = mapped_column(
        String(20), default="NO ANSWER"
    )  # ANSWERED | NO ANSWER | BUSY | FAILED
    is_unique: Mapped[bool] = mapped_column(default=False)
    is_target: Mapped[bool] = mapped_column(default=False)  # целевой звонок (>30 сек)

    # Атрибуция (денормализация из сессии для быстрых запросов)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    medium: Mapped[str | None] = mapped_column(String(255), nullable=True)
    campaign: Mapped[str | None] = mapped_column(String(255), nullable=True)
    keyword: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Запись
    recording_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # lazy="selectin" гарантирует что при project_id=None relationship вернёт None без ошибки
    project: Mapped["Project | None"] = relationship(back_populates="calls", lazy="selectin")
