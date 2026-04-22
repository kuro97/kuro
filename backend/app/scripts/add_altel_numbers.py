"""Добавляет 6 Altel-номеров в KuroTrack.

Usage:
  python -m app.scripts.add_altel_numbers
"""

import asyncio
import csv
import uuid
from pathlib import Path

from sqlalchemy import select

from app.core.database import async_session
from app.core.phone import normalize_phone
from app.core.redis import redis_client
from app.models.project import Project
from app.models.tracking_number import TrackingNumber
from app.services.number_pool import NumberPoolManager

TRUNKS_CSV = Path(__file__).parent.parent.parent / "asterisk" / "trunks.csv"

# Назначение номеров: разделим на динамику (пул для сайта) и статику (2GIS по городам)
# Можно настроить под вашу стратегию.
# В .csv берём только номера без "CHANGE_ME" в паролях.

# По умолчанию делим так:
#   Первые N_DYNAMIC номеров -> динамический пул (для сайта)
#   Остальные -> статика с source_label (2GIS по городам)
N_DYNAMIC = 3  # сколько номеров в пул (остаток пойдёт в статику)

STATIC_LABELS = [
    "2gis_almaty",
    "2gis_astana",
    "2gis_shymkent",
    "2gis_atyrau",
    "2gis_aktobe",
    "facebook_leadform",
]


async def main():
    if not TRUNKS_CSV.exists():
        print(f"Error: {TRUNKS_CSV} not found.")
        print("Create asterisk/trunks.csv from trunks.csv.example first.")
        return 1

    # Читаем номера из CSV
    phones = []
    with open(TRUNKS_CSV) as f:
        reader = csv.DictReader(f)
        for row in reader:
            phone = row["phone"].strip()
            if phone and not phone.startswith("#"):
                phones.append(phone)

    if not phones:
        print("No phones in trunks.csv")
        return 1

    async with async_session() as db:
        # Находим демо-проект (или первый активный)
        result = await db.execute(select(Project).where(Project.is_active).limit(1))
        project = result.scalar_one_or_none()
        if not project:
            print("No active project found. Run seed first.")
            return 1

        pool = NumberPoolManager(redis_client, str(project.id))

        # Удаляем старые fake-номера из Redis-пула и БД (если были seed'ы)
        existing = await db.execute(
            select(TrackingNumber).where(TrackingNumber.project_id == project.id)
        )
        for old in existing.scalars().all():
            # Удаляем из Redis
            await redis_client.zrem(f"pool:{project.id}:free", old.phone)
            await redis_client.hdel(f"pool:{project.id}:map:number", old.phone)
            await db.delete(old)
        await db.commit()

        # Добавляем реальные Altel номера
        added_dynamic = 0
        added_static = 0

        for i, phone in enumerate(phones):
            # Форматируем в международный формат +7...
            formatted = f"+{phone}" if not phone.startswith("+") else phone

            if i < N_DYNAMIC:
                # Динамический пул
                tn = TrackingNumber(
                    id=uuid.uuid4(),
                    phone=formatted,
                    phone_normalized=normalize_phone(formatted),
                    project_id=project.id,
                    number_type="dynamic",
                )
                db.add(tn)
                await pool.add_number_to_pool(formatted)
                added_dynamic += 1
                print(f"  [dynamic] {formatted}")
            else:
                label_idx = i - N_DYNAMIC
                label = (
                    STATIC_LABELS[label_idx]
                    if label_idx < len(STATIC_LABELS)
                    else f"static_{label_idx}"
                )
                tn = TrackingNumber(
                    id=uuid.uuid4(),
                    phone=formatted,
                    phone_normalized=normalize_phone(formatted),
                    project_id=project.id,
                    number_type="static",
                    source_label=label,
                )
                db.add(tn)
                added_static += 1
                print(f"  [static]  {formatted} -> {label}")

        await db.commit()

    print()
    print(f"Added {added_dynamic} dynamic + {added_static} static numbers")
    print(f"Project: {project.name} ({project.domain})")
    print(f"API Key: {project.api_key}")


if __name__ == "__main__":
    asyncio.run(main())
