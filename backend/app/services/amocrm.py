"""Клиент AMO CRM для создания лидов и заметок о звонках.

Интеграция опциональна: если amo_subdomain или amo_token не настроены —
методы возвращают None/False с warning-логом, не прерывая обработку звонка.
"""

import logging
import time

import httpx

from app.core.config import settings
from app.core.amo_constants import (
    FIELD_CITY,
    FIELD_DEPARTMENT,
    FIELD_UTM_SOURCE,
    FIELD_UTM_MEDIUM,
    FIELD_UTM_CAMPAIGN,
    FIELD_UTM_CONTENT,
    FIELD_UTM_TERM,
    ENUM_DEPT_OFFLINE,
)
from app.models.call import Call

logger = logging.getLogger(__name__)


class AmoAuthError(Exception):
    """AMO вернул 401/403 — токен протух. Не создаём лид чтобы не плодить дубли."""


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
    # 2GIS-листинги — статичные номера в карточках компаний
    "2gis_almaty":   "Алматы",
    "2gis_astana":   "Астана",
    "2gis_shymkent": "Шымкент",
    "2gis_atyrau":   "Атырау",
    "2gis_aktobe":   "Актобе",
    # Taplink-страницы городов (кнопка "позвонить" на каждый город)
    "taplink_almaty":   "Алматы",
    "taplink_astana":   "Астана",
    "taplink_shymkent": "Шымкент",
    "taplink_atyrau":   "Атырау",
    "taplink_aktobe":   "Актобе",
}


