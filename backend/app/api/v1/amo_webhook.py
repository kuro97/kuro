"""Webhook endpoint для AMO CRM.

AMO шлёт POST при изменении/добавлении лида (application/x-www-form-urlencoded).
Мы парсим все lead_id из тела и запускаем синхронизацию каждого.

Защита: если задан settings.amo_webhook_secret — проверяем заголовок X-Amo-Secret.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from app.core.config import settings
from app.services.amo_sync import amo_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/amo", tags=["amo"])


@router.post("/webhook")
async def amo_webhook(request: Request):
    """Принимает webhook от AMO CRM при изменении лида.

    AMO шлёт application/x-www-form-urlencoded с полями вроде:
        leads[update][0][id]=12345
        leads[status][0][id]=12345
        leads[add][0][id]=12345

    Парсим все lead_id и для каждого вызываем amo_sync.sync_lead(id).
    """
    # Проверяем секрет если задан в конфиге
    if settings.amo_webhook_secret:
        incoming_secret = request.headers.get("X-Amo-Secret")
        if incoming_secret != settings.amo_webhook_secret:
            logger.warning(
                "amo_webhook: невалидный X-Amo-Secret от %s",
                request.client.host if request.client else "unknown",
            )
            raise HTTPException(status_code=403, detail="Invalid webhook secret")

    # Парсим form-data от AMO
    form = await request.form()
    lead_ids: set[int] = set()

    for key, value in form.multi_items():
        # Ловим leads[update][N][id], leads[status][N][id], leads[add][N][id]
        if key.startswith("leads[") and "[id]" in key:
            try:
                lead_ids.add(int(value))
            except (ValueError, TypeError):
                continue

    logger.info("amo_webhook: получен запрос, lead_ids=%s", lead_ids)

    # Параллельный sync через asyncio.gather — return_exceptions чтобы одна ошибка
    # не прерывала синхронизацию остальных лидов
    if lead_ids:
        await asyncio.gather(
            *[amo_sync.sync_lead(lid) for lid in lead_ids],
            return_exceptions=True,
        )

    return {"ok": True, "synced": len(lead_ids)}
