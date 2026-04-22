import uuid
from datetime import datetime

from sqlalchemy import String, Boolean, DateTime, ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class TrackingNumber(Base):
    """Подменный номер из пула. Привязывается к проекту и назначается сессиям посетителей."""

    __tablename__ = "tracking_numbers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    # Нормализованный номер: только последние 10 цифр (без +, пробелов, скобок).
    # Используется для матчинга с DID из AMI CDR, который приходит без плюса.
    # Пример: phone='+77004982670' -> phone_normalized='7004982670'
    phone_normalized: Mapped[str] = mapped_column(String(15), unique=True, index=True, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True
    )
    number_type: Mapped[str] = mapped_column(
        String(20), default="dynamic"
    )  # dynamic | static
    source_label: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # для static: "google_ads", "billboard_almaty" и т.д.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    freeze_time: Mapped[int] = mapped_column(Integer, default=900)  # секунды
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    project: Mapped["Project"] = relationship(back_populates="tracking_numbers")