# Окно поиска свежего лида — 5 минут в секундах
_RECENT_LEAD_WINDOW_SECONDS = 300


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
            {"field_id": FIELD_DEPARTMENT, "values": [{"enum_id": ENUM_DEPT_OFFLINE}]},
        ]
        if call.source:
            custom.append({"field_code": "UTM_SOURCE", "values": [{"value": call.source}]})
            custom.append({"field_id": FIELD_UTM_SOURCE, "values": [{"value": call.source}]})
        if call.medium:
            custom.append({"field_code": "UTM_MEDIUM", "values": [{"value": call.medium}]})
            custom.append({"field_id": FIELD_UTM_MEDIUM, "values": [{"value": call.medium}]})
        if call.campaign:
            custom.append({"field_code": "UTM_CAMPAIGN", "values": [{"value": call.campaign}]})
            custom.append({"field_id": FIELD_UTM_CAMPAIGN, "values": [{"value": call.campaign}]})
        if call.keyword:
            custom.append({"field_code": "UTM_TERM", "values": [{"value": call.keyword}]})
            custom.append({"field_id": FIELD_UTM_TERM, "values": [{"value": call.keyword}]})
        if call.tracking_did:
            # DID кладём в UTM_CONTENT как "did:7004982670" — универсальный способ
            # пометить на какой номер был звонок без создания своего поля.
            did_value = f"did:{call.tracking_did}"
            custom.append({"field_code": "UTM_CONTENT", "values": [{"value": did_value}]})
            custom.append({"field_id": FIELD_UTM_CONTENT, "values": [{"value": did_value}]})
        # Город: приоритет — явный 2GIS source. Иначе — пробуем достать из campaign.
        city_value: str | None = _SOURCE_TO_CITY.get(call.source) or _city_from_campaign(call.campaign)
        if city_value:
            custom.append({
                "field_id": FIELD_CITY,
                "values": [{"value": city_value}],
            })
        else:
            # site/insta/fb-без-кампании — оставляем поле пустым
            custom.append({
                "field_id": FIELD_CITY,
                "values": None,
            })
        return custom

    async def _find_recent_lead_by_caller(
        self,
        client: httpx.AsyncClient,
        caller: str,
    ) -> tuple[int | None, bool]:
        """Ищет в AMO ЛЮБОЙ свежий лид (5 мин) от того же caller.

        Возвращает (lead_id, is_ours):
          - is_ours=True  — это наш kurotrack-лид (UTM_REFERRER=kurotrack)
          - is_ours=False — это лид от Asterisk-интеграции (без маркера)
          - (None, False) — ничего не нашлось
        Бросает AmoAuthError при 401/403 — вызывающий код НЕ должен создавать лид.
        """
        # AMO query принимает номер без + (только цифры)
        phone_no_plus = caller.lstrip("+")
        threshold_ts = int(time.time()) - _RECENT_LEAD_WINDOW_SECONDS

        try:
            resp = await client.get(
                f"{self._base_url()}/api/v4/leads",
                params={"query": phone_no_plus, "limit": 20, "with": "contacts"},
                headers=self._headers(),
            )
        except httpx.TimeoutException:
            # Таймаут — сетевая проблема, не auth. Лучше создать дубль чем потерять лид.
            logger.warning(
                "AMO: таймаут при поиске лида caller=%s — создаём новый лид",
                caller,
            )
            return (None, False)
        except Exception:
            logger.warning(
                "AMO: ошибка поиска лида для caller=%s — создаём новый лид",
                caller,
            )
            return (None, False)

        # При 401/403 токен протух — дальнейшее создание лида породит дубли.
        # Бросаем исключение чтобы вызывающий код прервал обработку.
        if resp.status_code in (401, 403):
            logger.error(
                "AMO: ошибка авторизации (токен протух?) при поиске лида caller=%s status=%d — НЕ создаём лид",
                caller, resp.status_code,
            )
            raise AmoAuthError(f"AMO auth failed: {resp.status_code}")

        try:
            resp.raise_for_status()
        except Exception:
            logger.warning(
                "AMO: ошибка поиска лида для caller=%s status=%s — создаём новый лид",
                caller, resp.status_code,
            )
            return (None, False)

        # AMO возвращает 204 No Content когда лидов по query нет — это нормально
        if resp.status_code == 204 or not resp.content:
            return (None, False)
        try:
            data = resp.json()
        except Exception:
            logger.warning("AMO: пустой/невалидный JSON ответ при поиске лида для caller=%s", caller)
            return (None, False)
        leads: list[dict] = data.get("_embedded", {}).get("leads", [])
        if not leads:
            return (None, False)

        # Сортируем по created_at убывающий — берём самый свежий первым
        leads_sorted = sorted(leads, key=lambda x: x.get("created_at", 0), reverse=True)

        for lead in leads_sorted:
            created_at = lead.get("created_at", 0)
            # Лид должен быть создан не раньше чем 5 минут назад
            if created_at < threshold_ts:
                break  # дальше только старее — нет смысла смотреть

            # Проверяем наличие маркера UTM_REFERRER=kurotrack
            custom_fields = lead.get("custom_fields_values") or []
            is_ours = any(
                cf.get("field_code") == "UTM_REFERRER"
                and any(
                    str(v.get("value", "")).lower() == "kurotrack"
                    for v in (cf.get("values") or [])
                )
                for cf in custom_fields
            )
            return (lead["id"], is_ours)

        return (None, False)

    async def create_lead_from_call(self, call: Call, caller: str) -> int | None:
        """Создаёт или обновляет лид в AMO CRM по данным входящего звонка.

        Логика защиты от дублей при параллельных AMI leg'ах:
        1. Ищем ЛЮБОЙ свежий лид (5 мин) по caller — наш или Asterisk-овский.
        2. Если нашли наш (is_ours=True) — возвращаем его id без изменений.
           Это второй/третий leg того же звонка — дубль подавляется.
        3. Если нашли Asterisk-овский (is_ours=False) — PATCH его нашими UTM.
        4. Если ничего нет — создаём новый лид через /leads/complex.

        Возвращает lead_id или None при ошибке / отключённой интеграции.
        При AmoAuthError (401/403) возвращает None и НЕ создаёт лид.
        """
        if not self._is_configured():
            logger.warning(
                "AMO CRM не настроен (amo_subdomain/amo_token пустые) — пропускаем создание лида"
            )
            return None

        lead_custom = self._build_custom_fields(call, caller)

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Ищем свежий лид по caller (за 5 минут).
                # AmoAuthError пробрасывается наружу — поймаем ниже.
                try:
                    existing_id, is_ours = await self._find_recent_lead_by_caller(client, caller)
                except AmoAuthError:
                    # Токен протух — прерываем создание лида чтобы не плодить дубли.
                    logger.error(
                        "AMO CRM: прерываем create_lead_from_call из-за auth-ошибки "
                        "(caller=%s uniqueid=%s) — обнови токен в настройках",
                        caller, call.uniqueid,
                    )
                    return None

                if existing_id and is_ours:
                    # Наш же лид от предыдущего leg-а — просто привязываем call, не создаём дубль
                    logger.info(
                        "AMO CRM: дубль leg — привязан к нашему лиду id=%s caller=%s uniqueid=%s",
                        existing_id, caller, call.uniqueid,
                    )
                    return existing_id

                if existing_id and not is_ours:
                    # Asterisk-овский лид — обновляем его нашими UTM/отдел/город
                    patch_body: dict = {"custom_fields_values": lead_custom}
                    if settings.amo_pipeline_id is not None:
                        patch_body["pipeline_id"] = settings.amo_pipeline_id
                    if settings.amo_responsible_user_id is not None:
                        patch_body["responsible_user_id"] = settings.amo_responsible_user_id

                    patch_resp = await client.patch(
                        f"{self._base_url()}/api/v4/leads/{existing_id}",
                        json=patch_body,
                        headers=self._headers(),
                    )
                    patch_resp.raise_for_status()
                    logger.info(
                        "AMO: дополнили UTM на Asterisk-овском лиде id=%s для caller=%s",
                        existing_id, caller,
                    )
                    return existing_id

                # Ничего не нашли — создаём новый лид через /leads/complex
                lead_body: dict = {"name": f"Входящий звонок {caller}"}
                if settings.amo_pipeline_id is not None:
                    lead_body["pipeline_id"] = settings.amo_pipeline_id
                if settings.amo_responsible_user_id is not None:
                    lead_body["responsible_user_id"] = settings.amo_responsible_user_id

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

                # AMO CRM при POST /leads/complex игнорирует values=null и подставляет
                # дефолтный enum "Другой" для enum-полей. Исправляем явным PATCH после создания.
                # PATCH с values=null корректно очищает поле — проверено на API v4.
                city_value = _SOURCE_TO_CITY.get(call.source) or _city_from_campaign(call.campaign)
                if not city_value:
                    try:
                        await client.patch(
                            f"{self._base_url()}/api/v4/leads/{lead_id}",
                            json={"custom_fields_values": [{"field_id": FIELD_CITY, "values": None}]},
                            headers=self._headers(),
                        )
                    except Exception:
                        logger.exception(
                            "AMO: ошибка очистки города у созданного лида id=%s", lead_id
                        )

                return lead_id
        except Exception:
            logger.exception(
                "AMO CRM: ошибка создания лида для caller=%s uniqueid=%s",
                caller, call.uniqueid,
            )
            return None


# Глобальный инстанс — импортируется в call_processor
amocrm_client = AmoCRMClient()
