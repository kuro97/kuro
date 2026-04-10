"""Скрипт начальной настройки: создаёт admin-пользователя, демо-проект и тестовые номера."""

import asyncio
import uuid

from app.core.auth import get_password_hash
from app.core.database import async_session, engine, Base
from app.core.redis import redis_client
from app.models.user import User
from app.models.project import Project
from app.models.tracking_number import TrackingNumber
from app.services.number_pool import NumberPoolManager

# Демо-номера (фейковые — для тестирования без реальных SIP)
DEMO_NUMBERS = [
    "+77001110001",
    "+77001110002",
    "+77001110003",
    "+77001110004",
    "+77001110005",
    "+77001110006",
    "+77001110007",
    "+77001110008",
]


async def seed():
    # Создаём таблицы
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as db:
        # 1. Admin user
        admin = User(
            id=uuid.uuid4(),
            email="admin@kurotrack.local",
            hashed_password=get_password_hash("admin123"),
            full_name="Admin",
            is_superuser=True,
        )
        db.add(admin)

        # 2. Demo project
        project = Project(
            id=uuid.uuid4(),
            name="Demo Site",
            domain="demo.kurotrack.local",
            default_phone="+77001234567",
            api_key="demo-api-key-for-testing",
        )
        db.add(project)

        # 3. Tracking numbers
        pool = NumberPoolManager(redis_client, str(project.id))
        for phone in DEMO_NUMBERS:
            tn = TrackingNumber(
                id=uuid.uuid4(),
                phone=phone,
                project_id=project.id,
                number_type="dynamic",
            )
            db.add(tn)
            await pool.add_number_to_pool(phone)

        await db.commit()

    print("=" * 50)
    print("Seed complete!")
    print(f"  Admin:    admin@kurotrack.local / admin123")
    print(f"  Project:  Demo Site (demo.kurotrack.local)")
    print(f"  API Key:  demo-api-key-for-testing")
    print(f"  Numbers:  {len(DEMO_NUMBERS)} demo numbers in pool")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(seed())
