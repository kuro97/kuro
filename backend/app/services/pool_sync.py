"""Синхронизация пула номеров: загружает active dynamic номера из PostgreSQL в Redis при старте."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session
from app.core.redis import redis_client
from app.models.tracking_number import TrackingNumber
from app.services.number_pool import NumberPoolManager

logger = logging.getLogger(__name__)


async def sync_pool_from_db():
    """Загружает все активные dynamic-номера из PostgreSQL в Redis.
    Вызывается при старте приложения для восстановления пула после перезапуска."""
    async with async_session() as db:
        result = await db.execute(
            select(TrackingNumber).where(
                TrackingNumber.is_active,
                TrackingNumber.number_type == "dynamic",
                TrackingNumber.project_id.isnot(None),
            )
        )
        numbers = result.scalars().all()

    if not numbers:
        logger.info("No tracking numbers found in DB — pool is empty")
        return

    # Группируем по project_id
    by_project: dict[str, list[str]] = {}
    for tn in numbers:
        pid = str(tn.project_id)
        by_project.setdefault(pid, []).append(tn.phone)

    total = 0
    for project_id, phones in by_project.items():
        pool = NumberPoolManager(redis_client, project_id)

        # Проверяем, какие номера уже заняты (не перезаписываем активные сессии)
        busy_key = f"pool:{project_id}:map:number"
        busy_numbers = set(await redis_client.hkeys(busy_key))

        added = 0
        for phone in phones:
            if phone not in busy_numbers:
                await pool.add_number_to_pool(phone)
                added += 1

        total += added
        logger.info(
            "Project %s: synced %d numbers to pool (%d busy, %d total)",
            project_id, added, len(busy_numbers), len(phones),
        )

    logger.info("Pool sync complete: %d numbers loaded across %d projects", total, len(by_project))
