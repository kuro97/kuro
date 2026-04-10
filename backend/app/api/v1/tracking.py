"""API для JS-скрипта: выдача подменных номеров и heartbeat."""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis
from app.models.project import Project
from app.models.session import VisitorSession
from app.schemas.tracking import (
    GetNumberRequest,
    GetNumberResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    PoolStats,
)
from app.services.number_pool import NumberPoolManager

router = APIRouter(prefix="/tracking", tags=["tracking"])


async def _get_project_by_api_key(
    x_api_key: str = Header(...), db: AsyncSession = Depends(get_db)
) -> Project:
    result = await db.execute(select(Project).where(Project.api_key == x_api_key, Project.is_active))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return project


@router.post("/get-number", response_model=GetNumberResponse)
async def get_tracking_number(
    body: GetNumberRequest,
    request: Request,
    project: Project = Depends(_get_project_by_api_key),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """Выдаёт подменный номер для сессии посетителя. Вызывается JS-скриптом при загрузке страницы."""
    pool = NumberPoolManager(redis, str(project.id))

    number = await pool.allocate_number(
        session_id=body.client_id,
        source=body.source,
        utm_campaign=body.campaign,
        utm_medium=body.medium,
        utm_keyword=body.keyword,
    )

    if not number:
        # Пул исчерпан — возвращаем дефолтный номер
        return GetNumberResponse(
            phone=project.default_phone,
            session_id=body.client_id,
        )

    # Сохраняем сессию в PostgreSQL
    session = VisitorSession(
        id=uuid.uuid4(),
        project_id=project.id,
        client_id=body.client_id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        source=body.source,
        medium=body.medium,
        campaign=body.campaign,
        keyword=body.keyword,
        content=body.content,
        gclid=body.gclid,
        referrer=body.referrer,
        landing_page=body.landing_page,
    )
    db.add(session)
    await db.commit()

    return GetNumberResponse(phone=number, session_id=body.client_id)


@router.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    body: HeartbeatRequest,
    project: Project = Depends(_get_project_by_api_key),
    redis=Depends(get_redis),
):
    """Heartbeat от JS-скрипта. Продлевает удержание номера за сессией."""
    pool = NumberPoolManager(redis, str(project.id))
    ok = await pool.heartbeat(body.session_id)
    return HeartbeatResponse(ok=ok)


@router.get("/pool-stats", response_model=PoolStats)
async def pool_stats(
    project: Project = Depends(_get_project_by_api_key),
    redis=Depends(get_redis),
):
    """Статистика пула номеров."""
    pool = NumberPoolManager(redis, str(project.id))
    stats = await pool.get_pool_stats()
    return PoolStats(**stats)
