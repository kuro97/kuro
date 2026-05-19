"""Клиент AMO CRM для создания лидов и заметок о звонках.

Интеграция опциональна: если amo_subdomain или amo_token не настроены —
методы возвращают None/False с warning-логом, не прерывая обработку звонка.
"""

import logging
import time

import httpx

from app.core.config import settings
from app.models.call import Call

logger = logging.getLogger(__name__)


# Извлечение города из UTM_CAMPAIGN. Маркетологи кодируют город суффиксом:
#   traffic_mektep_alm, traffic_mektep_ast, Poisk_BIL_Astana и т.п.
# Ключи — case-insensitive подстроки которые ищем в campaign.
_CAMPAIGN_TOKEN_TO_CITY = {
    "almaty":   "Алматы",
    "_alm":     "Алматы",
    "astana":   "Астана",
    "_ast":     "Астана",
    "shymkent": "Шымкент",
    "_shy":     "Шымкент",
    "atyrau":   "Атырау",
    "_aty":     "Атырау",
    "aktobe":   "Актобе",
    "_akt":     "Актобе",
}


def _city_from_campaign(campaign: str | None) -> str | None:
    """Возвращает название города если в campaign есть распознаваемый токен.

    Ищет case-insensitive, проверяет более длинные ключи первыми
    (чтобы 'almaty' нашёлся до '_alm' и т.п.).
    """
    if not campaign:
        return None
    c = campaign.lower()
    # Сортируем по длине ключа — длинные имена городов проверяем первыми
    for token in sorted(_CAMPAIGN_TOKEN_TO_CITY, key=len, reverse=True):
        if token in c:
            return _CAMPAIGN_TOKEN_TO_CITY[token]
    return None


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

