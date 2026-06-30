#!/usr/bin/env python3
"""Cron: каждые 5 минут раздаёт зависшие kurotrack-лиды без города менеджерам.

Логика:
  - Берём kurotrack-лиды за последние 7 дней
  - Фильтр: status_id=33378589 (НОВАЯ ЗАЯВКА), responsible=2275621 (admin),
             поле Город пустое или «Другой», лид старше 10 минут (даём Salesbot время)
  - Round-robin курсор хранится в Redis: ключ auto_assign:rr_cursor
  - Логи в /tmp/kurotrack-autoassign.log

Запускается через cron каждые 5 минут с flock.
Молчит если нечего распределять.
"""
import asyncio, sys, datetime, logging, json
sys.path.insert(0, "/home/alisher/kurotrack/backend")

import httpx
import redis.asyncio as aioredis
from app.core.config import settings
from app.core.amo_constants import FIELD_CITY, ENUM_CITY_DRUGOY, STATUS_LOST

logging.basicConfig(
    filename="/tmp/kurotrack-autoassign.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("autoassign")

# Аккаунт-«биржа», на котором висят нераспределённые лиды
ADMIN_ID = 2275621
# Статус «НОВАЯ ЗАЯВКА» — только такие лиды трогаем
STATUS_NEW = 33378589
# Окно поиска — лиды за последние 7 дней
WINDOW_DAYS = 7
# Пауза перед назначением — даём Salesbot время отработать городские лиды
MIN_AGE_SECONDS = 600  # 10 минут
# Redis-ключ для хранения позиции round-robin между запусками
REDIS_CURSOR_KEY = "auto_assign:rr_cursor"

# -----------------------------------------------------------------------
# Список менеджеров для round-robin раздачи.
# Обновлять: добавить/убрать кортеж (user_id, "Имя") из AMO.
# user_id берётся из /api/v4/users (поле id).
# -----------------------------------------------------------------------
MANAGERS = [
    (2275624, "Ерканат Серикович"),
    (2275630, "Адиль Максимулы"),
    (2381836, "Ақбөпе Талғатқызы"),
    (2807500, "Нурбек Талгатулы"),
    (2892409, "Еркебұлан Рашатұлы"),
    (3841648, "Арайлым Агайдарова"),
    (7349137, "Арита Ришадовна"),
    (8469901, "Алихан Серикович"),
    (8480965, "Сауле Жаркынбеккызы"),
    (9178070, "Салауат Таншолпан"),
    (9178074, "Асель Толеужанкызы"),
    (9399906, "Елдар Берикулы"),
    (9399918, "Каракат Куаткызы"),
    (9399922, "Молдир Нурлыханкызы"),
    (9399938, "Темирлан Аленбекулы"),
    (9399954, "Айгерим Мейрамовна"),
    (9399958, "Кулпынай Казбеккызы"),
    (9399970, "Ерсаин Ахметулы"),
    (9399990, "Аружан Маралбековна"),
    (9400006, "Тогжан Болебаевна"),
    (9400010, "Аида Асланкызы"),
    (9400018, "Алишер Айдосулы"),
    (9400090, "Зуппарова Умида"),
    (9400138, "Калшабек Нурислам"),
    (9400162, "Мухамеджан Аружан"),
    (12832058, "Жаксылык Асланбек"),
    (12955374, "Рахыманова Айым"),
    (13456338, "Дюсенов Ерсана"),
    (13531362, "Темиргалиев Рашид"),
    (13630778, "Ермаханбетова Зияда"),
    (13630786, "Мамадияров Рауль"),
    (13774770, "Сатибайулы Салихат"),
    (13781482, "Тулегенов Данияр"),
    (13807742, "Дамитхан Асхатович"),
    (13824610, "Байзакова Акжибек"),
    (13842746, "Амина Курманбекқызы"),
    (13905410, "Сапуанова Камшат"),
]


async def get_rr_cursor(redis_client) -> int:
    """Читаем текущую позицию round-robin из Redis."""
    val = await redis_client.get(REDIS_CURSOR_KEY)
    if val is None:
        return 0
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


async def set_rr_cursor(redis_client, cursor: int) -> None:
    """Сохраняем позицию round-robin в Redis."""
    await redis_client.set(REDIS_CURSOR_KEY, cursor)


async def fetch_leads(http_client, headers: dict, from_ts: int) -> list:
    """Собираем все kurotrack-лиды постранично начиная с from_ts."""
    leads = []
    page = 1
    while True:
        try:
            r = await http_client.get(
                f"https://{settings.amo_subdomain}.amocrm.ru/api/v4/leads",
                headers=headers,
                params={
                    "query": "kurotrack",
                    "limit": 250,
                    "page": page,
                    "filter[created_at][from]": from_ts,
                },
            )
        except Exception as e:
            log.error(f"GET leads page={page} exception={e}")
            break

        if r.status_code == 204:
            break
        if r.status_code != 200:
            log.error(f"GET leads page={page} HTTP {r.status_code}: {r.text[:150]}")
            break

        page_leads = r.json().get("_embedded", {}).get("leads", [])
        if not page_leads:
            break
        leads.extend(page_leads)
        if len(page_leads) < 250:
            break
        page += 1
        if page > 20:
            log.warning("Достигнут лимит страниц (20) при сборе лидов")
            break

    return leads


async def main():
    now_ts = int(datetime.datetime.utcnow().timestamp())
    from_ts = now_ts - WINDOW_DAYS * 86400  # за последние 7 дней
    cutoff_ts = now_ts - MIN_AGE_SECONDS     # старше 10 минут

    h = {"Authorization": f"Bearer {settings.amo_token}", "Content-Type": "application/json"}

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            all_leads = await fetch_leads(c, h, from_ts)

            # Фильтруем вручную: API может вернуть лиды вне диапазона
            in_window = [l for l in all_leads if l.get("created_at", 0) >= from_ts]

            # Находим лиды для назначения
            to_assign = []
            for lead in in_window:
                # Только нераспределённые в статусе НОВАЯ ЗАЯВКА
                if lead.get("status_id") != STATUS_NEW:
                    continue
                if lead.get("responsible_user_id") != ADMIN_ID:
                    continue
                # Даём Salesbot время — пропускаем свежие лиды
                if lead.get("created_at", 0) > cutoff_ts:
                    continue
                # Проверяем поле Город — НЕ трогаем лиды с заполненным городом
                city_val = None
                for f in lead.get("custom_fields_values") or []:
                    if f.get("field_id") == FIELD_CITY:
                        vals = f.get("values") or []
                        if vals:
                            city_val = vals[0].get("value")
                # Лиды с городом (кроме «Другой») раздаёт Salesbot — пропускаем
                if city_val is not None and city_val != "Другой":
                    continue
                to_assign.append(lead["id"])

            if not to_assign:
                # Молчим — нечего распределять
                return

            # Получаем текущую позицию round-robin из Redis
            cursor = await get_rr_cursor(redis_client)

            assigned_count = 0
            fail_count = 0

            for lead_id in to_assign:
                mgr_id, mgr_name = MANAGERS[cursor % len(MANAGERS)]
                cursor += 1

                try:
                    pr = await c.patch(
                        f"https://{settings.amo_subdomain}.amocrm.ru/api/v4/leads/{lead_id}",
                        headers=h,
                        json={"responsible_user_id": mgr_id},
                    )
                    if pr.status_code == 200:
                        assigned_count += 1
                        log.info(f"assigned lead={lead_id} to={mgr_name}({mgr_id})")
                    else:
                        fail_count += 1
                        log.warning(f"PATCH lead={lead_id} HTTP {pr.status_code}: {pr.text[:100]}")
                except Exception as e:
                    fail_count += 1
                    log.error(f"PATCH lead={lead_id} exception={e}")

            # Сохраняем обновлённый курсор чтобы следующий запуск продолжил по кругу
            await set_rr_cursor(redis_client, cursor)

            if assigned_count > 0 or fail_count > 0:
                log.info(f"=== run done: assigned={assigned_count} fail={fail_count} next_cursor={cursor} ===")

    finally:
        await redis_client.aclose()


asyncio.run(main())
