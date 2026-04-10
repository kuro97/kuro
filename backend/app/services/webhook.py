"""Webhook-сервис: отправляет данные о звонках во внешние системы (CRM, аналитика)."""

import logging

import httpx

logger = logging.getLogger(__name__)


class WebhookSender:
    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def send(self, url: str, payload: dict, headers: dict | None = None) -> bool:
        """Отправляет webhook на указанный URL. Возвращает True при успехе."""
        client = await self._get_client()
        try:
            response = await client.post(url, json=payload, headers=headers or {})
            if response.status_code < 300:
                logger.info("Webhook sent to %s: %d", url, response.status_code)
                return True
            else:
                logger.warning("Webhook failed %s: %d %s", url, response.status_code, response.text)
                return False
        except Exception:
            logger.exception("Webhook error for %s", url)
            return False

    async def send_call_event(self, webhook_url: str, call_data: dict) -> bool:
        """Отправляет событие звонка в CRM/аналитику."""
        payload = {
            "event": "call.completed",
            "data": {
                "caller_number": call_data.get("caller_number"),
                "tracking_did": call_data.get("tracking_did"),
                "duration": call_data.get("duration"),
                "billsec": call_data.get("billsec"),
                "disposition": call_data.get("disposition"),
                "source": call_data.get("source"),
                "medium": call_data.get("medium"),
                "campaign": call_data.get("campaign"),
                "keyword": call_data.get("keyword"),
                "is_unique": call_data.get("is_unique"),
                "is_target": call_data.get("is_target"),
                "recording_url": call_data.get("recording_url"),
                "started_at": call_data.get("started_at"),
            },
        }
        return await self.send(webhook_url, payload)

    async def send_to_bitrix24(self, webhook_url: str, call_data: dict) -> bool:
        """Создаёт лид в Битрикс24 при входящем звонке."""
        payload = {
            "fields": {
                "TITLE": f"Звонок от {call_data.get('caller_number', 'unknown')}",
                "PHONE": [{"VALUE": call_data.get("caller_number"), "VALUE_TYPE": "WORK"}],
                "SOURCE_ID": "CALL",
                "SOURCE_DESCRIPTION": (
                    f"Источник: {call_data.get('source', 'direct')}, "
                    f"Кампания: {call_data.get('campaign', '-')}"
                ),
                "UTM_SOURCE": call_data.get("source", ""),
                "UTM_MEDIUM": call_data.get("medium", ""),
                "UTM_CAMPAIGN": call_data.get("campaign", ""),
                "UTM_TERM": call_data.get("keyword", ""),
            }
        }
        return await self.send(f"{webhook_url}/crm.lead.add.json", payload)

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


webhook_sender = WebhookSender()
