"""Webhook endpoint для AMO CRM.

AMO шлёт POST при изменении/добавлении лида (application/x-www-form-urlencoded).
Мы парсим все lead_id из тела и запускаем синхронизацию каждого.

Для новых лидов (leads[add]) дополнительно проверяем реанимацию:
  если у контакта есть закрытый лид с "Гасится" — реанимируем его, новый закрываем как дубликат.

Защита: если задан settings.amo_webhook_secret — проверяем заголовок X-Amo-Secret.
"""

import asyncio
import hmac
import logging
import re

from fastapi import APIRouter, HTTPException, Request

from app.core.config import settings
from app.services.amo_sync import amo_sync
from app.services.lead_reopen import lead_reopen

# Только верхнеуровневый id из leads[update|status|add|delete][N][id].
# Не путать с вложенными [field_id], [pipeline_id], [user_id] и т.п.
_LEAD_ID_KEY_RE = re.compile(r"^leads\[(?:update|status|add|delete)\]\[\d+\]\[id\]$")

# Паттерн только для leads[add] — для реанимации нужны только новые лиды
_LEAD_ADD_KEY_RE = re.compile(r"^leads\[add\]\[\d+\]\[id\]$")

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
    Для новых лидов (leads[add]) — дополнительно проверяем реанимацию.
    """
    # Проверяем секрет если задан в конфиге
    if settings.amo_webhook_secret:
        incoming_secret = request.headers.get("X-Amo-Secret")
        if not hmac.compare_digest(incoming_secret or "", settings.amo_webhook_secret):
            logger.warning(
                "amo_webhook: невалидный X-Amo-Secret от %s",
                request.client.host if request.client else "unknown",
            )
            raise HTTPException(status_code=403, detail="Invalid webhook secret")

    # Парсим form-data от AMO
    form = await request.form()
    lead_ids: set[int] = set()
    # Отдельно собираем только leads[add] — для логики реанимации
    new_lead_ids: set[int] = set()

    for key, value in form.multi_items():
        # Точный матч ТОЛЬКО на верхнеуровневый id сделки.
        # Игнорируем [field_id], [pipeline_id], [user_id] и прочие вложенные id.
        if _LEAD_ID_KEY_RE.match(key):
            try:
                lid = int(value)
                lead_ids.add(lid)
            except (ValueError, TypeError):
                continue

        # Параллельно собираем новые лиды для реанимации
        if _LEAD_ADD_KEY_RE.match(key):
            try:
                new_lead_ids.add(int(value))
            except (ValueError, TypeError):
                continue

    logger.info(
        "amo_webhook: получен запрос, lead_ids=%s, new_lead_ids=%s",
        lead_ids, new_lead_ids,
    )

    # Параллельный sync через asyncio.gather — return_exceptions чтобы одна ошибка
    # не прерывала синхронизацию остальных лидов
    if lead_ids:
        await asyncio.gather(
            *[amo_sync.sync_lead(lid) for lid in lead_ids],
            return_exceptions=True,
        )

    # Проверяем реанимацию для новых лидов — после основного sync
    # Обрабатываем только leads[add], НЕ leads[status/update], чтобы не создавать цикл
    if new_lead_ids:
        await asyncio.gather(
            *[lead_reopen.check_and_reopen(lid) for lid in new_lead_ids],
            return_exceptions=True,
        )

    return {"ok": True, "synced": len(lead_ids)}
