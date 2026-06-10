"""Репозиторий для работы со звонками: весь SQL вынесен сюда из API-роутера."""

import asyncio
from datetime import datetime, timedelta, date, timezone

from sqlalchemy import select, func, cast, Date, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session
from app.models.call import Call
from app.schemas.tracking import SourceStats, CityStats, DayStats


# ---------------------------------------------------------------------------
# Вспомогательные выражения (были в calls.py как module-level helpers)
# ---------------------------------------------------------------------------

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
    """Ключ дедупликации: linkedid если есть, иначе uniqueid."""
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


# ---------------------------------------------------------------------------
# Вспомогательный dataclass для строки по дням (был DayRow в calls.py)
# ---------------------------------------------------------------------------

class DayRow:
    """Простая обёртка строки статистики по дням."""
    def __init__(self, day, total, qualified, paid):
        self.day = day
        self.total = total
        self.qualified = qualified
        self.paid = paid


# ---------------------------------------------------------------------------
# Методы репозитория
# ---------------------------------------------------------------------------

async def list_calls(
    db: AsyncSession,
    project_id: str,
    date_from: datetime | None,
    date_to: datetime | None,
    source: str | None,
    disposition: str | None,
    dedupe: bool,
    limit: int,
    offset: int,
) -> tuple[list, int]:
    """Список звонков с пагинацией и фильтрацией.

    Возвращает (items, total).
    dedupe=True — один физический звонок (победитель по linkedid).
    dedupe=False — все legs без дедупликации.
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
        dedup_ids_sel = select(dedup_subq.c.id)

        count_q = select(func.count()).where(Call.id.in_(dedup_ids_sel))
        total = (await db.execute(count_q)).scalar() or 0

        query = (
            select(Call)
            .where(Call.id.in_(dedup_ids_sel))
            .order_by(Call.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
    else:
        # Все legs без дедупликации
        count_q = select(func.count()).where(*base_conditions)
        total = (await db.execute(count_q)).scalar() or 0

        query = (
            select(Call)
            .where(*base_conditions)
            .order_by(Call.started_at.desc())
            .limit(limit)
            .offset(offset)
        )

    result = await db.execute(query)
    items = result.scalars().all()
    return items, total


async def list_unattributed(
    db: AsyncSession,
    days: int,
    dedupe: bool,
    limit: int,
    offset: int,
) -> list:
    """Звонки без атрибуции (project_id IS NULL).

    dedupe=True — дедупликация по linkedid.
    dedupe=False — все legs.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    base_conditions = [Call.project_id.is_(None), Call.started_at >= since]

    if dedupe:
        dedup_subq = _dedup_ids_subquery(base_conditions)
        query = (
            select(Call)
            .where(Call.id.in_(select(dedup_subq.c.id)))
            .order_by(Call.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
    else:
        query = (
            select(Call)
            .where(*base_conditions)
            .order_by(Call.started_at.desc())
            .limit(limit)
            .offset(offset)
        )

    result = await db.execute(query)
    return result.scalars().all()


async def get_kpi(base_conditions: list, dedup_ids_subq) -> dict:
    """KPI-метрики в отдельной сессии: total, answered, qualified, paid, revenue, with_utm.

    total / answered / with_utm — по дедуплицированным звонкам (один физический звонок = один leg-победитель).
    qualified / paid / revenue — по DISTINCT amo_lead_id среди ВСЕХ legs:
    внутри одного linkedid может быть несколько legs, и leg-победитель (ANSWERED) часто не тот,
    который реально создавал лид в AMO. Поэтому считаем лиды напрямую по amo_lead_id, а не через dedup.
    """
    dedup_ids = select(dedup_ids_subq.c.id)
    async with async_session() as db:
        total_q = select(func.count()).where(Call.id.in_(dedup_ids))
        answered_q = select(func.count()).where(Call.id.in_(dedup_ids), Call.disposition == "ANSWERED")
        with_utm_q = select(func.count()).where(
            Call.id.in_(dedup_ids),
            (Call.medium.is_not(None)) | (Call.campaign.is_not(None)) | (Call.keyword.is_not(None)),
        )

        # qualified / paid / revenue — по DISTINCT amo_lead_id из всех legs (не только победителей)
        qualified_q = select(
            func.count(func.distinct(Call.amo_lead_id)).filter(Call.amo_qualified == True)
        ).where(*base_conditions, Call.amo_lead_id.is_not(None))
        paid_q = select(
            func.count(func.distinct(Call.amo_lead_id)).filter(Call.amo_won == True)
        ).where(*base_conditions, Call.amo_lead_id.is_not(None))
        # revenue: сумма deal_amount по уникальным выигранным лидам
        revenue_subq = (
            select(Call.amo_lead_id, func.max(Call.amo_deal_amount).label("amount"))
            .where(*base_conditions, Call.amo_won == True, Call.amo_lead_id.is_not(None))
            .group_by(Call.amo_lead_id)
            .subquery()
        )
        revenue_q = select(func.coalesce(func.sum(revenue_subq.c.amount), 0))

        total = (await db.scalar(total_q)) or 0
        answered = (await db.scalar(answered_q)) or 0
        with_utm = (await db.scalar(with_utm_q)) or 0
        qualified = (await db.scalar(qualified_q)) or 0
        paid = (await db.scalar(paid_q)) or 0
        revenue = (await db.scalar(revenue_q)) or 0
    return {
        "total": total,
        "answered": answered,
        "qualified": qualified,
        "paid": paid,
        "revenue": revenue,
        "with_utm": with_utm,
    }


async def get_total_attempts(base_conditions: list) -> int:
    """Все legs без дедупликации — в отдельной сессии."""
    async with async_session() as db:
        q = select(func.count()).where(*base_conditions)
        return (await db.scalar(q)) or 0


async def get_by_source(base_conditions: list, dedup_ids_subq) -> list[SourceStats]:
    """Статистика по источникам трафика — в отдельной сессии.

    total / answered — по дедуп-subquery (физические звонки).
    qualified / paid / revenue — по DISTINCT amo_lead_id из всех legs.
    """
    dedup_ids = select(dedup_ids_subq.c.id)
    async with async_session() as db:
        # total / answered — из дедуп-subquery
        dedup_q = (
            select(
                Call.source.label("source"),
                func.count().label("total"),
                func.count().filter(Call.disposition == "ANSWERED").label("answered"),
            )
            .where(Call.id.in_(dedup_ids))
            .group_by(Call.source)
        )
        dedup_rows = {r.source: r for r in (await db.execute(dedup_q)).all()}

        # qualified / paid — DISTINCT amo_lead_id из всех legs
        leads_q = (
            select(
                Call.source.label("source"),
                func.count(func.distinct(Call.amo_lead_id)).filter(
                    Call.amo_qualified == True
                ).label("qualified"),
                func.count(func.distinct(Call.amo_lead_id)).filter(
                    Call.amo_won == True
                ).label("paid"),
            )
            .where(*base_conditions, Call.amo_lead_id.is_not(None))
            .group_by(Call.source)
        )
        leads_rows = {r.source: r for r in (await db.execute(leads_q)).all()}

        # revenue: одна сумма на лид (max deal_amount) — агрегируем через подзапрос
        rev_subq = (
            select(
                Call.source.label("source"),
                Call.amo_lead_id.label("lead_id"),
                func.max(Call.amo_deal_amount).label("amount"),
            )
            .where(*base_conditions, Call.amo_won == True, Call.amo_lead_id.is_not(None))
            .group_by(Call.source, Call.amo_lead_id)
            .subquery()
        )
        rev_q = (
            select(
                rev_subq.c.source,
                func.coalesce(func.sum(rev_subq.c.amount), 0).label("revenue"),
            )
            .group_by(rev_subq.c.source)
        )
        revenue_rows = {r.source: r.revenue for r in (await db.execute(rev_q)).all()}

    # Объединяем: источник берём из дедуп (он полнее по total)
    all_sources = set(dedup_rows.keys()) | set(leads_rows.keys())
    result = []
    for src in all_sources:
        dr = dedup_rows.get(src)
        lr = leads_rows.get(src)
        result.append(
            SourceStats(
                source=src if src is not None else "direct",
                total=dr.total if dr else 0,
                answered=dr.answered if dr else 0,
                qualified=lr.qualified if lr else 0,
                paid=lr.paid if lr else 0,
                revenue=revenue_rows.get(src, 0),
            )
        )
    result.sort(key=lambda s: s.total, reverse=True)
    return result


async def get_by_city(base_conditions: list, dedup_ids_subq) -> list[CityStats]:
    """Статистика по городам — в отдельной сессии.

    total — по дедуп-subquery (физические звонки).
    qualified / paid / revenue — по DISTINCT amo_lead_id из всех legs.
    """
    dedup_ids = select(dedup_ids_subq.c.id)
    async with async_session() as db:
        # total — из дедуп-subquery
        dedup_q = (
            select(
                Call.amo_city.label("city"),
                func.count().label("total"),
            )
            .where(Call.id.in_(dedup_ids))
            .group_by(Call.amo_city)
        )
        dedup_rows = {r.city: r.total for r in (await db.execute(dedup_q)).all()}

        # qualified / paid — DISTINCT amo_lead_id из всех legs
        leads_q = (
            select(
                Call.amo_city.label("city"),
                func.count(func.distinct(Call.amo_lead_id)).filter(
                    Call.amo_qualified == True
                ).label("qualified"),
                func.count(func.distinct(Call.amo_lead_id)).filter(
                    Call.amo_won == True
                ).label("paid"),
            )
            .where(*base_conditions, Call.amo_lead_id.is_not(None))
            .group_by(Call.amo_city)
        )
        leads_rows = {r.city: r for r in (await db.execute(leads_q)).all()}

        # revenue — подзапрос чтобы не задублировать сумму на несколько legs одного лида
        rev_subq = (
            select(
                Call.amo_city.label("city"),
                Call.amo_lead_id.label("lead_id"),
                func.max(Call.amo_deal_amount).label("amount"),
            )
            .where(*base_conditions, Call.amo_won == True, Call.amo_lead_id.is_not(None))
            .group_by(Call.amo_city, Call.amo_lead_id)
            .subquery()
        )
        rev_q = (
            select(
                rev_subq.c.city,
                func.coalesce(func.sum(rev_subq.c.amount), 0).label("revenue"),
            )
            .group_by(rev_subq.c.city)
        )
        revenue_rows = {r.city: r.revenue for r in (await db.execute(rev_q)).all()}

    all_cities = set(dedup_rows.keys()) | set(leads_rows.keys())
    result = []
    for city in all_cities:
        lr = leads_rows.get(city)
        result.append(
            CityStats(
                city=city if city is not None else "Не указан",
                total=dedup_rows.get(city, 0),
                qualified=lr.qualified if lr else 0,
                paid=lr.paid if lr else 0,
                revenue=revenue_rows.get(city, 0),
            )
        )
    result.sort(key=lambda c: c.total, reverse=True)
    return result


async def get_by_day(base_conditions: list, dedup_ids_subq) -> list[DayRow]:
    """Статистика по дням — в отдельной сессии.

    total — дедуп-subquery. qualified / paid — DISTINCT amo_lead_id из всех legs.
    """
    dedup_ids = select(dedup_ids_subq.c.id)
    async with async_session() as db:
        # total по дедуп (физические звонки)
        dedup_q = (
            select(
                cast(Call.started_at, Date).label("day"),
                func.count().label("total"),
            )
            .where(Call.id.in_(dedup_ids))
            .group_by(cast(Call.started_at, Date))
        )
        dedup_rows = {r.day: r.total for r in (await db.execute(dedup_q)).all()}

        # qualified / paid — по DISTINCT amo_lead_id
        leads_q = (
            select(
                cast(Call.started_at, Date).label("day"),
                func.count(func.distinct(Call.amo_lead_id)).filter(
                    Call.amo_qualified == True
                ).label("qualified"),
                func.count(func.distinct(Call.amo_lead_id)).filter(
                    Call.amo_won == True
                ).label("paid"),
            )
            .where(*base_conditions, Call.amo_lead_id.is_not(None))
            .group_by(cast(Call.started_at, Date))
        )
        leads_rows = {r.day: r for r in (await db.execute(leads_q)).all()}

    all_days = sorted(set(dedup_rows.keys()) | set(leads_rows.keys()))
    return [
        DayRow(
            day=d,
            total=dedup_rows.get(d, 0),
            qualified=leads_rows[d].qualified if d in leads_rows else 0,
            paid=leads_rows[d].paid if d in leads_rows else 0,
        )
        for d in all_days
    ]


async def get_dashboard_stats(
    base_conditions: list,
) -> tuple[dict, int, list[SourceStats], list[CityStats], list[DayRow]]:
    """Параллельный запуск всех агрегаций для stats-эндпоинта.

    Строит dedup_subquery один раз и передаёт во все 5 параллельных запросов.
    Возвращает (kpi_dict, total_attempts, by_source, by_city, day_rows).
    """
    dedup_subq = _dedup_ids_subquery(base_conditions)
    kpi, total_attempts, by_source, by_city, day_rows = await asyncio.gather(
        get_kpi(base_conditions, dedup_subq),
        get_total_attempts(base_conditions),
        get_by_source(base_conditions, dedup_subq),
        get_by_city(base_conditions, dedup_subq),
        get_by_day(base_conditions, dedup_subq),
    )
    return kpi, total_attempts, by_source, by_city, day_rows


async def daily_chart(
    db: AsyncSession,
    project_id: str,
    days: int,
) -> list:
    """Звонки по дням за период — для графика на дашборде.

    Возвращает сырые Row-объекты с полями: day, total, answered.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
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
    return result.all()


async def sources_chart(
    db: AsyncSession,
    project_id: str,
    days: int,
) -> list:
    """Звонки по источникам за период — для круговой диаграммы.

    Возвращает сырые Row-объекты с полями: source, total, answered.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
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
    return result.all()
