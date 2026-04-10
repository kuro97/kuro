"""Интеграции с аналитическими системами: Google Analytics 4, Яндекс.Метрика."""

import logging

import httpx

logger = logging.getLogger(__name__)


class GA4Integration:
    """Отправка событий звонков в Google Analytics 4 через Measurement Protocol."""

    GA4_URL = "https://www.google-analytics.com/mp/collect"

    def __init__(self, measurement_id: str, api_secret: str):
        self.measurement_id = measurement_id
        self.api_secret = api_secret

    async def send_call_event(self, client_id: str, call_data: dict) -> bool:
        """Отправляет событие call_tracking в GA4.

        client_id: значение cookie _ga посетителя (из сессии)
        call_data: данные о звонке
        """
        payload = {
            "client_id": client_id,
            "events": [
                {
                    "name": "call_tracking",
                    "params": {
                        "source": call_data.get("source", "direct"),
                        "medium": call_data.get("medium", ""),
                        "campaign": call_data.get("campaign", ""),
                        "keyword": call_data.get("keyword", ""),
                        "caller_number": call_data.get("caller_number", ""),
                        "duration": call_data.get("billsec", 0),
                        "disposition": call_data.get("disposition", ""),
                        "is_target": call_data.get("is_target", False),
                        "value": 1 if call_data.get("disposition") == "ANSWERED" else 0,
                    },
                }
            ],
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.GA4_URL,
                    params={
                        "measurement_id": self.measurement_id,
                        "api_secret": self.api_secret,
                    },
                    json=payload,
                    timeout=5.0,
                )
                if response.status_code < 300:
                    logger.info("GA4 event sent: client_id=%s", client_id)
                    return True
                logger.warning("GA4 error: %d %s", response.status_code, response.text)
                return False
        except Exception:
            logger.exception("GA4 send failed")
            return False


class YandexMetrikaIntegration:
    """Загрузка офлайн-конверсий в Яндекс.Метрику."""

    YM_URL = "https://api-metrika.yandex.net/management/v1/counter/{counter_id}/offline_conversions/upload"

    def __init__(self, counter_id: str, oauth_token: str):
        self.counter_id = counter_id
        self.oauth_token = oauth_token

    async def send_call_conversion(self, client_id: str, call_data: dict) -> bool:
        """Загружает конверсию звонка как офлайн-конверсию.

        client_id: Yandex ClientID из cookie _ym_uid
        call_data: данные о звонке
        """
        # Формат CSV для загрузки
        csv_content = (
            "ClientId,Target,DateTime,Price,Currency\n"
            f"{client_id},call_tracking,{call_data.get('started_at', '')},1,KZT\n"
        )

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.YM_URL.format(counter_id=self.counter_id),
                    headers={
                        "Authorization": f"OAuth {self.oauth_token}",
                        "Content-Type": "text/csv",
                    },
                    content=csv_content.encode(),
                    timeout=10.0,
                )
                if response.status_code < 300:
                    logger.info("YM conversion uploaded: client_id=%s", client_id)
                    return True
                logger.warning("YM error: %d %s", response.status_code, response.text)
                return False
        except Exception:
            logger.exception("YM upload failed")
            return False


class AnalyticsDispatcher:
    """Диспетчер аналитики: отправляет события во все настроенные системы."""

    def __init__(self):
        self._ga4: GA4Integration | None = None
        self._ym: YandexMetrikaIntegration | None = None

    def configure_ga4(self, measurement_id: str, api_secret: str):
        self._ga4 = GA4Integration(measurement_id, api_secret)

    def configure_ym(self, counter_id: str, oauth_token: str):
        self._ym = YandexMetrikaIntegration(counter_id, oauth_token)

    async def dispatch_call(self, client_id: str, call_data: dict):
        """Отправляет событие звонка во все настроенные системы."""
        if self._ga4:
            await self._ga4.send_call_event(client_id, call_data)
        if self._ym:
            await self._ym.send_call_conversion(client_id, call_data)


analytics = AnalyticsDispatcher()
