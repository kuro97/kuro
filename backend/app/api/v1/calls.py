"""API для работы со звонками: список, статистика, фильтрация."""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.call import Call
from app.schemas.tracking import CallOut, CallStats

router = APIRouter(prefix="/calls", tags=["calls"])


@router.get("/", response_model=list[CallOut])
async def list_calls(
    project_id: str = Query(...),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    source: str | None = Query(None),
    disposition: str | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    db: AsyncSession = Depends(get_db),
):
    """Список звонков с фильтрацией."""
    query = select(Call).where(Call.project_id == project_id)

    if date_from:
        query = query.where(Call.started_at >= date_from)
    if date_to:
        query = query.where(Call.started_at <= date_to)
    if source:
        query = query.where(Call.source == source)
    if disposition:
        query = query.where(Call.disposition == disposition)

    query = query.order_by(Call.started_at.desc()).limit(limit).offset(offset)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/stats", response_model=CallStats)
async def call_stats(
    project_id: str = Query(...),
    days: int = Query(30, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Агрегированная статистика звонков за период."""
    since = datetime.utcnow() - timedelta(days=days)
    base = select(Call).where(Call.project_id == project_id, Call.started_at >= since)

    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    answered = await db.scalar(
        select(func.count()).select_from(
            base.where(Call.disposition == "ANSWERED").subquery()
        )
    )
    unique = await db.scalar(
        select(func.count()).select_from(base.where(Call.is_unique).subquery())
    )
    target = await db.scalar(
        select(func.count()).select_from(base.where(Call.is_target).subquery())
    )
    avg_dur = await db.scalar(
        select(func.avg(Call.billsec)).select_from(
            base.where(Call.disposition == "ANSWERED").subquery()
        )
    )

    return CallStats(
        total_calls=total or 0,
        answered_calls=answered or 0,
        missed_calls=(total or 0) - (answered or 0),
        unique_calls=unique or 0,
        target_calls=target or 0,
        avg_duration=round(avg_dur or 0, 1),
        answer_rate=round((answered or 0) / total * 100, 1) if total else 0,
    )
