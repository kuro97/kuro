"""Фоновый worker: освобождает номера с истёкшими сессиями.
Проверяет last_activity в Redis и возвращает просроченные номера в пул."""

import asyncio
import json
import logging
import time

import redis.asyncio as redis

from app.core.config import settings
from app.core.redis import redis_client

logger = logging.getLogger(__name__)


async def cleanup_expired_sessions():
    """Один цикл очистки: проверяет все занятые номера во всех проектах."""
    # Находим все пулы
    pool_keys = await redis_client.keys("pool:*:map:number")

    released = 0
    for map_key in pool_keys:
        # pool:{project_id}:map:number → pool:{project_id}:free
        project_id = map_key.split(":")[1]
        pool_key = f"pool:{project_id}:free"
        session_map_key = f"pool:{project_id}:map:session"

        # Получаем все занятые номера
        all_numbers = await redis_client.hgetall(map_key)

        now = time.time()
        for number, raw_data in all_numbers.items():
            try:
                data = json.loads(raw_data)
            except (json.JSONDecodeError, TypeError):
                continue

            last_activity = data.get("last_activity", 0)
            elapsed = now - last_activity

            if elapsed > settings.heartbeat_timeout:
                # Сессия просрочена — освобождаем номер
                session_id = data.get("session_id")

                await redis_client.hdel(map_key, number)
                if session_id:
                    await redis_client.hdel(session_map_key, session_id)
                await redis_client.zadd(pool_key, {number: now})

                released += 1
                logger.info(
                    "Released number %s (project=%s, session=%s, idle=%.0fs)",
                    number, project_id, session_id, elapsed,
                )

    return released


async def run_cleanup_loop():
    """Бесконечный цикл очистки. Запускается как background task при старте приложения."""
    logger.info("Number cleanup worker started (interval=%ds)", settings.heartbeat_timeout)

    while True:
        try:
            released = await cleanup_expired_sessions()
            if released > 0:
                logger.info("Cleanup cycle: released %d numbers", released)
        except Exception:
            logger.exception("Error in cleanup cycle")

        await asyncio.sleep(settings.heartbeat_interval)
