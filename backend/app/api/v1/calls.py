"""API для работы со звонками: список, статистика, фильтрация, графики."""

from datetime import datetime, timedelta, date

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select, func, cast, Date, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.call import Call
from app.schemas.tracking import CallOut, CallStats, StatsResponse, SourceStats, CityStats, DayStats

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


@router.get("/unattributed", response_model=list[CallOut])
async def list_unattributed(
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Звонки без атрибуции (project_id IS NULL) — для оператора, чтобы вручную разобрать."""
    since = datetime.utcnow() - timedelta(days=days)
    query = (
        select(Call)
        .where(Call.project_id.is_(None), Call.started_at >= since)
        .order_by(Call.started_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/stats", response_model=StatsResponse)
async def call_stats(
    project_id: str = Query(...),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Агрегированная статистика по звонкам за период: KPI + по источникам + по городам + по дням."""
    # Базовые условия фильтрации
    base_conditions = [Call.project_id == project_id]
    if date_from:
        base_conditions.append(Call.started_at >= date_from)
    if date_to:
        base_conditions.append(Call.started_at <= date_to)

    # --- Итоговые метрики ---
    total_q = select(func.count()).where(*base_conditions)
    answered_q = select(func.count()).where(*base_conditions, Call.disposition == "ANSWERED")
    qualified_q = select(func.count()).where(*base_conditions, Call.amo_qualified == True)
    paid_q = select(func.count()).where(*base_conditions, Call.amo_won == True)
    revenue_q = select(func.coalesce(func.sum(Call.amo_deal_amount), 0)).where(
        *base_conditions, Call.amo_won == True
    )

    total = (await db.scalar(total_q)) or 0
    answered = (await db.scalar(answered_q)) or 0
    qualified = (await db.scalar(qualified_q)) or 0
    paid = (await db.scalar(paid_q)) or 0
    revenue = (await db.scalar(revenue_q)) or 0

    qualified_pct = round(qualified * 100 / total, 1) if total else 0.0
    paid_pct = round(paid * 100 / total, 1) if total else 0.0

    # --- По источникам (NULL → "direct") ---
    src_q = (
        select(
            func.coalesce(Call.source, "direct").label("source"),
            func.count().label("total"),
            func.count().filter(Call.disposition == "ANSWERED").label("answered"),
            func.count().filter(Call.amo_qualified == True).label("qualified"),
            func.count().filter(Call.amo_won == True).label("paid"),
            func.coalesce(
                func.sum(Call.amo_deal_amount).filter(Call.amo_won == True), 0
            ).label("revenue"),
        )
        .where(*base_conditions)
        .group_by(func.coalesce(Call.source, "direct"))
        .order_by(func.count().desc())
    )
    src_rows = (await db.execute(src_q)).all()
    by_source = [
        SourceStats(
            source=r.source,
            total=r.total,
            answered=r.answered,
            qualified=r.qualified,
            paid=r.paid,
            revenue=r.revenue,
        )
        for r in src_rows
    ]

    # --- По городам (NULL → "Не указан") ---
    city_q = (
        select(
            func.coalesce(Call.amo_city, "Не указан").label("city"),
            func.count().label("total"),
            func.count().filter(Call.amo_qualified == True).label("qualified"),
            func.count().filter(Call.amo_won == True).label("paid"),
            func.coalesce(
                func.sum(Call.amo_deal_amount).filter(Call.amo_won == True), 0
            ).label("revenue"),
        )
        .where(*base_conditions)
        .group_by(func.coalesce(Call.amo_city, "Не указан"))
        .order_by(func.count().desc())
    )
    city_rows = (await db.execute(city_q)).all()
    by_city = [
        CityStats(
            city=r.city,
            total=r.total,
            qualified=r.qualified,
            paid=r.paid,
            revenue=r.revenue,
        )
        for r in city_rows
    ]

    # --- По дням (с заполнением пропущенных дней нулями) ---
    # Генерируем полный диапазон дат с нулями, чтобы график был непрерывным
    day_q = (
        select(
            cast(Call.started_at, Date).label("day"),
            func.count().label("total"),
            func.count().filter(Call.amo_qualified == True).label("qualified"),
            func.count().filter(Call.amo_won == True).label("paid"),
        )
        .where(*base_conditions)
        .group_by(cast(Call.started_at, Date))
        .order_by(cast(Call.started_at, Date))
    )
    day_rows = (await db.execute(day_q)).all()

    # Строим словарь day_str -> данные
    day_map: dict[str, DayStats] = {}
    for r in day_rows:
        day_str = str(r.day)
        day_map[day_str] = DayStats(day=day_str, total=r.total, qualified=r.qualified, paid=r.paid)

    # Определяем диапазон дат для заполнения пробелов
    if date_from and date_to:
        range_start = date_from.date()
        range_end = date_to.date()
    elif date_from:
        range_start = date_from.date()
        range_end = date.today()
    elif date_to:
        # Берём 30 дней до date_to
        range_start = (date_to - timedelta(days=30)).date()
        range_end = date_to.date()
    else:
        range_start = date.today() - timedelta(days=30)
        range_end = date.today()

    by_day: list[DayStats] = []
    current = range_start
    while current <= range_end:
        day_str = str(current)
        by_day.append(
            day_map.get(day_str, DayStats(day=day_str, total=0, qualified=0, paid=0))
        )
        current += timedelta(days=1)

    return StatsResponse(
        total=total,
        answered=answered,
        qualified=qualified,
        paid=paid,
        revenue=revenue,
        qualified_pct=qualified_pct,
        paid_pct=paid_pct,
        by_source=by_source,
        by_city=by_city,
        by_day=by_day,
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
