"""Определение уникальности и целевых звонков + антиспам-фильтр."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call import Call

logger = logging.getLogger(__name__)

# Порог целевого звонка (секунды)
TARGET_CALL_THRESHOLD = 30

# Период уникальности (часы) — звонок уникален если с этого номера
# не было звонков на этот проект за последние N часов
UNIQUE_PERIOD_HOURS = 72

# Антиспам: максимум звонков с одного номера за период
SPAM_MAX_CALLS = 10
SPAM_PERIOD_HOURS = 1


async def classify_call(
    db: AsyncSession,
    project_id: str,
    caller_number: str,
    billsec: int,
) -> dict:
    """Классифицирует звонок: уникальный, целевой, спам.

    Returns:
        {"is_unique": bool, "is_target": bool, "is_spam": bool}
    """
    now = datetime.now(timezone.utc)

    # Целевой: разговор длился >= 30 секунд
    is_target = billsec >= TARGET_CALL_THRESHOLD

    # Уникальный: с этого номера не было звонков за последние 72 часа
    unique_since = now - timedelta(hours=UNIQUE_PERIOD_HOURS)
    prev_calls = await db.scalar(
        select(func.count()).select_from(
            select(Call)
            .where(
                Call.project_id == project_id,
                Call.caller_number == caller_number,
                Call.started_at >= unique_since,
                Call.disposition == "ANSWERED",
            )
            .subquery()
        )
    )
    is_unique = (prev_calls or 0) == 0

    # Антиспам: слишком много звонков за короткий период
    spam_since = now - timedelta(hours=SPAM_PERIOD_HOURS)
    recent_calls = await db.scalar(
        select(func.count()).select_from(
            select(Call)
            .where(
                Call.project_id == project_id,
                Call.caller_number == caller_number,
                Call.started_at >= spam_since,
            )
            .subquery()
        )
    )
    is_spam = (recent_calls or 0) >= SPAM_MAX_CALLS

    if is_spam:
        logger.warning(
            "Spam detected: %s called %d times in last %dh (project=%s)",
            caller_number, recent_calls, SPAM_PERIOD_HOURS, project_id,
        )

    return {
        "is_unique": is_unique,
        "is_target": is_target,
        "is_spam": is_spam,
    }
