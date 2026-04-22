"""Клиент AMO CRM для создания лидов и заметок о звонках.

Интеграция опциональна: если amo_subdomain или amo_token не настроены —
методы возвращают None/False с warning-логом, не прерывая обработку звонка.
"""

import logging

import httpx

from app.core.config import settings
from app.models.call import Call

logger = logging.getLogger(__name__)


class AmoCRMClient:
    """Клиент для работы с AMO CRM API v4."""

    def _base_url(self) -> str:
        return f"https://{settings.amo_subdomain}.amocrm.ru"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {settings.amo_token}",
            "Content-Type": "application/json",
        }

    def _is_configured(self) -> bool:
        return bool(settings.amo_subdomain and settings.amo_token)

    async def create_lead_from_call(self, call: Call, caller: str) -> int | None:
        """Создаёт лид в AMO CRM по данным входящего звонка.

        Возвращает lead_id из ответа AMO или None при ошибке / отключённой интеграции.
        """
        if not self._is_configured():
            logger.warning(
                "AMO CRM не настроен (amo_subdomain/amo_token пустые) — пропускаем создание лида"
            )
            return None

        # Формируем тело запроса: лид + встроенный контакт с телефоном
        lead_body: dict = {"name": f"Входящий звонок {caller}"}
        if settings.amo_pipeline_id is not None:
            lead_body["pipeline_id"] = settings.amo_pipeline_id
        if settings.amo_responsible_user_id is not None:
            lead_body["responsible_user_id"] = settings.amo_responsible_user_id

        lead_body["_embedded"] = {
            "contacts": [
                {
                    "name": caller,
                    "custom_fields_values": [
                        {
                            "field_code": "PHONE",
                            "values": [{"value": caller}],
                        }
                    ],
                }
            ]
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    f"{self._base_url()}/api/v4/leads",
                    json=[lead_body],
                    headers=self._headers(),
                )
                response.raise_for_status()
                data = response.json()
                lead_id: int = data["_embedded"]["leads"][0]["id"]
                logger.info(
                    "AMO CRM: создан лид id=%s для caller=%s uniqueid=%s",
                    lead_id, caller, call.uniqueid,
                )
                return lead_id
        except Exception:
            logger.exception(
                "AMO CRM: ошибка создания лида для caller=%s uniqueid=%s",
                caller, call.uniqueid,
            )
            return None

    async def add_call_note(self, lead_id: int, call: Call) -> bool:
        """Добавляет заметку о звонке к лиду в AMO CRM.

        Возвращает True при успехе, False при ошибке.
        """
        if not self._is_configured():
            return False

        # call_status: 1 — отвечен, 4 — пропущен
        call_status = 1 if call.disposition == "ANSWERED" else 4

        note_body = {
            "note_type": "call_in",
            "params": {
                "uniq": call.uniqueid,
                "duration": call.billsec,
                "source": call.tracking_did,
                "phone": call.caller_number,
                "call_status": call_status,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    f"{self._base_url()}/api/v4/leads/{lead_id}/notes",
                    json=[note_body],
                    headers=self._headers(),
                )
                response.raise_for_status()
                logger.info(
                    "AMO CRM: добавлена заметка к лиду id=%s uniqueid=%s",
                    lead_id, call.uniqueid,
                )
                return True
        except Exception:
            logger.exception(
                "AMO CRM: ошибка добавления заметки к лиду id=%s uniqueid=%s",
                lead_id, call.uniqueid,
            )
            return False


# Глобальный инстанс — импортируется в call_processor
amocrm_client = AmoCRMClient()
