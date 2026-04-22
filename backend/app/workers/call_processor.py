"""Обработчик событий звонков от Asterisk AMI.
Привязывает входящие звонки к сессиям, классифицирует, отправляет в аналитику и CRM."""

import json
import logging
import uuid
from datetime import datetime, timezone

from app.core.database import async_session
from app.core.phone import normalize_phone
from app.core.redis import redis_client
from app.models.call import Call
from app.models.project import Project
from app.models.tracking_number import TrackingNumber
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


async def _find_session_by_did(did_raw: str) -> tuple[dict | None, str | None]:
    """Ищет данные сессии по подменному номеру (DID).

    Нормализует did_raw и сравнивает с нормализованными ключами хэша.
    Возвращает (session_data, project_id) или (None, None) если не найдено.
    O(N) по количеству номеров — допустимо при <100 номерах.
    """
    did_norm = normalize_phone(did_raw)
    if not did_norm:
        return None, None

    keys = await redis_client.keys("pool:*:map:number")
    for key in keys:
        # hgetall возвращает все пары phone→json_data для данного пула
        all_numbers = await redis_client.hgetall(key)
        for phone, data in all_numbers.items():
            if normalize_phone(phone) == did_norm:
                # Извлекаем project_id из ключа вида pool:<project_id>:map:number
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
    """CDR — полные данные о звонке. Основная логика обработки.

    DID читается в порядке: user_field (пробрасывается dialplan-ом через
    Set(CDR(userfield)=${EXTEN})) → dst (внутренний extension, fallback).
    Оба значения нормализуются через normalize_phone перед матчем.
    Если project_id не найден — Call всё равно сохраняется с project_id=None
    (неатрибуцированный звонок), чтобы не терять данные.
    """
    uniqueid = event.get("uniqueid")
    user_field = event.get("user_field")
    dst = event.get("dst")
    src = event.get("src")

    # DID читается в порядке приоритета:
    # 1. Redis inbound_did:{uniqueid} — захвачен из Newchannel (from-trunk, DID из SIP INVITE)
    # 2. Redis inbound_did:{linkedid} — если uniqueid не совпал (bridged channel)
    # 3. user_field — Set(CDR(userfield)=...) из dialplan (fallback, на будущее)
    # 4. dst — последняя надежда (extension менеджера, обычно не DID)
    linkedid = event.get("linkedid")
    redis_did: str | None = None
    try:
        redis_did = (
            await redis_client.get(f"inbound_did:{uniqueid}")
            or (await redis_client.get(f"inbound_did:{linkedid}") if linkedid else None)
        )
    except Exception:
        logger.exception("Ошибка чтения inbound_did из Redis: uniqueid=%s", uniqueid)

    did_raw = redis_did or user_field or dst

    # Чистим Redis-ключи сразу после чтения, чтобы не засорять
    if redis_did and uniqueid:
        try:
            await redis_client.delete(f"inbound_did:{uniqueid}")
            if linkedid:
                await redis_client.delete(f"inbound_did:{linkedid}")
        except Exception:
            logger.exception("Ошибка удаления inbound_did из Redis: uniqueid=%s", uniqueid)

    if not did_raw:
        logger.warning(
            "CDR without did: uniqueid=%s user_field=%s dst=%s",
            uniqueid, user_field, dst,
        )
        return

    if not src:
        logger.warning(
            "CDR without src: uniqueid=%s user_field=%s dst=%s",
            uniqueid, user_field, dst,
        )
        return

    # Нормализуем DID для матча
    did_norm = normalize_phone(did_raw)

    # 1. Ищем сессию по нормализованному DID в Redis
    try:
        session_data, project_id = await _find_session_by_did(did_raw)
    except Exception:
        logger.exception(
            "Ошибка поиска сессии: uniqueid=%s user_field=%s dst=%s src=%s",
            uniqueid, user_field, dst, src,
        )
        session_data, project_id = None, None

    # 2. Если в Redis не нашли — ищем проект напрямую через TrackingNumber.phone_normalized
    if project_id is None:
        try:
            async with async_session() as db:
                result = await db.execute(
                    select(TrackingNumber).where(
                        TrackingNumber.phone_normalized == did_norm
                    )
                )
                tracking_number = result.scalar_one_or_none()
                if tracking_number:
                    project_id = str(tracking_number.project_id)
        except Exception:
            logger.exception(
                "Ошибка поиска TrackingNumber: uniqueid=%s user_field=%s dst=%s src=%s",
                uniqueid, user_field, dst, src,
            )
            project_id = None

    # Логируем неатрибуцированные звонки
    if project_id is None:
        logger.warning(
            "Unattributed call: did=%s user_field=%s dst=%s uniqueid=%s",
            did_raw, user_field, dst, uniqueid,
        )

    billsec = int(event.get("billsec") or 0)
    disposition = event.get("disposition", "NO ANSWER")

    try:
        async with async_session() as db:
            # 3. Классифицируем звонок (уникальный, целевой, спам)
            classification = {"is_unique": False, "is_target": billsec >= 30, "is_spam": False}
            if project_id:
                try:
                    classification = await classify_call(db, project_id, src, billsec)
                except Exception:
                    logger.exception(
                        "Ошибка классификации звонка: uniqueid=%s user_field=%s dst=%s src=%s",
                        uniqueid, user_field, dst, src,
                    )

            # 4. Создаём запись — project_id может быть None (неатрибуцированный звонок)
            call = Call(
                id=uuid.uuid4(),
                uniqueid=uniqueid or str(uuid.uuid4()),
                caller_number=src,
                # Сохраняем исходный DID для дебага; нормализованный — в tracking_did
                tracking_did=did_norm,
                duration=int(event.get("duration") or 0),
                billsec=billsec,
                disposition=disposition,
                started_at=active_calls.get(uniqueid, {}).get(
                    "started_at", datetime.now(timezone.utc)
                ),
                is_unique=classification["is_unique"],
                is_target=classification["is_target"],
                # project_id=None разрешён (nullable=True после миграции 0002)
                project_id=uuid.UUID(project_id) if project_id else None,
            )

            # 5. Атрибуция из сессии
            if session_data:
                call.source = session_data.get("source")
                call.medium = session_data.get("utm_medium")
                call.campaign = session_data.get("utm_campaign")
                call.keyword = session_data.get("utm_keyword")

            # 6. Запись звонка — проверяем локальный файл
            try:
                recording_path = recording_service.get_local_path(
                    call.uniqueid, call.tracking_did
                )
                recording_url = await recording_service.upload_recording(
                    recording_path, str(call.id)
                )
                if recording_url:
                    call.recording_url = recording_url
            except Exception:
                logger.exception(
                    "Ошибка загрузки записи: uniqueid=%s user_field=%s dst=%s src=%s",
                    uniqueid, user_field, dst, src,
                )

            db.add(call)
            await db.commit()

            logger.info(
                "CDR saved: %s -> %s (did=%s), %ss, %s, project=%s, source=%s, unique=%s, target=%s, spam=%s",
                src, did_raw, did_norm, billsec, disposition, project_id,
                call.source, classification["is_unique"],
                classification["is_target"], classification["is_spam"],
            )

            # 7. Отправляем в аналитику (GA4, Яндекс.Метрика)
            if session_data and not classification["is_spam"]:
                call_dict = {
                    "caller_number": src,
                    "tracking_did": did_raw,
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

                try:
                    await analytics.dispatch_call(
                        client_id=session_data.get("session_id", ""),
                        call_data=call_dict,
                    )
                except Exception:
                    logger.exception(
                        "Ошибка отправки в аналитику: uniqueid=%s user_field=%s dst=%s src=%s",
                        uniqueid, user_field, dst, src,
                    )

                # 8. Webhook в CRM
                if project_id:
                    try:
                        project_result = await db.execute(
                            select(Project).where(Project.id == uuid.UUID(project_id))
                        )
                        project = project_result.scalar_one_or_none()
                        if project and project.webhook_url:
                            await webhook_sender.send_call_event(project.webhook_url, call_dict)
                    except Exception:
                        logger.exception(
                            "Ошибка отправки webhook: uniqueid=%s user_field=%s dst=%s src=%s",
                            uniqueid, user_field, dst, src,
                        )

    except Exception:
        logger.exception(
            "Failed to persist CDR: uniqueid=%s user_field=%s dst=%s src=%s",
            uniqueid, user_field, dst, src,
        )

    # Очистка кеша
    active_calls.pop(uniqueid, None)
