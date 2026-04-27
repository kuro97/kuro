"""AMO poll worker: страховочный фоновый процесс синхронизации лидов.

Каждые 10 минут синхронизирует все Call с amo_lead_id за последние 24 часа.
Нужен на случай если webhook от AMO не доставился (сеть, перезагрузка сервера и т.п.).
"""

import asyncio
import logging

from app.services.amo_sync import amo_sync

logger = logging.getLogger(__name__)

# Интервал между итерациями (секунды)
_INTERVAL_SEC = 600

# Сколько часов назад смотреть при каждом polling-прогоне
_LOOKBACK_HOURS = 24


async def run_amo_poll_loop() -> None:
    """Бесконечный цикл polling-синхронизации AMO лидов.

    Запускается как background task в lifespan main.py рядом с reconciliation_task.
    """
    logger.info("AMO poll worker started (interval=%ds)", _INTERVAL_SEC)
    while True:
        try:
            updated = await amo_sync.sync_recent_leads(hours_back=_LOOKBACK_HOURS)
            if updated:
                logger.info("AMO poll: synced %d leads", updated)
        except Exception:
            logger.exception("AMO poll iteration failed")
        await asyncio.sleep(_INTERVAL_SEC)
