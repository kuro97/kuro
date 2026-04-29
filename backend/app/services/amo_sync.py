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

# status_id = 143 — стандартный AMO "Закрыто и не реализовано" (не считается ни квалом, ни оплатой)
_AMO_STATUS_LOST = 143

# sort-порог начала квалификации (КВАЛИФИКАЦИЯ ПРОЙДЕНА и выше)
_SORT_QUALIFIED = 80

# sort-порог начала оплат (ПРЕДОПЛАТА получена №1 и выше)
_SORT_PAID = 150

# Имя кастомного поля города
_FIELD_CITY = "Город"


class AmoSyncService:
    """Сервис для обновления AMO-полей в Call из API AMO CRM."""

    def __init__(self):
        # Кеш маппинга (pipeline_id, status_id) → sort.
        # Заполняется лениво при первом обращении к pipeline.
        # Ключ — pipeline_id (int), значение — dict[status_id → sort]
        self._status_sort_cache: dict[int, dict[int, int]] = {}

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

    async def _get_status_sort(
        self,
        client: httpx.AsyncClient,
        pipeline_id: int,
        status_id: int,
    ) -> int | None:
        """Возвращает sort для статуса в воронке.

        Использует in-memory кеш на инстанс сервиса.
        Если pipeline_id или status_id неизвестен — возвращает None, не падает.
        """
        # Если pipeline уже в кеше — ищем статус
        if pipeline_id in self._status_sort_cache:
            return self._status_sort_cache[pipeline_id].get(status_id)

        # Загружаем статусы воронки из API
        try:
            resp = await client.get(
                f"{self._base_url()}/api/v4/leads/pipelines/{pipeline_id}/statuses",
                headers=self._headers(),
            )
        except Exception:
            logger.exception(
                "_get_status_sort: HTTP ошибка при запросе статусов pipeline_id=%d",
                pipeline_id,
            )
            return None

        if resp.status_code != 200:
            logger.warning(
                "_get_status_sort: AMO вернул %d для pipeline_id=%d — пропускаем кеширование",
                resp.status_code, pipeline_id,
            )
            return None

        try:
            data = resp.json()
        except Exception:
            logger.exception(
                "_get_status_sort: не удалось распарсить JSON статусов pipeline_id=%d",
                pipeline_id,
            )
            return None

        # AMO возвращает список статусов в _embedded.statuses
        statuses = (data.get("_embedded") or {}).get("statuses") or []
        sort_map: dict[int, int] = {}
        for s in statuses:
            sid = s.get("id")
            ssort = s.get("sort")
            if sid is not None and ssort is not None:
                sort_map[int(sid)] = int(ssort)

        # Также добавляем системные статусы won/lost с фиксированными sort
        # (AMO не всегда возвращает их в этом endpoint)
        sort_map.setdefault(_AMO_STATUS_WON, 10000)
        sort_map.setdefault(_AMO_STATUS_LOST, 11000)

        self._status_sort_cache[pipeline_id] = sort_map
        logger.debug(
            "_get_status_sort: закешировано %d статусов для pipeline_id=%d",
            len(sort_map), pipeline_id,
        )

        return sort_map.get(status_id)

    def _calc_qualified_won(
        self,
        status_id: int | None,
        sort: int | None,
        qualified_field_value: str | None,
    ) -> tuple[bool, bool]:
        """Вычисляет (amo_qualified, amo_won).

        Правила:
          - amo_qualified = кастомное поле "Квалификация пройдена" == "Да"
            (так считают и менеджеры, и AMO статистика воронки)
          - amo_won = (sort >= 150 и НЕ status_id 143) ИЛИ status_id == 142 (won)
        """
        is_lost = status_id == _AMO_STATUS_LOST
        is_system_won = status_id == _AMO_STATUS_WON

        amo_qualified = bool(
            qualified_field_value
            and qualified_field_value.strip().lower() in ("да", "yes", "true", "1")
        )

        amo_won = (
            (sort is not None and sort >= _SORT_PAID and not is_lost)
            or is_system_won
        )

        return amo_qualified, amo_won

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

            # Шаг 2: запрашиваем лид из AMO (один клиент на все запросы в рамках sync_lead)
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        f"{self._base_url()}/api/v4/leads/{lead_id}",
                        headers=self._headers(),
                        params={"with": "contacts"},
                    )

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
                        logger.exception(
                            "sync_lead: не удалось распарсить JSON ответа lead_id=%d", lead_id
                        )
                        return False

                    # Шаг 3: извлекаем поля

                    # pipeline_id, status_id — прямо в теле ответа
                    pipeline_id: int | None = lead_data.get("pipeline_id")
                    status_id: int | None = lead_data.get("status_id")

                    # amount (price) → amo_deal_amount
                    deal_amount: int | None = lead_data.get("price")

                    # amo_city — кастомное поле "Город"
                    amo_city = self._extract_custom_field(lead_data, _FIELD_CITY)

                    # "Квалификация пройдена" — кастомное поле, по нему квал
                    qualified_field = self._extract_custom_field(
                        lead_data, "Квалификация пройдена"
                    )

                    # Шаг 4: получаем sort статуса из кеша/API
                    sort: int | None = None
                    if pipeline_id is not None and status_id is not None:
                        sort = await self._get_status_sort(client, pipeline_id, status_id)

                    # Шаг 5: вычисляем квал (по custom field) и оплату (по sort/status)
                    amo_qualified, amo_won = self._calc_qualified_won(
                        status_id, sort, qualified_field
                    )

            except Exception:
                logger.exception("sync_lead: HTTP ошибка при запросе lead_id=%d", lead_id)
                return False

            # Шаг 6: обновляем Call
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
            "(pipeline=%s, status=%s, sort=%s, won=%s, qualified=%s, city=%s, amount=%s)",
            lead_id, pipeline_id, status_id, sort, amo_won, amo_qualified, amo_city, deal_amount,
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
