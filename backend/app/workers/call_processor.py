"""Обработчик событий звонков от Asterisk AMI.
Привязывает входящие звонки к сессиям, классифицирует, отправляет в аналитику и CRM."""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.database import async_session
from app.core.phone import normalize_phone
from app.core.redis import redis_client
from app.models.call import Call
from app.models.project import Project
from app.models.tracking_number import TrackingNumber
from app.services.amocrm import amocrm_client
from app.services.analytics import analytics
from app.services.call_quality import classify_call
from app.services.recordings import recording_service
from app.services.webhook import webhook_sender

# Исключения БД для retry-логики: транзиентные сбои пула/соединения ретраим,
# IntegrityError (дубль) — НЕТ.
from asyncpg.exceptions import (
    TooManyConnectionsError,
    ConnectionDoesNotExistError,
    PostgresConnectionError,
)
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    OperationalError,
    TimeoutError as SATimeoutError,
)

logger = logging.getLogger(__name__)

# Кеш активных звонков (uniqueid → данные)
active_calls: dict[str, dict] = {}

# Глобальный маппинг number → project_id (заполняется при pool sync)
_number_project_cache: dict[str, str] = {}

# Нормализация source: приводим кастомные значения utm_source
# (которые маркетологи вводят в URL) к каноническим именам источников
# чтобы фильтры/KPI не дробились.
_SOURCE_ALIASES = {
    "google_alish": "google_ads",
    "google": "google_ads",
    "google_cpc": "google_ads",
    "fb": "facebook",
    "fb_ads": "facebook",
    "ig": "instagram",
}

# Ограничиваем одновременную обработку CDR чтобы залп веток не выжрал весь пул
_cdr_semaphore = asyncio.Semaphore(25)


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
        await _retry_handle_cdr(event)


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


