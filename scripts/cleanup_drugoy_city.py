#!/usr/bin/env python3
"""Cron: каждые 5 минут чистит поле 'Город'='Другой' у kurotrack-лидов.

Логика:
  - Берём из AMO лиды с UTM_REFERRER=kurotrack за последние 7 дней
  - Если поле Город = "Другой" (enum 860179) и source НЕ 2gis/taplink → PATCH очистка
  - Логи в /tmp/kurotrack-cleanup.log

Запускается через cron каждые 5 минут с flock.
"""
import asyncio, os, sys, datetime, logging, json
sys.path.insert(0, "/home/alisher/kurotrack/backend")

import httpx
from app.core.config import settings

logging.basicConfig(
    filename="/tmp/kurotrack-cleanup.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("cleanup")

FIELD_CITY = 879211
GEO_SOURCES_PREFIXES = ("2gis_", "taplink_")


async def main():
    h = {"Authorization": f"Bearer {settings.amo_token}", "Content-Type": "application/json"}
    week_ago = int((datetime.datetime.utcnow() - datetime.timedelta(days=7)).timestamp())

    cleared = 0
    scanned = 0
    fail = 0

    async with httpx.AsyncClient(timeout=15) as c:
        page = 1
        while True:
            try:
                r = await c.get(
                    f"https://{settings.amo_subdomain}.amocrm.ru/api/v4/leads",
                    headers=h,
                    params={
                        "query": "kurotrack",
                        "limit": 250,
                        "page": page,
                        "filter[created_at][from]": week_ago,
                    },
                )
            except Exception as e:
                log.error(f"GET leads page={page} exception={e}")
                break

            if r.status_code == 204:
                break
            if r.status_code != 200:
                log.error(f"GET leads page={page} HTTP {r.status_code}")
                break

            leads = r.json().get("_embedded", {}).get("leads", [])
            if not leads:
                break

            for lead in leads:
                scanned += 1
                src = ""
                city = None
                for f in lead.get("custom_fields_values") or []:
                    code = f.get("field_code")
                    fid = f.get("field_id")
                    vals = f.get("values") or []
                    if code == "UTM_SOURCE":
                        src = vals[0].get("value", "") if vals else ""
                    if fid == FIELD_CITY:
                        if vals:
                            city = vals[0].get("value")

                # Пропускаем 2gis/taplink — у них город реально привязан к источнику
                if src.startswith(GEO_SOURCES_PREFIXES):
                    continue
                # Пропускаем лиды без поля город или не с "Другой"
                if city != "Другой":
                    continue

                # Чистим
                try:
                    pr = await c.patch(
                        f"https://{settings.amo_subdomain}.amocrm.ru/api/v4/leads/{lead['id']}",
                        headers=h,
                        json={"custom_fields_values": [{"field_id": FIELD_CITY, "values": None}]},
                    )
                    if pr.status_code == 200:
                        cleared += 1
                        log.info(f"cleared city for lead id={lead['id']} src={src}")
                    else:
                        fail += 1
                        log.warning(f"PATCH {lead['id']} HTTP {pr.status_code}: {pr.text[:100]}")
                except Exception as e:
                    fail += 1
                    log.error(f"PATCH {lead['id']} exception={e}")

            if len(leads) < 250:
                break
            page += 1
            if page > 20:
                break

    if cleared > 0 or fail > 0:
        log.info(f"=== run done: scanned={scanned} cleared={cleared} fail={fail}")

asyncio.run(main())
