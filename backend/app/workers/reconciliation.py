"""Reconciliation worker: восстанавливает атрибуцию потерянных звонков.

Запускается каждые 5 минут. Ищет звонки за последний час у которых
project_id=None и tracking_did не совпадает ни с одним нашим DID.
Для каждого такого звонка пытается найти «правильный» call-leg того же
звонка и скопировать из него tracking_did / project_id / source и т.д.

Стратегии корреляции (в порядке приоритета):
  1. linkedid — Asterisk связывает все leg одного звонка единым linkedid.
  2. caller_number + время ±60 сек — fallback для старых записей без linkedid.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.database import async_session
from app.models.call import Call
from app.models.tracking_number import TrackingNumber
from app.services.amocrm import amocrm_client

logger = logging.getLogger(__name__)

# Интервал между итерациями reconciliation (секунды)
_INTERVAL_SEC = 300

# Окно поиска звонков для reconciliation
_LOOKBACK_HOURS = 1

# Окно корреляции по времени (стратегия 2)
_TIME_WINDOW_SEC = 60


async def _attribute_from(call: Call, source_call: Call, db) -> None:
    """Обновляет call атрибуцией из source_call и пушит в AMO.

    Не создаёт дубликат в AMO — проверяет call.amo_lead_id перед пушем.
    """
    call.tracking_did = source_call.tracking_did
    call.project_id = source_call.project_id
    call.source = source_call.source
    call.medium = source_call.medium
    call.campaign = source_call.campaign
    await db.commit()

    logger.info(
        "Reconciled call uniqueid=%s: tracking_did -> %s (from uniqueid=%s)",
        call.uniqueid,
        call.tracking_did,
        source_call.uniqueid,
    )

    # Пушим в AMO только если лид ещё не создан для этого звонка
    if call.amo_lead_id:
        return

    try:
        lead_id = await amocrm_client.create_lead_from_call(call, call.caller_number)
        if lead_id:
            call.amo_lead_id = lead_id
            await db.commit()
            await amocrm_client.add_call_note(lead_id, call)
    except Exception:
        logger.exception(
            "AMO push failed during reconciliation uniqueid=%s", call.uniqueid
        )


async def reconcile_once() -> None:
    """Одна итерация: ищем unattributed calls, пытаемся восстановить атрибуцию."""
    async with async_session() as db:
        # Загружаем список нормализованных DID наших активных tracking-номеров
        our_dids_rows = await db.execute(
            select(TrackingNumber.phone_normalized).where(
                TrackingNumber.is_active.is_(True)
            )
        )
        our_dids: set[str] = {r[0] for r in our_dids_rows.all() if r[0]}

        if not our_dids:
            # Нет активных номеров — нечего reconcile
            return

        # Ищем звонки за последний час у которых нет project_id
        # и tracking_did не совпадает с нашими DID
        cutoff = datetime.now(timezone.utc) - timedelta(hours=_LOOKBACK_HOURS)
        unattributed_rows = await db.execute(
            select(Call).where(
                Call.project_id.is_(None),
                Call.started_at >= cutoff,
                Call.tracking_did.not_in(our_dids),
            )
        )
        unattributed: list[Call] = list(unattributed_rows.scalars().all())

        if not unattributed:
            return

        logger.info("Reconciliation: found %d unattributed calls", len(unattributed))

        for call in unattributed:
            # Стратегия 1: корреляция по linkedid
            # Asterisk задаёт одинаковый linkedid всем каналам одного звонка
            if call.linkedid:
                matched_row = await db.execute(
                    select(Call)
                    .where(
                        Call.linkedid == call.linkedid,
                        Call.tracking_did.in_(our_dids),
                        Call.project_id.is_not(None),
                    )
                    .limit(1)
                )
                matched = matched_row.scalar_one_or_none()
                if matched:
                    await _attribute_from(call, matched, db)
                    continue

            # Стратегия 2: корреляция по caller_number + близкое время ±60 сек
            # Fallback для записей без linkedid или когда стратегия 1 не дала результат
            if call.caller_number and call.started_at:
                window = timedelta(seconds=_TIME_WINDOW_SEC)
                matched_row = await db.execute(
                    select(Call)
                    .where(
                        Call.caller_number == call.caller_number,
                        Call.started_at.between(
                            call.started_at - window,
                            call.started_at + window,
                        ),
                        Call.tracking_did.in_(our_dids),
                        Call.project_id.is_not(None),
                        Call.id != call.id,
                    )
                    .limit(1)
                )
                matched = matched_row.scalar_one_or_none()
                if matched:
                    await _attribute_from(call, matched, db)


async def run_reconciliation_loop() -> None:
    """Бесконечный цикл reconciliation. Запускается как background task при старте."""
    logger.info("Reconciliation worker started (interval=%ds)", _INTERVAL_SEC)
    while True:
        try:
            await reconcile_once()
        except Exception:
            logger.exception("Reconciliation iteration failed")
        await asyncio.sleep(_INTERVAL_SEC)
