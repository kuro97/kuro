"""Расширенный health-роутер.
Возвращает реальное состояние всех внешних зависимостей (AMI, БД, Redis).
Никогда не бросает 5xx — ошибки превращаются в False-флаги.
"""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis
from app.services.ami_client import ami_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str        # всегда "ok" если процесс жив
    service: str       # "kurotrack"
    ami_connected: bool  # AMIClient.is_connected
    db_ok: bool          # SELECT 1 отработал без ошибки
    redis_ok: bool       # PING отработал без ошибки


async def _check_db(db: AsyncSession) -> bool:
    """Проверяет доступность базы данных через SELECT 1."""
    try:
        await db.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("DB health check failed")
        return False


async def _check_redis(redis) -> bool:
    """Проверяет доступность Redis через PING."""
    try:
        await redis.ping()
        return True
    except Exception:
        logger.exception("Redis health check failed")
        return False


@router.get("/health", response_model=HealthResponse)
async def health(
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> HealthResponse:
    """Возвращает состояние всех внешних зависимостей.
    Никогда не возвращает 5xx: при ошибках зависимостей — False в соответствующем флаге.
    """
    db_ok = await _check_db(db)
    redis_ok = await _check_redis(redis)

    return HealthResponse(
        status="ok",
        service="kurotrack",
        ami_connected=ami_client.is_connected,
        db_ok=db_ok,
        redis_ok=redis_ok,
    )