# Окно поиска Asterisk-овского лида — 5 минут в секундах
_ASTERISK_WINDOW_SECONDS = 300


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

    def _build_custom_fields(self, call: Call, caller: str) -> list[dict]:
        """Собирает список custom_fields_values для лида.

        Используется и при создании нового лида, и при PATCH существующего.
        Не включает контактные данные — только UTM/отдел/город.
        """
        custom: list[dict] = [
            # Единый маркер всех лидов от KuroTrack — фильтр "все звонки".
            {"field_code": "UTM_REFERRER", "values": [{"value": "kurotrack"}]},
            # Поле "Отдел" в AMO (field_id=912857): Offline=914379, Online=914381.
            # Все звонковые лиды по определению offline (клиент позвонил сам).
            {"field_id": 912857, "values": [{"enum_id": 914379}]},
        ]
        if call.source:
            custom.append({"field_code": "UTM_SOURCE", "values": [{"value": call.source}]})
            custom.append({"field_id": 869441, "values": [{"value": call.source}]})
        if call.medium:
            custom.append({"field_code": "UTM_MEDIUM", "values": [{"value": call.medium}]})
            custom.append({"field_id": 869443, "values": [{"value": call.medium}]})
        if call.campaign:
            custom.append({"field_code": "UTM_CAMPAIGN", "values": [{"value": call.campaign}]})
            custom.append({"field_id": 869445, "values": [{"value": call.campaign}]})
        if call.keyword:
            custom.append({"field_code": "UTM_TERM", "values": [{"value": call.keyword}]})
            custom.append({"field_id": 869449, "values": [{"value": call.keyword}]})
        if call.tracking_did:
            # DID кладём в UTM_CONTENT как "did:7004982670" — универсальный способ
            # пометить на какой номер был звонок без создания своего поля.
            did_value = f"did:{call.tracking_did}"
            custom.append({"field_code": "UTM_CONTENT", "values": [{"value": did_value}]})
            custom.append({"field_id": 869447, "values": [{"value": did_value}]})
        # Город: приоритет — явный 2GIS source. Иначе — пробуем достать из campaign.
        city_value: str | None = _SOURCE_TO_CITY.get(call.source) or _city_from_campaign(call.campaign)
        if city_value:
            custom.append({
                "field_id": _FIELD_CITY_ID,
                "values": [{"value": city_value}],
            })
        else:
            # site/insta/fb-без-кампании — оставляем поле пустым
            custom.append({
                "field_id": _FIELD_CITY_ID,
                "values": None,
            })
        return custom

    async def _find_existing_asterisk_lead(
        self,
        client: httpx.AsyncClient,
        caller: str,
    ) -> int | None:
        """Ищет в AMO лид от Asterisk (без UTM_REFERRER=kurotrack) за последние 5 минут.

        Возвращает lead_id если найден, иначе None.
        При любой ошибке AMO — возвращает None (fallback на создание нового лида).
        """
        # AMO query принимает номер без + (только цифры)
        phone_no_plus = caller.lstrip("+")
        threshold_ts = int(time.time()) - _ASTERISK_WINDOW_SECONDS

        try:
            resp = await client.get(
                f"{self._base_url()}/api/v4/leads",
                params={"query": phone_no_plus, "limit": 20, "with": "contacts"},
                headers=self._headers(),
            )
            resp.raise_for_status()
        except Exception:
            logger.warning(
                "AMO: ошибка поиска лида для caller=%s — создаём новый лид",
                caller,
            )
            return None

        data = resp.json()
        leads: list[dict] = data.get("_embedded", {}).get("leads", [])
        if not leads:
            return None

        # Сортируем по created_at убывающий — берём самый свежий первым
        leads_sorted = sorted(leads, key=lambda x: x.get("created_at", 0), reverse=True)

        for lead in leads_sorted:
            created_at = lead.get("created_at", 0)
            # Лид должен быть создан не раньше чем 5 минут назад
            if created_at < threshold_ts:
                break  # дальше только старее — нет смысла смотреть

            # Проверяем что это НЕ наш лид (нет UTM_REFERRER=kurotrack)
            custom_fields = lead.get("custom_fields_values") or []
            has_kurotrack_marker = any(
                cf.get("field_code") == "UTM_REFERRER"
                and any(v.get("value") == "kurotrack" for v in (cf.get("values") or []))
                for cf in custom_fields
            )
            if not has_kurotrack_marker:
                return lead["id"]

        return None

    async def create_lead_from_call(self, call: Call, caller: str) -> int | None:
        """Создаёт или обновляет лид в AMO CRM по данным входящего звонка.

        Логика:
        1. Ищем Asterisk-овский лид за последние 5 минут с этим же номером.
        2. Если найден — PATCH его нашими UTM/отдел/город, не трогаем имя.
        3. Если нет — создаём новый лид через /leads/complex как раньше.

        Возвращает lead_id или None при ошибке / отключённой интеграции.
        """
        if not self._is_configured():
            logger.warning(
                "AMO CRM не настроен (amo_subdomain/amo_token пустые) — пропускаем создание лида"
            )
            return None

        custom_fields = self._build_custom_fields(call, caller)

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Сначала ищем Asterisk-овский лид чтобы не плодить дубли
                asterisk_lead_id = await self._find_existing_asterisk_lead(client, caller)

                if asterisk_lead_id is not None:
                    # PATCH существующего лида — добавляем наши UTM/отдел/город.
                    # Имя лида ("87XXX - Входящий") НЕ трогаем.
                    patch_body: dict = {"custom_fields_values": custom_fields}
                    if settings.amo_pipeline_id is not None:
                        patch_body["pipeline_id"] = settings.amo_pipeline_id
                    if settings.amo_responsible_user_id is not None:
                        patch_body["responsible_user_id"] = settings.amo_responsible_user_id

                    patch_resp = await client.patch(
                        f"{self._base_url()}/api/v4/leads/{asterisk_lead_id}",
                        json=patch_body,
                        headers=self._headers(),
                    )
                    patch_resp.raise_for_status()
                    logger.info(
                        "AMO: дополнили UTM на Asterisk-овском лиде id=%s для caller=%s",
                        asterisk_lead_id, caller,
                    )
                    return asterisk_lead_id

                # Asterisk-овского лида нет — создаём новый через /leads/complex
                lead_body: dict = {"name": f"Входящий звонок {caller}"}
                if settings.amo_pipeline_id is not None:
                    lead_body["pipeline_id"] = settings.amo_pipeline_id
                if settings.amo_responsible_user_id is not None:
                    lead_body["responsible_user_id"] = settings.amo_responsible_user_id

                lead_body["custom_fields_values"] = custom_fields
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