async def _find_existing_lead_for_caller(db, caller_number: str, exclude_call_id, window_minutes: int = 30):
    """Ищет существующий AMO лид для этого номера за последние window_minutes минут.

    Используется для дедупликации multi-leg звонков: когда bridge создаёт несколько
    SIP-leg'ов одного звонка, только первый создаёт лид, остальные привязываются к нему.
    Возвращает amo_lead_id или None.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    result = await db.execute(
        select(Call.amo_lead_id)
        .where(
            Call.caller_number == caller_number,
            Call.amo_lead_id.is_not(None),
            Call.started_at >= cutoff,
            Call.id != exclude_call_id,
        )
        .order_by(Call.started_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _resolve_did(
    uniqueid: str | None,
    linkedid: str | None,
    user_field: str | None,
    dst: str | None,
) -> str | None:
    """Определяет DID (подменный номер) для звонка.

    Порядок приоритета:
    1. Redis inbound_did:{uniqueid} — захвачен из Newchannel (from-trunk, DID из SIP INVITE)
    2. Redis inbound_did:{linkedid} — если uniqueid не совпал (bridged channel)
    3. user_field — Set(CDR(userfield)=...) из dialplan (fallback, на будущее)
    4. dst — последняя надежда (extension менеджера, обычно не DID)

    Чистит Redis-ключи сразу после чтения чтобы не засорять память.
    Возвращает did_raw или None.
    """
    redis_did: str | None = None
    try:
        redis_did = (
            await redis_client.get(f"inbound_did:{uniqueid}")
            or (await redis_client.get(f"inbound_did:{linkedid}") if linkedid else None)
        )
    except Exception:
        logger.exception("Ошибка чтения inbound_did из Redis: uniqueid=%s", uniqueid)

    # Чистим Redis-ключи сразу после чтения, чтобы не засорять
    if redis_did and uniqueid:
        try:
            await redis_client.delete(f"inbound_did:{uniqueid}")
            if linkedid:
                await redis_client.delete(f"inbound_did:{linkedid}")
        except Exception:
            logger.exception("Ошибка удаления inbound_did из Redis: uniqueid=%s", uniqueid)

    return redis_did or user_field or dst


async def _apply_source_attribution(db, call: Call, session_data: dict | None, did_norm: str | None) -> None:
    """Атрибутирует источник звонка — мутирует поля call.source/medium/campaign/keyword.

    Если есть DNI-сессия — берём utm-параметры из неё (приоритет).
    Иначе фолбэк на source_label из TrackingNumber (статичный номер).
    После атрибуции нормализуем source через _SOURCE_ALIASES
    и применяем правило traffic_mektep_* → facebook.
    """
    if session_data:
        call.source = session_data.get("source")
        call.medium = session_data.get("utm_medium")
        call.campaign = session_data.get("utm_campaign")
        call.keyword = session_data.get("utm_keyword")
    else:
        # Фолбэк: если DNI-сессии нет (статичный номер или snippet не отработал),
        # берём source_label из самого tracking_number. Так звонки на
        # "2gis_almaty", "instagram" и т.п. номера сразу видны в дашборде.
        if did_norm:
            tn_row = await db.execute(
                select(TrackingNumber).where(
                    TrackingNumber.phone_normalized == did_norm
                )
            )
            tn_obj = tn_row.scalar_one_or_none()
            if tn_obj and tn_obj.source_label:
                call.source = tn_obj.source_label

    # Нормализация source через модульный словарь _SOURCE_ALIASES
    if call.source and call.source in _SOURCE_ALIASES:
        call.source = _SOURCE_ALIASES[call.source]
    # Если кампания "traffic_mektep_*" — это FB Ads, переопределяем source
    if call.campaign and call.campaign.startswith("traffic_mektep_"):
        call.source = "facebook"


async def _push_to_amo(db, call: Call, src: str, uniqueid: str | None) -> None:
    """Создаёт или переиспользует AMO-лид для атрибуцированного входящего звонка.

    AMO CRM: создаём лид для любого атрибуцированного входящего звонка.
    FAILED/BUSY тоже — это потенциальные лиды, клиент пытался связаться.

    Дедупликация multi-leg: один входящий звонок через bridge создаёт несколько
    CDR-событий (по одному на каждый extension менеджера). Чтобы не плодить
    дубли в AMO используем трёхуровневую защиту:
    1. SQL-проверка за 30 минут — ДО попытки взять lock (быстрый путь)
    2. Redis lock — чтобы два параллельных процесса не прошли в AMO API одновременно
    3. SQL-проверка после взятия lock — финальная защита от race condition
    """
    if not call.project_id or not call.caller_number:
        return

    lock_key = f"amo_lead_lock:{call.caller_number}"
    lock_acquired = False
    try:
        # --- Уровень 1: SQL-проверка ДО lock (быстрый путь для последующих leg'ов) ---
        # Если другой leg уже создал лид за последние 30 минут — берём его.
        # Это покрывает большинство случаев: leg'и приходят с разницей в 1-3 сек,
        # к моменту второго leg'а первый уже успел сохранить amo_lead_id в БД.
        pre_lock_lead_id = await _find_existing_lead_for_caller(
            db, call.caller_number, call.id, window_minutes=30
        )
        if pre_lock_lead_id:
            call.amo_lead_id = pre_lock_lead_id
            await db.commit()
            logger.info(
                "AMO: дубль leg (pre-lock SQL), привязан к лиду %s (caller=%s)",
                pre_lock_lead_id, call.caller_number,
            )
            try:
                await amocrm_client.add_call_note(pre_lock_lead_id, call)
            except Exception:
                logger.exception(
                    "AMO: add_call_note на reuse не сработал (lead=%s)", pre_lock_lead_id
                )
            # Лид уже есть — дальше не идём
            return

        # --- Уровень 2: Redis lock — только один процесс идёт в AMO API ---
        # SET NX EX атомарен: только один из параллельных leg'ов получит True.
        lock_acquired = await redis_client.set(lock_key, "1", nx=True, ex=60)

        if not lock_acquired:
            # Другой leg держит lock прямо сейчас. Вместо слепого sleep(3) —
            # короткий поллинг SQL 3×1с: как только сосед сохранит amo_lead_id,
            # выходим раньше и не держим слот семафора зря.
            post_wait_lead_id = None
            for _ in range(3):
                await asyncio.sleep(1)
                post_wait_lead_id = await _find_existing_lead_for_caller(
                    db, call.caller_number, call.id, window_minutes=30
                )
                if post_wait_lead_id:
                    break
            if post_wait_lead_id:
                call.amo_lead_id = post_wait_lead_id
                await db.commit()
                logger.info(
                    "AMO: дубль leg (post-wait SQL), привязан к лиду %s (caller=%s)",
                    post_wait_lead_id, call.caller_number,
                )
                try:
                    await amocrm_client.add_call_note(post_wait_lead_id, call)
                except Exception:
                    logger.exception(
                        "AMO: add_call_note на reuse (post-wait) не сработал (lead=%s)",
                        post_wait_lead_id,
                    )
            else:
                logger.warning(
                    "AMO: lock не получен и лид не найден после ожидания (caller=%s, uniqueid=%s) — пропускаем",
                    call.caller_number, uniqueid,
                )
        else:
            # Lock наш. Делаем финальную SQL-проверку перед созданием:
            # защита от случая когда lock истёк (>60с) но лид уже был создан.
            # --- Уровень 3: SQL-проверка ПОСЛЕ взятия lock (финальная) ---
            post_lock_lead_id = await _find_existing_lead_for_caller(
                db, call.caller_number, call.id, window_minutes=30
            )

            if post_lock_lead_id:
                # Лид уже есть (создан до истечения предыдущего lock'а)
                call.amo_lead_id = post_lock_lead_id
                await db.commit()
                logger.info(
                    "AMO: лид уже существует (post-lock SQL), привязан %s (caller=%s)",
                    post_lock_lead_id, call.caller_number,
                )
                try:
                    await amocrm_client.add_call_note(post_lock_lead_id, call)
                except Exception:
                    logger.exception(
                        "AMO: add_call_note на post-lock reuse не сработал (lead=%s)",
                        post_lock_lead_id,
                    )
            else:
                # Лида нет — создаём. Это первый и единственный leg который дойдёт сюда.
                lead_id = await amocrm_client.create_lead_from_call(call, src)
                if lead_id:
                    call.amo_lead_id = lead_id
                    await db.commit()
                    await amocrm_client.add_call_note(lead_id, call)

    except Exception:
        logger.exception(
            "AMO CRM push failed for call uniqueid=%s", uniqueid
        )
    finally:
        if lock_acquired:
            try:
                await redis_client.delete(lock_key)
            except Exception:
                pass


# Транзиентные исключения БД, которые имеет смысл ретраить.
# ВАЖНО: IntegrityError сюда НЕ входит — дубль это норма, ретрай его не исправит.
_RETRIABLE_DB_ERRORS: tuple[type[Exception], ...] = (
    TooManyConnectionsError,
    ConnectionDoesNotExistError,
    PostgresConnectionError,
    OperationalError,
    SATimeoutError,
)


def _is_retriable_db_error(exc: Exception) -> bool:
    """Транзиентный ли это сбой БД (стоит ретраить)?

    IntegrityError (дубль) НЕ ретраим — сразу False, даже если он потомок DBAPIError.
    Иначе проверяем сам exc и распакованный e.orig (asyncpg-исключение, обёрнутое
    SQLAlchemy в DBAPIError/OperationalError).
    """
    if isinstance(exc, IntegrityError):
        return False
    if isinstance(exc, _RETRIABLE_DB_ERRORS):
        return True
    # SQLAlchemy оборачивает драйверные ошибки в DBAPIError, оригинал — в .orig
    if isinstance(exc, DBAPIError) and exc.orig is not None:
        return isinstance(exc.orig, _RETRIABLE_DB_ERRORS)
    return False


async def _retry_handle_cdr(event: dict, max_attempts: int = 3) -> None:
    """Retry-обёртка для _handle_cdr с backoff на транзиентных сбоях БД.

    Ретраим: TooManyConnections, TimeoutError пула, OperationalError,
    ConnectionDoesNotExist, PostgresConnectionError (в т.ч. обёрнутые в DBAPIError.orig).
    НЕ ретраим: IntegrityError (дубль) и любые другие исключения — пробрасываем сразу.

    Семафор ограничивает параллелизм: max 25 CDR одновременно.
    Backoff: 1, 2, 4 сек между попытками.
    """
    async with _cdr_semaphore:
        for attempt in range(max_attempts):
            try:
                await _handle_cdr(event)
                return
            except Exception as exc:
                # Нетранзиентная ошибка (в т.ч. IntegrityError) — не ретраим
                if not _is_retriable_db_error(exc):
                    raise
                if attempt < max_attempts - 1:
                    wait_secs = 2 ** attempt  # 1, 2, 4 сек
                    logger.warning(
                        "Транзиентный сбой БД %s (попытка %d/%d), retry через %ds: uniqueid=%s",
                        type(exc).__name__, attempt + 1, max_attempts, wait_secs,
                        event.get("uniqueid"),
                    )
                    await asyncio.sleep(wait_secs)
                    continue
                logger.error(
                    "CDR потерян после %d попыток (%s): uniqueid=%s",
                    max_attempts, type(exc).__name__, event.get("uniqueid"),
                )
                raise


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
    linkedid = event.get("linkedid")

    # Определяем DID по приоритету: Redis → user_field → dst
    did_raw = await _resolve_did(uniqueid, linkedid, user_field, dst)

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

    # Нормализуем caller для ключа lock-а
    normalized_caller = normalize_phone(src)

    # Защита от race-условия: два leg-а одного звонка пришли в окно 1-2 сек.
    # Если lock не получен — другой leg уже обрабатывает этот caller.
    # Ждём 3 сек: к тому моменту первый leg сохранит amo_lead_id в БД,
    # и SQL pre-check во втором leg-е найдёт его и переиспользует.
    call_lock_key = f"call_lock:{normalized_caller}"
    call_lock_acquired = await redis_client.set(call_lock_key, "1", nx=True, ex=120)

    if not call_lock_acquired:
        logger.info(
            "call_lock: caller=%s уже обрабатывается другим leg-ом — поллим до 3с",
            normalized_caller,
        )
        # Вместо слепого sleep(3) — короткий поллинг: как только сосед-leg сохранит
        # свою запись Call с этим caller_number, продолжаем. Раньше выходим — раньше
        # освобождаем слот семафора. Держим слот максимум 3с (как было), но обычно меньше.
        normalized_caller_search = normalized_caller
        for _ in range(3):
            await asyncio.sleep(1)
            try:
                async with async_session() as _poll_db:
                    exists_row = await _poll_db.execute(
                        select(Call.id)
                        .where(Call.caller_number == src)
                        .where(
                            Call.started_at
                            >= datetime.now(timezone.utc) - timedelta(minutes=30)
                        )
                        .limit(1)
                    )
                    if exists_row.scalar_one_or_none() is not None:
                        break
            except Exception:
                # Поллинг — best-effort; ошибка чтения не должна прерывать обработку
                logger.debug(
                    "call_lock poll: ошибка проверки caller=%s (продолжаем ждать)",
                    normalized_caller_search,
                )
        # После ожидания продолжаем — SQL pre-check в _push_to_amo найдёт лид от первого leg-а

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
                # linkedid сохраняется для reconciliation: по нему worker ищет
                # другой call-leg того же звонка с правильным tracking_did
                linkedid=linkedid or None,
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

            # 5. Атрибуция из сессии (DNI) или фолбэк на source_label + нормализация
            await _apply_source_attribution(db, call, session_data, did_norm)

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

            # Проверяем дубликат по uniqueid перед INSERT
            # AMI может прислать CDR дважды (после переподключения worker-а)
            existing_call_row = await db.execute(
                select(Call).where(Call.uniqueid == call.uniqueid)
            )
            if existing_call_row.scalar_one_or_none() is not None:
                logger.info("CDR duplicate skipped: uniqueid=%s", uniqueid)
                return

            db.add(call)
            try:
                await db.commit()
            except Exception as exc:
                # Защита от race condition: два leg пришли одновременно —
                # один уже вставил запись, второй получил IntegrityError
                from sqlalchemy.exc import IntegrityError
                if isinstance(exc, IntegrityError):
                    await db.rollback()
                    logger.info("CDR duplicate (race) skipped: uniqueid=%s", uniqueid)
                    return
                raise

            # AMO CRM: создаём/переиспользуем лид (с трёхуровневой дедупликацией)
            await _push_to_amo(db, call, src, uniqueid)

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

    # Релизим lock на caller (TTL 120 сек подстрахует при любом исходе)
    if call_lock_acquired:
        try:
            await redis_client.delete(call_lock_key)
        except Exception:
            pass  # TTL 120 сек подстрахует
