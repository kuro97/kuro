"""API для работы со звонками: список, статистика, фильтрацию, графики.

SQL-логика вынесена в app.repositories.call_repository.
Хендлеры здесь тонкие: получают параметры, вызывают репозиторий, формируют ответ.
"""

import hashlib
from datetime import datetime, timedelta, date, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.core.redis import redis_client
from app.models.user import User
from app.repositories import call_repository as repo
from app.schemas.tracking import CallOut, CallListResponse, CallStats, StatsResponse, SourceStats, CityStats, DayStats

router = APIRouter(prefix="/calls", tags=["calls"])


@router.get("/", response_model=CallListResponse)
async def list_calls(
    project_id: str = Query(...),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    source: str | None = Query(None),
    disposition: str | None = Query(None),
    limit: int = Query(100, le=200),
    offset: int = Query(0),
    dedupe: bool = Query(True, description="True — один звонок на linkedid; False — все legs"),
    current_user: User = Depends(get_current_user),  # требуем JWT-авторизацию
    db: AsyncSession = Depends(get_db),
):
    """Список звонков с фильтрацией и total count для пагинации.

    dedupe=True (по умолчанию) — дедупликация по linkedid, один физический звонок = одна запись.
    dedupe=False — все сырые legs без схлопывания.
    Возвращает {items: [...], total: int}.
    """
    items, total = await repo.list_calls(
        db=db,
        project_id=project_id,
        date_from=date_from,
        date_to=date_to,
        source=source,
        disposition=disposition,
        dedupe=dedupe,
        limit=limit,
        offset=offset,
    )
    return CallListResponse(items=items, total=total)


@router.get("/unattributed", response_model=list[CallOut])
async def list_unattributed(
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    dedupe: bool = Query(True, description="True — один звонок на linkedid; False — все legs"),
    current_user: User = Depends(get_current_user),  # требуем JWT-авторизацию
    db: AsyncSession = Depends(get_db),
):
    """Звонки без атрибуции (project_id IS NULL) — для оператора, чтобы вручную разобрать.

    dedupe=True (по умолчанию) — дедупликация по linkedid.
    dedupe=False — все сырые legs.
    """
    return await repo.list_unattributed(
        db=db,
        days=days,
        dedupe=dedupe,
        limit=limit,
        offset=offset,
    )


@router.get("/stats", response_model=StatsResponse)
async def call_stats(
    project_id: str = Query(...),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    current_user: User = Depends(get_current_user),  # требуем JWT-авторизацию
    db: AsyncSession = Depends(get_db),
):
    """Агрегированная статистика по звонкам за период: KPI + по источникам + по городам + по дням.

    Возвращает total (уникальных по linkedid) и total_attempts (все legs без дедупликации).
    Параллельные SQL через asyncio.gather + Redis-кеш 30 секунд.
    """
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

    # Базовые условия фильтрации для репозитория
    from app.models.call import Call
    base_conditions = [Call.project_id == project_id]
    if date_from:
        base_conditions.append(Call.started_at >= date_from)
    if date_to:
        base_conditions.append(Call.started_at <= date_to)

    # Параллельный запуск всех агрегаций
    kpi, total_attempts, by_source, by_city, day_rows = await repo.get_dashboard_stats(base_conditions)

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
    current_user: User = Depends(get_current_user),  # требуем JWT-авторизацию
    db: AsyncSession = Depends(get_db),
):
    """Звонки по дням за период — для графика на дашборде."""
    rows = await repo.daily_chart(db=db, project_id=project_id, days=days)
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
    current_user: User = Depends(get_current_user),  # требуем JWT-авторизацию
    db: AsyncSession = Depends(get_db),
):
    """Звонки по источникам — для круговой диаграммы."""
    rows = await repo.sources_chart(db=db, project_id=project_id, days=days)
    return [
        SourcePoint(source=r.source, total=r.total, answered=r.answered)
        for r in rows
    ]
