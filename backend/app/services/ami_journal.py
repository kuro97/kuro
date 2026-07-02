"""Персистентный журнал AMI-событий (защита от потери звонков).

Каждое сырое событие звонка сначала пишется в таблицу ami_events (status=pending),
затем обрабатывается прежним хендлером process_call_event. Если процесс упал/рестартовал
между приёмом Cdr и commit — событие остаётся pending и будет переобработано при старте
(replay_pending_events). Идемпотентность обеспечена дедупликацией по calls.uniqueid.
"""

import asyncio
import json
import logging
from typing import Awaitable, Callable

from sqlalchemy import text

from app.core.database import async_session

logger = logging.getLogger(__name__)

# Максимум попыток обработки одного события до пометки окончательно failed.
_MAX_ATTEMPTS = 5
# Интервал цикла ретеншна (секунды).
_CLEANUP_INTERVAL_SEC = 3600
# Сколько дней хранить done-события.
_RETENTION_DAYS = 7


async def record_event(event: dict) -> int | None:
    """Пишет сырое событие в журнал (status=pending) и возвращает его id.

    Быстрый одиночный INSERT, отдельная короткоживущая сессия. При ошибке БД
    логирует и возвращает None — обработка события всё равно продолжится
    (журнал — это страховка, а не блокер основного пути).
    """
    try:
        payload_json = json.dumps(event, default=str)
    except Exception:
        logger.exception("ami_journal.record_event: payload не сериализуется в JSON")
        return None

    try:
        async with async_session() as db:
            result = await db.execute(
                text(
                    """
                    INSERT INTO ami_events (event_type, uniqueid, payload, status, received_at)
                    VALUES (:event_type, :uniqueid, CAST(:payload AS JSONB), 'pending', now())
                    RETURNING id
                    """
                ),
                {
                    "event_type": event.get("event"),
                    "uniqueid": event.get("uniqueid"),
                    "payload": payload_json,
                },
            )
            event_id = result.scalar_one()
            await db.commit()
            return event_id
    except Exception:
        logger.exception("ami_journal.record_event: сбой записи в журнал")
        return None


async def mark_done(event_id: int) -> None:
    """Помечает событие обработанным: status='done', processed_at=now()."""
    async with async_session() as db:
        await db.execute(
            text("UPDATE ami_events SET status='done', processed_at=now() WHERE id = :id"),
            {"id": event_id},
        )
        await db.commit()


async def mark_failed(event_id: int, error: str) -> None:
    """Помечает событие проваленным: status='failed', attempts+=1, last_error=error."""
    async with async_session() as db:
        await db.execute(
            text(
                """
                UPDATE ami_events
                SET status='failed', attempts = attempts + 1, last_error = :error
                WHERE id = :id
                """
            ),
            {"id": event_id, "error": error},
        )
        await db.commit()


async def replay_pending_events(
    handler: Callable[[dict], Awaitable[None]],
) -> int:
    """Переобрабатывает все зависшие события при старте воркера.

    Берёт события со status IN ('pending','failed') и attempts < _MAX_ATTEMPTS,
    сортирует по received_at ASC, для каждого вызывает handler(payload).
    Успех → mark_done, исключение → mark_failed. Возвращает число успешно
    переобработанных событий. Дубли звонков/лидов не создаются (дедуп по uniqueid).

    Нюанс: при replay in-memory кеш active_calls (call_processor.py) пуст, поэтому
    started_at для new_call-событий возьмётся из datetime.now() внутри хендлера —
    время звонка может сместиться максимум на длительность даунтайма. CDR всё равно
    сохранится, лид создастся (дубли исключены дедупом по uniqueid/AMO).
    """
    async with async_session() as db:
        rows = await db.execute(
            text(
                """
                SELECT id, payload FROM ami_events
                WHERE status IN ('pending','failed') AND attempts < :max_attempts
                ORDER BY received_at ASC
                """
            ),
            {"max_attempts": _MAX_ATTEMPTS},
        )
        events = rows.all()

    if not events:
        return 0

    replayed = 0
    for idx, (event_id, payload) in enumerate(events, start=1):
        payload_dict = payload if isinstance(payload, dict) else json.loads(payload)
        try:
            await handler(payload_dict)
            await mark_done(event_id)
            replayed += 1
        except Exception as exc:
            logger.exception("ami_journal.replay: ошибка обработки события id=%s", event_id)
            try:
                await mark_failed(event_id, str(exc))
            except Exception:
                logger.exception(
                    "ami_journal.replay: не удалось пометить failed событие id=%s", event_id
                )
        if idx % 100 == 0:
            logger.info("ami_journal.replay: обработано %d/%d событий", idx, len(events))

    return replayed


async def cleanup_old_events(retention_days: int = _RETENTION_DAYS) -> int:
    """Удаляет done-события старше retention_days. Возвращает число удалённых строк."""
    async with async_session() as db:
        result = await db.execute(
            text(
                """
                DELETE FROM ami_events
                WHERE status='done' AND processed_at < now() - make_interval(days => :days)
                """
            ),
            {"days": retention_days},
        )
        await db.commit()
        return result.rowcount


async def run_journal_cleanup_loop() -> None:
    """Бесконечный фоновый цикл ретеншна журнала (раз в _CLEANUP_INTERVAL_SEC)."""
    while True:
        try:
            await asyncio.sleep(_CLEANUP_INTERVAL_SEC)
            deleted = await cleanup_old_events(_RETENTION_DAYS)
            if deleted:
                logger.info("ami_journal.cleanup: удалено %d done-событий старше %d дней", deleted, _RETENTION_DAYS)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("ami_journal.run_journal_cleanup_loop: ошибка цикла ретеншна")
