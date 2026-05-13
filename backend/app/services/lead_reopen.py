"""Сервис реанимации закрытых лидов при повторном обращении клиента.

Логика:
  1. AMO присылает webhook leads[add] с новым лидом
  2. Мы проверяем контакты нового лида
  3. Если у контакта есть закрытый лид с "Причина отказа" = "Гасится" —
     реанимируем старый лид, новый закрываем как дубликат

Защита от цикла:
  - Redis lock lead_reopen:{contact_id} TTL=60с
  - Проверяем что "Причина отказа" нового лида != "Дубликат" (наш флаг)
  - Обрабатываем только leads[add], не leads[status/update]
"""

import logging

import httpx

from app.core.config import settings
from app.core.redis import redis_client

logger = logging.getLogger(__name__)

# Константы AMO-аккаунта qadam
_PIPELINE_ID = 3321094           # "Новые продажи"
_STATUS_REOPENED = 48026560      # "Клиент реанимирован"
_STATUS_CLOSED = 143             # "Закрыто и не реализовано"
_FIELD_REFUSAL_REASON = 878831   # "Причина отказа"
_FIELD_CALL_ATTEMPT = 912743     # "call attempt"

# Значение поля "Причина отказа" при автозакрытии salesbot-ом по недозвону
_REASON_GASITSYA = "Гасится"

# Значение поля "Причина отказа" когда мы сами закрываем дубликат
_REASON_DUPLICATE = "Дубликат — реанимирован старый лид"

# TTL Redis-лока в секундах — защита от параллельной обработки и webhook-циклов
_LOCK_TTL_SECONDS = 60


