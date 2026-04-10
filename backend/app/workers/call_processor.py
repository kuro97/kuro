"""Обработчик событий звонков от Asterisk AMI.
Привязывает входящие звонки к сессиям посетителей через подменные номера."""

import logging
import uuid
from datetime import datetime, timezone

from app.core.database import async_session
from app.core.redis import redis_client
from app.models.call import Call
from app.services.number_pool import NumberPoolManager

logger = logging.getLogger(__name__)

# Кеш активных звонков (uniqueid → данные)
active_calls: dict[str, dict] = {}


async def process_call_event(event: dict):
    """Главный обработчик событий AMI. Маршрутизирует по типу события."""
    event_type = event.get("event")

    if event_type == "new_call":
        await _handle_new_call(event)
    elif event_type == "hangup":
        await _handle_hangup(event)
    elif event_type == "cdr":
        await _handle_cdr(event)


async def _handle_new_call(event: dict):
    """Входящий звонок. Ищем маппинг подменного номера → сессия."""
    uniqueid = event.get("uniqueid")
    exten = event.get("exten")  # DID — подменный номер
    caller = event.get("caller_id_num")

    if not exten or not caller:
        return

    logger.info("New call: %s → %s (uniqueid=%s)", caller, exten, uniqueid)

    active_calls[uniqueid] = {
        "caller": caller,
        "tracking_did": exten,
        "started_at": datetime.now(timezone.utc),
    }


async def _handle_hangup(event: dict):
    """Завершение звонка. Удаляем из кеша активных."""
    uniqueid = event.get("uniqueid")
    if uniqueid in active_calls:
        logger.info("Call ended: uniqueid=%s", uniqueid)


async def _handle_cdr(event: dict):
    """CDR-событие — полные данные о звонке. Сохраняем в БД с атрибуцией."""
    uniqueid = event.get("uniqueid")
    dst = event.get("dst")  # DID — подменный номер
    src = event.get("src")  # номер звонящего

    if not dst or not src:
        return

    # Ищем данные сессии по подменному номеру в Redis
    # Пробуем все проекты (в production — через lookup-таблицу number→project)
    session_data = None
    # Простой поиск: перебираем mapping ключи
    # В production это будет один HGET по глобальной таблице number→project_id
    keys = await redis_client.keys("pool:*:map:number")
    for key in keys:
        data = await redis_client.hget(key, dst)
        if data:
            import json
            session_data = json.loads(data)
            break

    # Сохраняем звонок в PostgreSQL
    async with async_session() as db:
        call = Call(
            id=uuid.uuid4(),
            uniqueid=uniqueid or str(uuid.uuid4()),
            caller_number=src,
            tracking_did=dst,
            duration=int(event.get("duration") or 0),
            billsec=int(event.get("billsec") or 0),
            disposition=event.get("disposition", "NO ANSWER"),
            started_at=active_calls.get(uniqueid, {}).get(
                "started_at", datetime.now(timezone.utc)
            ),
            is_target=int(event.get("billsec") or 0) >= 30,
        )

        # Атрибуция из сессии
        if session_data:
            call.source = session_data.get("source")
            call.medium = session_data.get("utm_medium")
            call.campaign = session_data.get("utm_campaign")
            call.keyword = session_data.get("utm_keyword")

        db.add(call)
        await db.commit()
        logger.info(
            "CDR saved: %s → %s, %ss, %s, source=%s",
            src, dst, call.billsec, call.disposition, call.source,
        )

    # Очистка кеша
    active_calls.pop(uniqueid, None)
