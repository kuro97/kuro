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
from app.core.amo_constants import STATUS_WON, STATUS_LOST, SORT_QUALIFIED, SORT_PAID
from app.core.database import async_session
from app.models.call import Call

logger = logging.getLogger(__name__)

# status_id = 142 — стандартный AMO "Успешно реализовано" (won)
_AMO_STATUS_WON = STATUS_WON

# status_id = 143 — стандартный AMO "Закрыто и не реализовано" (не считается ни квалом, ни оплатой)
_AMO_STATUS_LOST = STATUS_LOST

# sort-порог начала квалификации (КВАЛИФИКАЦИЯ ПРОЙДЕНА и выше)
_SORT_QUALIFIED = SORT_QUALIFIED

# sort-порог начала оплат (ПРЕДОПЛАТА получена №1 и выше)
_SORT_PAID = SORT_PAID

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

        Структура разделена на три фазы чтобы не держать DB-соединение
        во время HTTP-запросов к AMO (timeout=10с × N запросов):
          Фаза 1 — SELECT Call из БД (быстро, закрываем сессию).
          Фаза 2 — HTTP к AMO: GET lead + GET statuses (медленно, без DB).
          Фаза 3 — UPDATE Call в БД (быстро, закрываем сессию).
        """
        if not self._is_configured():
            logger.warning(
                "AMO CRM не настроен (amo_subdomain/amo_token пустые) — "
                "sync_lead пропускаем (lead_id=%d)", lead_id
            )
            return False

        # --- Фаза 1: читаем Call из БД, сразу освобождаем соединение ---
        async with async_session() as db:
            row = await db.execute(
                select(Call).where(Call.amo_lead_id == lead_id).limit(1)
            )
            call = row.scalar_one_or_none()

        if call is None:
            logger.debug("sync_lead: Call с amo_lead_id=%d не найден в БД", lead_id)
            return False

        # --- Фаза 2: HTTP к AMO (DB-сессия закрыта) ---
        # Все HTTP-запросы выполняются без открытого DB-коннекта.
        # Это предотвращает исчерпание пула при polling 30-дневного окна.
        pipeline_id: int | None = None
        status_id: int | None = None
        deal_amount: int | None = None
        amo_city: str | None = None
        qualified_field: str | None = None
        sort: int | None = None
        amo_qualified: bool = False
        amo_won: bool = False

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

                # pipeline_id, status_id — прямо в теле ответа
                pipeline_id = lead_data.get("pipeline_id")
                status_id = lead_data.get("status_id")

                # amount (price) → amo_deal_amount
                deal_amount = lead_data.get("price")

                # amo_city — кастомное поле "Город".
                # Если AMO вернул "Другой" (дефолт для site/insta и т.п.) — пишем None,
                # чтобы на дашборде было "—" вместо бессмысленного "Другой".
                amo_city = self._extract_custom_field(lead_data, _FIELD_CITY)
                if amo_city and amo_city.strip() == "Другой":
                    amo_city = None

                # "Квалификация пройдена" — кастомное поле, по нему квал
                qualified_field = self._extract_custom_field(
                    lead_data, "Квалификация пройдена"
                )

                # Получаем sort статуса из кеша/API.
                # _get_status_sort делает HTTP — вызываем здесь, в HTTP-фазе.
                if pipeline_id is not None and status_id is not None:
                    sort = await self._get_status_sort(client, pipeline_id, status_id)

                # Вычисляем квал (по custom field) и оплату (по sort/status)
                amo_qualified, amo_won = self._calc_qualified_won(
                    status_id, sort, qualified_field
                )

        except Exception:
            logger.exception("sync_lead: HTTP ошибка при запросе lead_id=%d", lead_id)
            return False

        # --- Фаза 3: обновляем Call в БД (новая сессия, HTTP уже закрыт) ---
        async with async_session() as db:
            # Перезагружаем Call в новой сессии (предыдущая сессия уже закрыта)
            row = await db.execute(
                select(Call).where(Call.amo_lead_id == lead_id).limit(1)
            )
            call = row.scalar_one_or_none()
            if call is None:
                # Маловероятно, но защита от удаления между фазами
                logger.warning(
                    "sync_lead: Call с amo_lead_id=%d исчез из БД между фазами", lead_id
                )
                return False

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
