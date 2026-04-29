"""API для работы со звонками: список, статистика, фильтрация, графики."""

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, date

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select, func, cast, Date, text, literal_column, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db, async_session
from app.core.redis import redis_client
from app.models.call import Call
from app.schemas.tracking import CallOut, CallStats, StatsResponse, SourceStats, CityStats, DayStats

router = APIRouter(prefix="/calls", tags=["calls"])


def _dispo_rank():
    """Ранг диспозиции: чем меньше — тем лучше (ANSWERED — победитель)."""
    return case(
        (Call.disposition == "ANSWERED", 1),
        (Call.disposition == "NO ANSWER", 2),
        (Call.disposition == "BUSY", 3),
        (Call.disposition == "FAILED", 4),
        else_=5,
    ).label("dispo_rank")


def _group_key():
    """Ключ дедупликации: linkedid если есть, иначе uniqueid (старые записи не схлопываются)."""
    return func.coalesce(Call.linkedid, Call.uniqueid)


def _dedup_ids_subquery(base_conditions: list):
    """
    Subquery возвращает id записей-победителей (DISTINCT ON group_key).
    Каждый физический звонок представлен одной строкой с лучшей диспозицией.
    """
    group_key = _group_key()
    dispo_rank = _dispo_rank()
    return (
        select(Call.id)
        .where(*base_conditions)
        .order_by(group_key, dispo_rank, Call.billsec.desc(), Call.started_at.asc())
        .distinct(group_key)
        .subquery()
    )


