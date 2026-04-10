"""Клиент для Asterisk Manager Interface (AMI). Слушает события звонков в реальном времени."""

import asyncio
import logging

from panoramisk import Manager

from app.core.config import settings

logger = logging.getLogger(__name__)


class AMIClient:
    def __init__(self):
        self.manager = Manager(
            host=settings.ami_host,
            port=settings.ami_port,
            username=settings.ami_username,
            secret=settings.ami_secret,
            ping_delay=10,
        )
        self._call_handlers: list = []

    def on_call_event(self, handler):
        """Декоратор/метод для регистрации обработчиков событий звонков."""
        self._call_handlers.append(handler)
        return handler

    async def _handle_newchannel(self, manager, message):
        """Новый канал — начало звонка."""
        event_data = {
            "event": "new_call",
            "uniqueid": message.get("Uniqueid"),
            "channel": message.get("Channel"),
            "caller_id_num": message.get("CallerIDNum"),
            "caller_id_name": message.get("CallerIDName"),
            "exten": message.get("Exten"),
            "context": message.get("Context"),
        }
        for handler in self._call_handlers:
            try:
                await handler(event_data)
            except Exception:
                logger.exception("Error in call handler")

    async def _handle_hangup(self, manager, message):
        """Завершение звонка."""
        event_data = {
            "event": "hangup",
            "uniqueid": message.get("Uniqueid"),
            "channel": message.get("Channel"),
            "cause": message.get("Cause"),
            "cause_txt": message.get("Cause-txt"),
        }
        for handler in self._call_handlers:
            try:
                await handler(event_data)
            except Exception:
                logger.exception("Error in call handler")

    async def _handle_cdr(self, manager, message):
        """CDR — полная запись о звонке после завершения."""
        event_data = {
            "event": "cdr",
            "uniqueid": message.get("UniqueID"),
            "src": message.get("Source"),
            "dst": message.get("Destination"),
            "dcontext": message.get("DestinationContext"),
            "duration": message.get("Duration"),
            "billsec": message.get("BillableSeconds"),
            "disposition": message.get("Disposition"),
            "channel": message.get("Channel"),
            "dstchannel": message.get("DestinationChannel"),
        }
        for handler in self._call_handlers:
            try:
                await handler(event_data)
            except Exception:
                logger.exception("Error in call handler")

    async def connect(self):
        """Подключается к AMI и начинает слушать события."""
        self.manager.register_event("Newchannel", self._handle_newchannel)
        self.manager.register_event("Hangup", self._handle_hangup)
        self.manager.register_event("Cdr", self._handle_cdr)

        logger.info("Connecting to Asterisk AMI at %s:%s", settings.ami_host, settings.ami_port)
        await self.manager.connect()
        logger.info("Connected to Asterisk AMI")

    async def disconnect(self):
        """Отключается от AMI."""
        self.manager.close()
        logger.info("Disconnected from Asterisk AMI")

    async def originate_call(self, number: str, extension: str, context: str = "from-internal"):
        """Инициирует исходящий звонок (для callback-виджета)."""
        response = await self.manager.send_action({
            "Action": "Originate",
            "Channel": f"PJSIP/{extension}",
            "Exten": number,
            "Context": context,
            "Priority": "1",
            "CallerID": f"Callback <{number}>",
            "Timeout": "30000",
            "Async": "true",
        })
        return response


ami_client = AMIClient()
