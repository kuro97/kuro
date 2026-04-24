"""API для JS-скрипта: выдача подменных номеров, heartbeat, resolve-did."""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.phone import normalize_phone
from app.core.redis import get_redis
from app.models.project import Project
from app.models.tracking_number import TrackingNumber
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

    # Находим tracking_number_id по выданному номеру, чтобы связать сессию.
    # Без этой связки атрибуция входящего звонка не найдёт UTM.
    tn_row = await db.execute(
        select(TrackingNumber).where(
            TrackingNumber.phone_normalized == normalize_phone(number)
        )
    )
    tn = tn_row.scalar_one_or_none()

    # Сохраняем сессию в PostgreSQL
    session = VisitorSession(
        id=uuid.uuid4(),
        project_id=project.id,
        tracking_number_id=tn.id if tn else None,
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


# --- Resolve DID (вызывается AGI-скриптом из Asterisk) ---


class ResolveDIDRequest(BaseModel):
    did: str
    caller: str


class ResolveDIDResponse(BaseModel):
    target_number: str
    campaign_id: str
    session_id: str
    record_path: str


@router.post("/resolve-did", response_model=ResolveDIDResponse)
async def resolve_did(
    body: ResolveDIDRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """Определяет маршрут по подменному DID-номеру. Вызывается AGI-скриптом при входящем звонке."""
    # Нормализуем DID и ищем по phone_normalized, чтобы матч работал независимо от формата
    # ('+77004982670' и '7004982670' — один и тот же номер)
    normalized_did = normalize_phone(body.did)
    result = await db.execute(
        select(TrackingNumber).where(
            TrackingNumber.phone_normalized == normalized_did,
            TrackingNumber.is_active,
        )
    )
    tn = result.scalar_one_or_none()

    if not tn or not tn.project_id:
        return ResolveDIDResponse(
            target_number="100",
            campaign_id="",
            session_id="",
            record_path="/var/spool/asterisk/monitor",
        )

    # Получаем данные сессии из Redis
    pool = NumberPoolManager(redis, str(tn.project_id))
    session_data = await pool.get_session_by_number(body.did)

    # Получаем проект для определения target
    project_result = await db.execute(select(Project).where(Project.id == tn.project_id))
    project = project_result.scalar_one_or_none()

    return ResolveDIDResponse(
        target_number=project.default_phone if project else "100",
        campaign_id=session_data.get("utm_campaign", "") if session_data else "",
        session_id=session_data.get("session_id", "") if session_data else "",
        record_path="/var/spool/asterisk/monitor",
    )