class LeadReopenService:
    """Реанимация закрытых лидов при повторном обращении клиента."""

    def _base_url(self) -> str:
        return f"https://{settings.amo_subdomain}.amocrm.ru"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {settings.amo_token}",
            "Content-Type": "application/json",
        }

    def _is_configured(self) -> bool:
        return bool(settings.amo_subdomain and settings.amo_token)

    async def check_and_reopen(self, new_lead_id: int) -> bool:
        """Проверяет новый лид. Если это дубликат закрытого — реанимирует старый.

        Возвращает True если произошла реанимация (новый лид закрыт как дубликат).
        """
        if not self._is_configured():
            logger.warning(
                "lead_reopen: AMO CRM не настроен — пропускаем check_and_reopen(lead_id=%d)",
                new_lead_id,
            )
            return False

        # Получаем контакты нового лида
        contact_ids = await self._get_lead_contacts(new_lead_id)
        if not contact_ids:
            logger.debug("lead_reopen: нет контактов у lead_id=%d — пропускаем", new_lead_id)
            return False

        for contact_id in contact_ids:
            # Ставим Redis-лок на контакт, чтобы не обработать его параллельно
            lock_key = f"lead_reopen:{contact_id}"
            # SET NX EX — атомарно: set only if not exists, with TTL
            acquired = await redis_client.set(lock_key, new_lead_id, ex=_LOCK_TTL_SECONDS, nx=True)
            if not acquired:
                logger.warning(
                    "lead_reopen: лок занят contact_id=%d (lead_id=%d) — пропускаем",
                    contact_id, new_lead_id,
                )
                continue

            try:
                closed_lead = await self._find_closed_lead_by_contact(contact_id, exclude_lead_id=new_lead_id)
                if closed_lead is None:
                    continue

                closed_lead_id = closed_lead["id"]
                logger.info(
                    "lead_reopen: найден закрытый лид id=%d (contact_id=%d), реанимируем. "
                    "Новый лид id=%d будет закрыт как дубликат.",
                    closed_lead_id, contact_id, new_lead_id,
                )

                # Реанимируем старый лид
                reopened = await self._reopen_lead(closed_lead_id)
                if not reopened:
                    logger.warning(
                        "lead_reopen: не удалось реанимировать лид id=%d", closed_lead_id
                    )
                    continue

                # Закрываем новый лид как дубликат
                await self._close_as_duplicate(new_lead_id, original_lead_id=closed_lead_id)

                # Добавляем заметку к реанимированному лиду
                await self._add_reopen_note(closed_lead_id, duplicate_lead_id=new_lead_id)

                return True

            finally:
                # Снимаем лок после обработки (TTL сам снимет, но лучше явно)
                await redis_client.delete(lock_key)

        return False

    async def _get_lead_contacts(self, lead_id: int) -> list[int]:
        """Возвращает contact_id привязанные к лиду.

        GET /api/v4/leads/{lead_id}?with=contacts
        → _embedded.contacts[].id
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self._base_url()}/api/v4/leads/{lead_id}",
                    headers=self._headers(),
                    params={"with": "contacts"},
                )

                if resp.status_code == 404:
                    logger.warning("lead_reopen: lead_id=%d не найден в AMO (404)", lead_id)
                    return []

                if resp.status_code != 200:
                    logger.warning(
                        "lead_reopen: AMO вернул %d при получении контактов lead_id=%d",
                        resp.status_code, lead_id,
                    )
                    return []

                data = resp.json()

                # Проверяем что лид не является нашим дубликатом (флаг защиты от цикла)
                refusal_reason = self._extract_field_value(data, _FIELD_REFUSAL_REASON)
                if refusal_reason == _REASON_DUPLICATE:
                    logger.debug(
                        "lead_reopen: lead_id=%d уже закрыт нами как дубликат — пропускаем",
                        lead_id,
                    )
                    return []

                contacts = (
                    (data.get("_embedded") or {})
                    .get("contacts") or []
                )
                contact_ids = [int(c["id"]) for c in contacts if c.get("id")]
                return contact_ids

        except Exception:
            logger.exception("lead_reopen: ошибка при получении контактов lead_id=%d", lead_id)
            return []

    async def _find_closed_lead_by_contact(self, contact_id: int, exclude_lead_id: int) -> dict | None:
        """Ищет закрытый лид контакта с причиной 'Гасится'.

        GET /api/v4/contacts/{contact_id}?with=leads
        → фильтруем по status_id=143 и field_id=878831 value="Гасится"
        Если несколько — берём самый свежий (наибольший id).
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Получаем контакт с лидами
                resp = await client.get(
                    f"{self._base_url()}/api/v4/contacts/{contact_id}",
                    headers=self._headers(),
                    params={"with": "leads"},
                )

                if resp.status_code == 404:
                    logger.warning(
                        "lead_reopen: contact_id=%d не найден в AMO (404)", contact_id
                    )
                    return None

                if resp.status_code != 200:
                    logger.warning(
                        "lead_reopen: AMO вернул %d при получении контакта contact_id=%d",
                        resp.status_code, contact_id,
                    )
                    return None

                contact_data = resp.json()
                embedded_leads = (
                    (contact_data.get("_embedded") or {})
                    .get("leads") or []
                )

                # Собираем id кандидатов: закрытые лиды контакта (кроме нового)
                candidate_ids = [
                    int(lead["id"])
                    for lead in embedded_leads
                    if lead.get("id") and int(lead["id"]) != exclude_lead_id
                ]

                if not candidate_ids:
                    return None

                # Для каждого кандидата проверяем: status_id=143, pipeline=3321094, причина="Гасится"
                # Берём наиболее свежий — с наибольшим id
                # Ограничиваем число запросов чтобы не превысить таймаут webhook AMO
                candidate_ids_sorted = sorted(candidate_ids, reverse=True)[:5]

                for lead_id in candidate_ids_sorted:
                    lead_resp = await client.get(
                        f"{self._base_url()}/api/v4/leads/{lead_id}",
                        headers=self._headers(),
                    )

                    if lead_resp.status_code != 200:
                        continue

                    lead_data = lead_resp.json()

                    # Проверяем воронку
                    if lead_data.get("pipeline_id") != _PIPELINE_ID:
                        continue

                    # Проверяем статус "Закрыто и не реализовано"
                    if lead_data.get("status_id") != _STATUS_CLOSED:
                        continue

                    # Проверяем причину отказа
                    reason = self._extract_field_value(lead_data, _FIELD_REFUSAL_REASON)
                    if reason != _REASON_GASITSYA:
                        continue

                    logger.debug(
                        "lead_reopen: найден подходящий закрытый лид id=%d (contact_id=%d)",
                        lead_id, contact_id,
                    )
                    return lead_data

        except Exception:
            logger.exception(
                "lead_reopen: ошибка при поиске закрытого лида contact_id=%d", contact_id
            )

        return None

    async def _reopen_lead(self, lead_id: int) -> bool:
        """Переводит лид в статус 'Клиент реанимирован', очищает причину отказа и call attempt.

        PATCH /api/v4/leads/{lead_id}
        """
        payload = {
            "pipeline_id": _PIPELINE_ID,
            "status_id": _STATUS_REOPENED,
            "custom_fields_values": [
                # Очищаем "Причина отказа"
                {"field_id": _FIELD_REFUSAL_REASON, "values": [{"value": ""}]},
                # Сбрасываем "call attempt" в 0
                {"field_id": _FIELD_CALL_ATTEMPT, "values": [{"value": "0"}]},
            ],
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.patch(
                    f"{self._base_url()}/api/v4/leads/{lead_id}",
                    json=payload,
                    headers=self._headers(),
                )

                if resp.status_code not in (200, 202):
                    logger.warning(
                        "lead_reopen: AMO вернул %d при реанимации lead_id=%d",
                        resp.status_code, lead_id,
                    )
                    return False

                logger.info("lead_reopen: лид id=%d реанимирован (статус=%d)", lead_id, _STATUS_REOPENED)
                return True

        except Exception:
            logger.exception("lead_reopen: ошибка при реанимации lead_id=%d", lead_id)
            return False

    async def _close_as_duplicate(self, lead_id: int, original_lead_id: int) -> bool:
        """Закрывает новый лид как дубликат с пометкой на оригинальный лид.

        PATCH /api/v4/leads/{lead_id}
        """
        payload = {
            "status_id": _STATUS_CLOSED,
            "loss_reason_id": None,
            "custom_fields_values": [
                {
                    "field_id": _FIELD_REFUSAL_REASON,
                    "values": [{"value": _REASON_DUPLICATE}],
                },
            ],
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.patch(
                    f"{self._base_url()}/api/v4/leads/{lead_id}",
                    json=payload,
                    headers=self._headers(),
                )

                if resp.status_code not in (200, 202):
                    logger.warning(
                        "lead_reopen: AMO вернул %d при закрытии дубликата lead_id=%d "
                        "(оригинал id=%d)",
                        resp.status_code, lead_id, original_lead_id,
                    )
                    return False

                logger.info(
                    "lead_reopen: дубликат lead_id=%d закрыт (оригинал id=%d)",
                    lead_id, original_lead_id,
                )
                return True

        except Exception:
            logger.exception(
                "lead_reopen: ошибка при закрытии дубликата lead_id=%d", lead_id
            )
            return False

    async def _add_reopen_note(self, lead_id: int, duplicate_lead_id: int) -> bool:
        """Добавляет заметку к реанимированному лиду о дубликате.

        POST /api/v4/leads/{lead_id}/notes
        """
        note_text = (
            f"Лид реанимирован автоматически. "
            f"Клиент повторно обратился. "
            f"Дубликат лид #{duplicate_lead_id} закрыт."
        )
        payload = [
            {
                "note_type": "common",
                "params": {"text": note_text},
            }
        ]

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self._base_url()}/api/v4/leads/{lead_id}/notes",
                    json=payload,
                    headers=self._headers(),
                )

                if resp.status_code not in (200, 201):
                    logger.warning(
                        "lead_reopen: AMO вернул %d при добавлении заметки к lead_id=%d",
                        resp.status_code, lead_id,
                    )
                    return False

                logger.info(
                    "lead_reopen: добавлена заметка к реанимированному лиду id=%d "
                    "(дубликат id=%d)",
                    lead_id, duplicate_lead_id,
                )
                return True

        except Exception:
            logger.exception(
                "lead_reopen: ошибка при добавлении заметки к lead_id=%d", lead_id
            )
            return False

    def _extract_field_value(self, lead_data: dict, field_id: int) -> str | None:
        """Возвращает первое значение кастомного поля по field_id."""
        fields = lead_data.get("custom_fields_values") or []
        for field in fields:
            if field.get("field_id") == field_id:
                values = field.get("values") or []
                if values:
                    return str(values[0].get("value", ""))
        return None


# Глобальный инстанс — импортируется в webhook handler
lead_reopen = LeadReopenService()
