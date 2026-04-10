"""API для работы со звонками: список, статистика, фильтрация, графики."""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select, func, cast, Date
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


class DailyPoint(BaseModel):
    date: str
    total: int
    answered: int
    missed: int


class SourcePoint(BaseModel):
    source: str
    total: int
    answered: int


@router.get("/chart/daily", response_model=list[DailyPoint])
async def daily_chart(
    project_id: str = Query(...),
    days: int = Query(30, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Звонки по дням за период — для графика на дашборде."""
    since = datetime.utcnow() - timedelta(days=days)

    # Все звонки по дням
    query = (
        select(
            cast(Call.started_at, Date).label("day"),
            func.count().label("total"),
            func.count().filter(Call.disposition == "ANSWERED").label("answered"),
        )
        .where(Call.project_id == project_id, Call.started_at >= since)
        .group_by("day")
        .order_by("day")
    )
    result = await db.execute(query)
    rows = result.all()

    return [
        DailyPoint(
            date=str(r.day),
            total=r.total,
            answered=r.answered,
            missed=r.total - r.answered,
        )
        for r in rows
    ]


@router.get("/chart/sources", response_model=list[SourcePoint])
async def sources_chart(
    project_id: str = Query(...),
    days: int = Query(30, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Звонки по источникам — для круговой диаграммы."""
    since = datetime.utcnow() - timedelta(days=days)

    query = (
        select(
            func.coalesce(Call.source, "direct").label("source"),
            func.count().label("total"),
            func.count().filter(Call.disposition == "ANSWERED").label("answered"),
        )
        .where(Call.project_id == project_id, Call.started_at >= since)
        .group_by("source")
        .order_by(func.count().desc())
        .limit(10)
    )
    result = await db.execute(query)
    rows = result.all()

    return [
        SourcePoint(source=r.source, total=r.total, answered=r.answered)
        for r in rows
    ]
