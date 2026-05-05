"""Клиент AMO CRM для создания лидов и заметок о звонках.

Интеграция опциональна: если amo_subdomain или amo_token не настроены —
методы возвращают None/False с warning-логом, не прерывая обработку звонка.
"""

import logging

import httpx

from app.core.config import settings
from app.models.call import Call

logger = logging.getLogger(__name__)


# Маппинг source → город для 2GIS-номеров.
# ID поля "Город" в AMO = 879211 (enum-поле, AMO мапит value на enum по тексту).
_SOURCE_TO_CITY = {
    "2gis_almaty":   "Алматы",
    "2gis_astana":   "Астана",
    "2gis_shymkent": "Шымкент",
    "2gis_atyrau":   "Атырау",
    "2gis_aktobe":   "Актобе",
}

_FIELD_CITY_ID = 879211


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

        # Пишем UTM-метки в ДВА места одновременно:
        # 1. tracking_data поля (по field_code) — для внутренней статистики и
        #    фильтров воронки AMO (Воронка → Фильтр → utm_source).
        # 2. text-поля (по field_id) — для отображения непосредственно на
        #    карточке лида. tracking_data на карточке AMO не показывает.
        #
        # ID text-полей у AMO аккаунта qadam (получены через GET /api/v4/leads/custom_fields):
        #   869441 = UTM_SOURCE
        #   869443 = UTM_MEDIUM
        #   869445 = UTM_CAMPAIGN
        #   869447 = UTM_CONTENT
        #   869449 = UTM_TERM
        lead_custom: list[dict] = [
            # Единый маркер всех лидов от KuroTrack — фильтр "все звонки".
            {"field_code": "UTM_REFERRER", "values": [{"value": "kurotrack"}]},
            # Поле "Отдел" в AMO (field_id=912857): Offline=914379, Online=914381.
            # Все звонковые лиды по определению offline (клиент позвонил сам).
            {"field_id": 912857, "values": [{"enum_id": 914379}]},
        ]
        if call.source:
            lead_custom.append({"field_code": "UTM_SOURCE", "values": [{"value": call.source}]})
            lead_custom.append({"field_id": 869441, "values": [{"value": call.source}]})
        if call.medium:
            lead_custom.append({"field_code": "UTM_MEDIUM", "values": [{"value": call.medium}]})
            lead_custom.append({"field_id": 869443, "values": [{"value": call.medium}]})
        if call.campaign:
            lead_custom.append({"field_code": "UTM_CAMPAIGN", "values": [{"value": call.campaign}]})
            lead_custom.append({"field_id": 869445, "values": [{"value": call.campaign}]})
        if call.keyword:
            lead_custom.append({"field_code": "UTM_TERM", "values": [{"value": call.keyword}]})
            lead_custom.append({"field_id": 869449, "values": [{"value": call.keyword}]})
        if call.tracking_did:
            # DID кладём в UTM_CONTENT как "did:7004982670" — универсальный способ
            # пометить на какой номер был звонок без создания своего поля.
            did_value = f"did:{call.tracking_did}"
            lead_custom.append({"field_code": "UTM_CONTENT", "values": [{"value": did_value}]})
            lead_custom.append({"field_id": 869447, "values": [{"value": did_value}]})
        # Город:
        #   - для 2GIS-источников передаём конкретный город по value
        #   - для всех остальных (site/insta/fb/tiktok/direct/null) — "Online"
        #     (enum_id=914441 в нашем AMO). AMO не позволяет очистить enum-поле
        #     полностью, а "Online" семантически правильнее чем default "Другой".
        if call.source in _SOURCE_TO_CITY:
            lead_custom.append({
                "field_id": _FIELD_CITY_ID,
                "values": [{"value": _SOURCE_TO_CITY[call.source]}],
            })
        else:
            lead_custom.append({
                "field_id": _FIELD_CITY_ID,
                "values": [{"enum_id": 914441}],  # Online
            })

        if lead_custom:
            lead_body["custom_fields_values"] = lead_custom

        lead_body["_embedded"] = {
            "contacts": [
                {
                    "name": caller,
                    "custom_fields_values": [
                        {
                            "field_code": "PHONE",
                            "values": [{"value": caller, "enum_code": "MOB"}],
                        }
                    ],
                }
            ]
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # /leads/complex создаёт лид вместе с новым контактом в одном запросе.
                # Обычный /leads требует ссылку на существующий contact id.
                response = await client.post(
                    f"{self._base_url()}/api/v4/leads/complex",
                    json=[lead_body],
                    headers=self._headers(),
                )
                response.raise_for_status()
                data = response.json()
                # /leads/complex возвращает массив: [{"id": N, "contact_id": M, ...}]
                lead_id: int = data[0]["id"]
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