@router.get("/", response_model=list[CallOut])
async def list_calls(
    project_id: str = Query(...),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    source: str | None = Query(None),
    disposition: str | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    dedupe: bool = Query(True, description="True — один звонок на linkedid; False — все legs"),
    db: AsyncSession = Depends(get_db),
):
    """Список звонков с фильтрацией.
    dedupe=True (по умолчанию) — дедупликация по linkedid, один физический звонок = одна запись.
    dedupe=False — все сырые legs без схлопывания.
    """
    # Собираем условия фильтрации
    base_conditions: list = [Call.project_id == project_id]
    if date_from:
        base_conditions.append(Call.started_at >= date_from)
    if date_to:
        base_conditions.append(Call.started_at <= date_to)
    if source:
        base_conditions.append(Call.source == source)
    if disposition:
        base_conditions.append(Call.disposition == disposition)

    if dedupe:
        # Subquery: выбираем id "лучшего" leg для каждого физического звонка
        dedup_subq = _dedup_ids_subquery(base_conditions)
        query = (
            select(Call)
            .where(Call.id.in_(select(dedup_subq.c.id)))
            .order_by(Call.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
    else:
        # Все legs без дедупликации
        query = (
            select(Call)
            .where(*base_conditions)
            .order_by(Call.started_at.desc())
            .limit(limit)
            .offset(offset)
        )

    result = await db.execute(query)
    return result.scalars().all()


@router.get("/unattributed", response_model=list[CallOut])
async def list_unattributed(
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    dedupe: bool = Query(True, description="True — один звонок на linkedid; False — все legs"),
    db: AsyncSession = Depends(get_db),
):
    """Звонки без атрибуции (project_id IS NULL) — для оператора, чтобы вручную разобрать.
    dedupe=True (по умолчанию) — дедупликация по linkedid.
    dedupe=False — все сырые legs.
    """
    since = datetime.utcnow() - timedelta(days=days)
    base_conditions = [Call.project_id.is_(None), Call.started_at >= since]

    if dedupe:
        # Subquery: выбираем id "лучшего" leg для каждого физического звонка
        dedup_subq = _dedup_ids_subquery(base_conditions)
        query = (
            select(Call)
            .where(Call.id.in_(select(dedup_subq.c.id)))
            .order_by(Call.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
    else:
        # Все legs без дедупликации
        query = (
            select(Call)
            .where(*base_conditions)
            .order_by(Call.started_at.desc())
            .limit(limit)
            .offset(offset)
        )

    result = await db.execute(query)
    return result.scalars().all()


async def _kpi_query(base_conditions: list, dedup_ids_subq) -> dict:
    """KPI-метрики в отдельной сессии: total, answered, qualified, paid, revenue, with_utm."""
    dedup_ids = select(dedup_ids_subq.c.id)
    async with async_session() as db:
        total_q = select(func.count()).where(Call.id.in_(dedup_ids))
        answered_q = select(func.count()).where(Call.id.in_(dedup_ids), Call.disposition == "ANSWERED")
        qualified_q = select(func.count()).where(Call.id.in_(dedup_ids), Call.amo_qualified == True)
        paid_q = select(func.count()).where(Call.id.in_(dedup_ids), Call.amo_won == True)
        revenue_q = select(func.coalesce(func.sum(Call.amo_deal_amount), 0)).where(
            Call.id.in_(dedup_ids), Call.amo_won == True
        )
        with_utm_q = select(func.count()).where(
            Call.id.in_(dedup_ids),
            (Call.medium.is_not(None)) | (Call.campaign.is_not(None)) | (Call.keyword.is_not(None)),
        )
        # Выполняем последовательно внутри одной сессии (они быстрые — COUNT без GROUP BY)
        total = (await db.scalar(total_q)) or 0
        answered = (await db.scalar(answered_q)) or 0
        qualified = (await db.scalar(qualified_q)) or 0
        paid = (await db.scalar(paid_q)) or 0
        revenue = (await db.scalar(revenue_q)) or 0
        with_utm = (await db.scalar(with_utm_q)) or 0
    return {
        "total": total,
        "answered": answered,
        "qualified": qualified,
        "paid": paid,
        "revenue": revenue,
        "with_utm": with_utm,
    }


async def _total_attempts_query(base_conditions: list) -> int:
    """Все legs без дедупликации — в отдельной сессии."""
    async with async_session() as db:
        q = select(func.count()).where(*base_conditions)
        return (await db.scalar(q)) or 0


async def _by_source_query(dedup_ids_subq) -> list[SourceStats]:
    """Статистика по источникам трафика — в отдельной сессии."""
    dedup_ids = select(dedup_ids_subq.c.id)
    async with async_session() as db:
        src_q = (
            select(
                Call.source.label("source"),
                func.count().label("total"),
                func.count().filter(Call.disposition == "ANSWERED").label("answered"),
                func.count().filter(Call.amo_qualified == True).label("qualified"),
                func.count().filter(Call.amo_won == True).label("paid"),
                func.coalesce(
                    func.sum(Call.amo_deal_amount).filter(Call.amo_won == True), 0
                ).label("revenue"),
            )
            .where(Call.id.in_(dedup_ids))
            .group_by(Call.source)
            .order_by(func.count().desc())
        )
        rows = (await db.execute(src_q)).all()
    return [
        SourceStats(
            source=r.source if r.source is not None else "direct",
            total=r.total,
            answered=r.answered,
            qualified=r.qualified,
            paid=r.paid,
            revenue=r.revenue,
        )
        for r in rows
    ]


async def _by_city_query(dedup_ids_subq) -> list[CityStats]:
    """Статистика по городам — в отдельной сессии."""
    dedup_ids = select(dedup_ids_subq.c.id)
    async with async_session() as db:
        city_q = (
            select(
                Call.amo_city.label("city"),
                func.count().label("total"),
                func.count().filter(Call.amo_qualified == True).label("qualified"),
                func.count().filter(Call.amo_won == True).label("paid"),
                func.coalesce(
                    func.sum(Call.amo_deal_amount).filter(Call.amo_won == True), 0
                ).label("revenue"),
            )
            .where(Call.id.in_(dedup_ids))
            .group_by(Call.amo_city)
            .order_by(func.count().desc())
        )
        rows = (await db.execute(city_q)).all()
    return [
        CityStats(
            city=r.city if r.city is not None else "Не указан",
            total=r.total,
            qualified=r.qualified,
            paid=r.paid,
            revenue=r.revenue,
        )
        for r in rows
    ]


async def _by_day_query(dedup_ids_subq) -> list[tuple]:
    """Статистика по дням — в отдельной сессии. Возвращает сырые строки."""
    dedup_ids = select(dedup_ids_subq.c.id)
    async with async_session() as db:
        day_q = (
            select(
                cast(Call.started_at, Date).label("day"),
                func.count().label("total"),
                func.count().filter(Call.amo_qualified == True).label("qualified"),
                func.count().filter(Call.amo_won == True).label("paid"),
            )
            .where(Call.id.in_(dedup_ids))
            .group_by(cast(Call.started_at, Date))
            .order_by(cast(Call.started_at, Date))
        )
        return (await db.execute(day_q)).all()


@router.get("/stats", response_model=StatsResponse)
async def call_stats(
    project_id: str = Query(...),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Агрегированная статистика по звонкам за период: KPI + по источникам + по городам + по дням.
    Возвращает total (уникальных по linkedid) и total_attempts (все legs без дедупликации).
    Параллельные SQL через asyncio.gather + Redis-кеш 30 секунд."""

    # --- Redis-кеш: проверяем до выполнения тяжёлых запросов ---
    cache_key_raw = f"stats:{project_id}:{date_from}:{date_to}"
    cache_key = "stats:" + hashlib.md5(cache_key_raw.encode()).hexdigest()

    try:
        cached = await redis_client.get(cache_key)
        if cached:
            return StatsResponse.model_validate_json(cached)
    except Exception:
        # Redis недоступен — продолжаем без кеша, не падаем
        pass

    # Базовые условия фильтрации
    base_conditions = [Call.project_id == project_id]
    if date_from:
        base_conditions.append(Call.started_at >= date_from)
    if date_to:
        base_conditions.append(Call.started_at <= date_to)

    # Subquery с DISTINCT ON group_key — один id на физический звонок
    # Строим subquery один раз и передаём во все параллельные запросы
    dedup_subq = _dedup_ids_subquery(base_conditions)

    # --- Параллельный запуск всех агрегаций через asyncio.gather ---
    # Каждая функция открывает свою сессию, т.к. одна AsyncSession не поддерживает параллелизм
    kpi, total_attempts, by_source, by_city, day_rows = await asyncio.gather(
        _kpi_query(base_conditions, dedup_subq),
        _total_attempts_query(base_conditions),
        _by_source_query(dedup_subq),
        _by_city_query(dedup_subq),
        _by_day_query(dedup_subq),
    )

    total = kpi["total"]
    answered = kpi["answered"]
    qualified = kpi["qualified"]
    paid = kpi["paid"]
    revenue = kpi["revenue"]
    with_utm = kpi["with_utm"]

    qualified_pct = round(qualified * 100 / total, 1) if total else 0.0
    paid_pct = round(paid * 100 / total, 1) if total else 0.0

    # --- По дням: заполняем пропущенные дни нулями чтобы график был непрерывным ---
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

    response = StatsResponse(
        total=total,
        total_attempts=total_attempts,
        answered=answered,
        qualified=qualified,
        paid=paid,
        revenue=revenue,
        qualified_pct=qualified_pct,
        paid_pct=paid_pct,
        with_utm=with_utm,
        by_source=by_source,
        by_city=by_city,
        by_day=by_day,
    )

    # --- Сохраняем в Redis на 30 секунд (TTL — компромисс свежесть/нагрузка) ---
    try:
        await redis_client.set(cache_key, response.model_dump_json(), ex=30)
    except Exception:
        # Redis недоступен — не падаем, просто без кеша
        pass

    return response


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
