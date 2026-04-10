"""Обработчик событий звонков от Asterisk AMI.
Привязывает входящие звонки к сессиям, классифицирует, отправляет в аналитику и CRM."""

import json
import logging
import uuid
from datetime import datetime, timezone

from app.core.database import async_session
from app.core.redis import redis_client
from app.models.call import Call
from app.models.project import Project
from app.services.analytics import analytics
from app.services.call_quality import classify_call
from app.services.recordings import recording_service
from app.services.webhook import webhook_sender

from sqlalchemy import select

logger = logging.getLogger(__name__)

# Кеш активных звонков (uniqueid → данные)
active_calls: dict[str, dict] = {}

# Глобальный маппинг number → project_id (заполняется при pool sync)
_number_project_cache: dict[str, str] = {}


async def _find_session_by_did(did: str) -> tuple[dict | None, str | None]:
    """Ищет данные сессии по подменному номеру. Возвращает (session_data, project_id)."""
    keys = await redis_client.keys("pool:*:map:number")
    for key in keys:
        data = await redis_client.hget(key, did)
        if data:
            project_id = key.split(":")[1]
            return json.loads(data), project_id
    return None, None


async def process_call_event(event: dict):
    """Главный обработчик событий AMI."""
    event_type = event.get("event")

    if event_type == "new_call":
        await _handle_new_call(event)
    elif event_type == "hangup":
        await _handle_hangup(event)
    elif event_type == "cdr":
        await _handle_cdr(event)


async def _handle_new_call(event: dict):
    """Входящий звонок."""
    uniqueid = event.get("uniqueid")
    exten = event.get("exten")
    caller = event.get("caller_id_num")

    if not exten or not caller:
        return

    logger.info("New call: %s -> %s (uniqueid=%s)", caller, exten, uniqueid)
    active_calls[uniqueid] = {
        "caller": caller,
        "tracking_did": exten,
        "started_at": datetime.now(timezone.utc),
    }


async def _handle_hangup(event: dict):
    """Завершение звонка."""
    uniqueid = event.get("uniqueid")
    if uniqueid in active_calls:
        logger.info("Call ended: uniqueid=%s", uniqueid)


async def _handle_cdr(event: dict):
    """CDR — полные данные о звонке. Основная логика обработки."""
    uniqueid = event.get("uniqueid")
    dst = event.get("dst")  # DID
    src = event.get("src")  # Caller

    if not dst or not src:
        return

    # 1. Ищем сессию по подменному номеру
    session_data, project_id = await _find_session_by_did(dst)
    billsec = int(event.get("billsec") or 0)
    disposition = event.get("disposition", "NO ANSWER")

    async with async_session() as db:
        # 2. Классифицируем звонок (уникальный, целевой, спам)
        classification = {"is_unique": False, "is_target": billsec >= 30, "is_spam": False}
        if project_id:
            classification = await classify_call(db, project_id, src, billsec)

        # 3. Создаём запись
        call = Call(
            id=uuid.uuid4(),
            uniqueid=uniqueid or str(uuid.uuid4()),
            caller_number=src,
            tracking_did=dst,
            duration=int(event.get("duration") or 0),
            billsec=billsec,
            disposition=disposition,
            started_at=active_calls.get(uniqueid, {}).get(
                "started_at", datetime.now(timezone.utc)
            ),
            is_unique=classification["is_unique"],
            is_target=classification["is_target"],
        )

        if project_id:
            call.project_id = uuid.UUID(project_id)

        # 4. Атрибуция из сессии
        if session_data:
            call.source = session_data.get("source")
            call.medium = session_data.get("utm_medium")
            call.campaign = session_data.get("utm_campaign")
            call.keyword = session_data.get("utm_keyword")

        # 5. Запись звонка — проверяем локальный файл
        recording_path = recording_service.get_local_path(
            call.uniqueid, call.tracking_did
        )
        recording_url = await recording_service.upload_recording(
            recording_path, str(call.id)
        )
        if recording_url:
            call.recording_url = recording_url

        db.add(call)
        await db.commit()

        logger.info(
            "CDR saved: %s -> %s, %ss, %s, source=%s, unique=%s, target=%s, spam=%s",
            src, dst, billsec, disposition, call.source,
            classification["is_unique"], classification["is_target"],
            classification["is_spam"],
        )

        # 6. Отправляем в аналитику (GA4, Яндекс.Метрика)
        if session_data and not classification["is_spam"]:
            call_dict = {
                "caller_number": src,
                "tracking_did": dst,
                "duration": call.duration,
                "billsec": billsec,
                "disposition": disposition,
                "source": call.source,
                "medium": call.medium,
                "campaign": call.campaign,
                "keyword": call.keyword,
                "is_unique": classification["is_unique"],
                "is_target": classification["is_target"],
                "recording_url": call.recording_url,
                "started_at": call.started_at.isoformat() if call.started_at else "",
            }

            await analytics.dispatch_call(
                client_id=session_data.get("session_id", ""),
                call_data=call_dict,
            )

            # 7. Webhook в CRM
            if project_id:
                project_result = await db.execute(
                    select(Project).where(Project.id == project_id)
                )
                project = project_result.scalar_one_or_none()
                if project and project.webhook_url:
                    await webhook_sender.send_call_event(project.webhook_url, call_dict)

    # Очистка кеша
    active_calls.pop(uniqueid, None)
