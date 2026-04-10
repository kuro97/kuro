"""API для управления подменными номерами в пуле."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis
from app.models.tracking_number import TrackingNumber
from app.services.number_pool import NumberPoolManager

router = APIRouter(prefix="/numbers", tags=["numbers"])


class NumberCreate(BaseModel):
    phone: str
    project_id: str
    number_type: str = "dynamic"  # dynamic | static
    source_label: str | None = None
    freeze_time: int = 900


class NumberOut(BaseModel):
    id: uuid.UUID
    phone: str
    project_id: uuid.UUID | None
    number_type: str
    source_label: str | None
    is_active: bool
    freeze_time: int

    model_config = {"from_attributes": True}


class BulkAddRequest(BaseModel):
    project_id: str
    phones: list[str]


class BulkAddResponse(BaseModel):
    added: int
    skipped: int


@router.post("/", response_model=NumberOut, status_code=201)
async def add_number(
    body: NumberCreate,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """Добавить номер в систему и в Redis-пул."""
    existing = await db.execute(
        select(TrackingNumber).where(TrackingNumber.phone == body.phone)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Number already exists")

    number = TrackingNumber(
        phone=body.phone,
        project_id=uuid.UUID(body.project_id),
        number_type=body.number_type,
        source_label=body.source_label,
        freeze_time=body.freeze_time,
    )
    db.add(number)
    await db.commit()
    await db.refresh(number)

    # Добавляем в Redis-пул если dynamic
    if body.number_type == "dynamic":
        pool = NumberPoolManager(redis, body.project_id)
        await pool.add_number_to_pool(body.phone)

    return number


@router.post("/bulk", response_model=BulkAddResponse)
async def bulk_add_numbers(
    body: BulkAddRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """Массовое добавление номеров в пул."""
    added = 0
    skipped = 0
    pool = NumberPoolManager(redis, body.project_id)

    for phone in body.phones:
        existing = await db.execute(
            select(TrackingNumber).where(TrackingNumber.phone == phone)
        )
        if existing.scalar_one_or_none():
            skipped += 1
            continue

        number = TrackingNumber(
            phone=phone,
            project_id=uuid.UUID(body.project_id),
            number_type="dynamic",
        )
        db.add(number)
        await pool.add_number_to_pool(phone)
        added += 1

    await db.commit()
    return BulkAddResponse(added=added, skipped=skipped)


@router.get("/", response_model=list[NumberOut])
async def list_numbers(
    project_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Список всех номеров проекта."""
    result = await db.execute(
        select(TrackingNumber)
        .where(TrackingNumber.project_id == project_id)
        .order_by(TrackingNumber.phone)
    )
    return result.scalars().all()


@router.delete("/{number_id}")
async def delete_number(
    number_id: str,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """Удалить номер из системы и пула."""
    result = await db.execute(
        select(TrackingNumber).where(TrackingNumber.id == number_id)
    )
    number = result.scalar_one_or_none()
    if not number:
        raise HTTPException(status_code=404, detail="Number not found")

    # Удаляем из Redis-пула
    if number.project_id:
        pool = NumberPoolManager(redis, str(number.project_id))
        await pool.release_number(number.phone)
        # Также удаляем из sorted set
        await redis.zrem(f"pool:{number.project_id}:free", number.phone)

    await db.delete(number)
    await db.commit()
    return {"ok": True}
