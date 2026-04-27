"""Сервис синхронизации данных лида из AMO CRM в таблицу calls.

Используется двумя способами:
  1. Webhook (real-time): AMO шлёт POST при изменении лида → sync_lead(lead_id)
  2. Polling (fallback): каждые 10 минут → sync_recent_leads(hours_back=24)
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.core.database import async_session
from app.models.call import Call

logger = logging.getLogger(__name__)

# status_id = 142 — стандартный AMO "Успешно реализовано" (won)
_AMO_STATUS_WON = 142

# Имя кастомного поля квалификации
_FIELD_QUALIFICATION = "Квалификация пройдена"

# Значение enum'а квалификации, которое означает True
_FIELD_QUALIFICATION_YES = "Да"

# Имя кастомного поля города
_FIELD_CITY = "Город"


class AmoSyncService:
    """Сервис для обновления AMO-полей в Call из API AMO CRM."""

    def _base_url(self) -> str:
        return f"https://{settings.amo_subdomain}.amocrm.ru"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {settings.amo_token}",
            "Content-Type": "application/json",
        }

    def _is_configured(self) -> bool:
        return bool(settings.amo_subdomain and settings.amo_token)

    def _extract_custom_field(self, lead_data: dict, field_name: str) -> str | None:
        """Возвращает первое значение кастомного поля по его имени (name)."""
        fields = lead_data.get("custom_fields_values") or []
        for field in fields:
            if field.get("field_name") == field_name:
                values = field.get("values") or []
                if values:
                    return str(values[0].get("value", ""))
        return None

    async def sync_lead(self, lead_id: int) -> bool:
        """Читает актуальный лид из AMO API и обновляет соответствующий Call.

        Возвращает True если обновление прошло успешно, False — при ошибке
        или если интеграция не настроена.
        """
        if not self._is_configured():
            logger.warning(
                "AMO CRM не настроен (amo_subdomain/amo_token пустые) — "
                "sync_lead пропускаем (lead_id=%d)", lead_id
            )
            return False

        # Шаг 1: находим Call с amo_lead_id == lead_id
        async with async_session() as db:
            row = await db.execute(
                select(Call).where(Call.amo_lead_id == lead_id).limit(1)
            )
            call = row.scalar_one_or_none()
            if call is None:
                logger.debug("sync_lead: Call с amo_lead_id=%d не найден в БД", lead_id)
                return False

            # Шаг 2: запрашиваем лид из AMO
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        f"{self._base_url()}/api/v4/leads/{lead_id}",
                        headers=self._headers(),
                        params={"with": "contacts"},
                    )
            except Exception:
                logger.exception("sync_lead: HTTP ошибка при запросе lead_id=%d", lead_id)
                return False

            # 404 — лид удалён в AMO, не падаем
            if resp.status_code == 404:
                logger.warning(
                    "sync_lead: lead_id=%d не найден в AMO (404) — пропускаем", lead_id
                )
                return False

            if resp.status_code != 200:
                logger.warning(
                    "sync_lead: AMO вернул %d для lead_id=%d — пропускаем",
                    resp.status_code, lead_id,
                )
                return False

            try:
                lead_data = resp.json()
            except Exception:
                logger.exception("sync_lead: не удалось распарсить JSON ответа lead_id=%d", lead_id)
                return False

            # Шаг 3: извлекаем поля

            # pipeline_id, status_id — прямо в теле ответа
            pipeline_id: int | None = lead_data.get("pipeline_id")
            status_id: int | None = lead_data.get("status_id")

            # amount (price) → amo_deal_amount
            deal_amount: int | None = lead_data.get("price")

            # amo_won — True если status_id == 142 (won)
            amo_won = status_id == _AMO_STATUS_WON

            # amo_qualified — поле "Квалификация пройдена", значение "Да" → True
            qual_value = self._extract_custom_field(lead_data, _FIELD_QUALIFICATION)
            amo_qualified = (qual_value == _FIELD_QUALIFICATION_YES) if qual_value is not None else False

            # amo_city — поле "Город"
            amo_city = self._extract_custom_field(lead_data, _FIELD_CITY)

            # Шаг 4: обновляем Call
            call.amo_pipeline_id = pipeline_id
            call.amo_status_id = status_id
            call.amo_deal_amount = deal_amount
            call.amo_won = amo_won
            call.amo_qualified = amo_qualified
            call.amo_city = amo_city
            call.amo_updated_at = datetime.now(timezone.utc)

            await db.commit()

        logger.info(
            "sync_lead: обновлён Call с amo_lead_id=%d "
            "(pipeline=%s, status=%s, won=%s, qualified=%s, city=%s, amount=%s)",
            lead_id, pipeline_id, status_id, amo_won, amo_qualified, amo_city, deal_amount,
        )
        return True

    async def sync_recent_leads(self, hours_back: int = 24) -> int:
        """Берёт все Call.amo_lead_id за последние N часов и зовёт sync_lead для каждого.

        Возвращает количество успешно обновлённых записей.
        """
        if not self._is_configured():
            logger.warning(
                "AMO CRM не настроен — sync_recent_leads пропускаем"
            )
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

        async with async_session() as db:
            rows = await db.execute(
                select(Call.amo_lead_id).where(
                    Call.amo_lead_id.is_not(None),
                    Call.started_at >= cutoff,
                )
            )
            lead_ids: list[int] = [r[0] for r in rows.all()]

        if not lead_ids:
            return 0

        updated = 0
        for lead_id in lead_ids:
            try:
                ok = await self.sync_lead(lead_id)
                if ok:
                    updated += 1
            except Exception:
                logger.exception("sync_recent_leads: ошибка при sync_lead(%d)", lead_id)

        return updated


# Глобальный инстанс — импортируется в webhook и poll worker
amo_sync = AmoSyncService()
