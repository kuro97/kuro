"""Генератор демо-звонков. Создаёт реалистичные тестовые данные для дашборда.
Запуск: python -m app.scripts.demo_calls"""

import asyncio
import random
import uuid
from datetime import datetime, timedelta, timezone

from app.core.database import async_session, engine, Base
from app.models.call import Call
from app.models.session import VisitorSession
from app.models.project import Project
from sqlalchemy import select

# Источники трафика с весами (чем больше — тем чаще)
SOURCES = [
    ("google", "cpc", "almaty_repair", 35),
    ("google", "organic", None, 20),
    ("yandex", "cpc", "astana_services", 15),
    ("yandex", "organic", None, 10),
    ("2gis", "referral", None, 8),
    (None, None, None, 7),  # direct
    ("instagram", "social", "promo_april", 3),
    ("whatsapp", "messenger", None, 2),
]

DISPOSITIONS = [
    ("ANSWERED", 65),
    ("NO ANSWER", 25),
    ("BUSY", 10),
]

CALLER_PREFIXES = ["+7701", "+7702", "+7705", "+7707", "+7708", "+7747", "+7771", "+7775", "+7776", "+7778"]

DEMO_NUMBERS = [
    "+77001110001", "+77001110002", "+77001110003", "+77001110004",
    "+77001110005", "+77001110006", "+77001110007", "+77001110008",
]

KEYWORDS = [
    "ремонт квартир алматы", "сантехник вызов", "электрик на дом",
    "ремонт офиса астана", "отделка квартир", "дизайн интерьера",
    "строительная компания", None, None, None,
]


def weighted_choice(items):
    total = sum(w for _, w in items)
    r = random.uniform(0, total)
    cumulative = 0
    for item, weight in items:
        cumulative += weight
        if r <= cumulative:
            return item
    return items[-1][0]


def random_phone():
    prefix = random.choice(CALLER_PREFIXES)
    return prefix + "".join([str(random.randint(0, 9)) for _ in range(7)])


def random_source():
    options = [(s, w) for (*s, w) in SOURCES]
    source_tuple = weighted_choice(options)
    return source_tuple  # (source, medium, campaign)


def random_disposition():
    options = [(d, w) for d, w in DISPOSITIONS]
    return weighted_choice(options)


async def generate_demo_calls(days: int = 30, calls_per_day: tuple = (15, 45)):
    """Генерирует демо-звонки за последние N дней."""

    async with async_session() as db:
        # Находим демо-проект
        result = await db.execute(select(Project).where(Project.domain == "demo.kurotrack.local"))
        project = result.scalar_one_or_none()
        if not project:
            print("Demo project not found. Run seed first: python -m app.scripts.seed")
            return

        project_id = project.id
        now = datetime.now(timezone.utc)
        total_created = 0
        unique_callers = set()

        for day_offset in range(days, 0, -1):
            day_start = now - timedelta(days=day_offset)
            num_calls = random.randint(*calls_per_day)

            # Меньше звонков в выходные
            if day_start.weekday() >= 5:
                num_calls = int(num_calls * 0.4)

            for _ in range(num_calls):
                # Случайное время в рабочие часы (8:00 - 20:00)
                hour = random.randint(8, 19)
                minute = random.randint(0, 59)
                call_time = day_start.replace(hour=hour, minute=minute, second=random.randint(0, 59))

                caller = random_phone()
                source_data = random_source()
                source, medium, campaign = source_data
                disposition = random_disposition()
                tracking_did = random.choice(DEMO_NUMBERS)

                # Длительность
                if disposition == "ANSWERED":
                    billsec = random.choices(
                        [random.randint(5, 29), random.randint(30, 180), random.randint(180, 600)],
                        weights=[30, 50, 20],
                    )[0]
                    duration = billsec + random.randint(5, 15)
                else:
                    billsec = 0
                    duration = random.randint(10, 30)

                is_unique = caller not in unique_callers
                unique_callers.add(caller)

                # Создаём сессию
                session = VisitorSession(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    client_id=f"demo_{uuid.uuid4().hex[:12]}",
                    source=source,
                    medium=medium,
                    campaign=campaign,
                    keyword=random.choice(KEYWORDS) if source in ("google", "yandex") and medium == "cpc" else None,
                    referrer=f"https://www.{source}.com/" if source else None,
                    landing_page=f"https://demo.kurotrack.local/{random.choice(['', 'services', 'contacts', 'about'])}",
                    created_at=call_time - timedelta(minutes=random.randint(1, 30)),
                    last_activity=call_time,
                )
                db.add(session)

                # Создаём звонок
                call = Call(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    session_id=session.id,
                    uniqueid=f"demo-{uuid.uuid4().hex[:16]}",
                    caller_number=caller,
                    tracking_did=tracking_did,
                    target_number=project.default_phone,
                    started_at=call_time,
                    duration=duration,
                    billsec=billsec,
                    disposition=disposition,
                    is_unique=is_unique,
                    is_target=billsec >= 30,
                    source=source,
                    medium=medium,
                    campaign=campaign,
                    keyword=session.keyword,
                )
                db.add(call)
                total_created += 1

        await db.commit()

    print(f"Generated {total_created} demo calls over {days} days")
    print(f"Unique callers: {len(unique_callers)}")


if __name__ == "__main__":
    asyncio.run(generate_demo_calls())
