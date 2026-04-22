"""Клиент для Asterisk Manager Interface (AMI).
Слушает события звонков в реальном времени, автоматически переподключается
при разрыве соединения (exponential backoff 2→60 сек).
"""

import asyncio
import logging

from panoramisk import Manager

from app.core.config import settings

logger = logging.getLogger(__name__)


class AMIClient:
    def __init__(self):
        self._manager: Manager | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._stop = False
        # Флаг состояния соединения — читается из health endpoint
        self.is_connected = False
        # Список зарегистрированных обработчиков событий звонков
        self._call_handlers: list = []

    def _build_manager(self) -> Manager:
        """Создаёт новый экземпляр panoramisk.Manager с настройками из конфига."""
        return Manager(
            host=settings.ami_host,
            port=settings.ami_port,
            username=settings.ami_username,
            secret=settings.ami_secret,
            ping_delay=10,
        )

    def on_call_event(self, handler):
        """Регистрирует callback для событий Cdr/Newchannel/Hangup.
        Сохраняется в self._call_handlers и перепривязывается при каждом reconnect.
        Совместим с main.py: ami_client.on_call_event(process_call_event).
        """
        self._call_handlers.append(handler)
        return handler

    async def start(self):
        """Запускает фоновую задачу, которая держит AMI-коннект.
        Не блокирует — ошибки первого подключения не роняют приложение.
        Повторный вызов игнорируется, если задача уже запущена.
        """
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._stop = False
        self._reconnect_task = asyncio.create_task(self._run_forever())

    async def _run_forever(self):
        """Бесконечный цикл reconnect с exponential backoff (2→60 сек)."""
        backoff = 2
        while not self._stop:
            try:
                self._manager = self._build_manager()
                # Регистрируем обработчики на новый manager
                self._manager.register_event("Newchannel", self._handle_newchannel)
                self._manager.register_event("Hangup", self._handle_hangup)
                self._manager.register_event("Cdr", self._handle_cdr)

                logger.info(
                    "Подключение к Asterisk AMI %s:%s",
                    settings.ami_host,
                    settings.ami_port,
                )
                await self._manager.connect()
                self.is_connected = True
                backoff = 2  # сбрасываем backoff после успешного подключения
                logger.info("Подключено к Asterisk AMI")

                # Ждём разрыва: проверяем состояние транспорта каждые 5 секунд
                while not self._stop and self.is_connected:
                    await asyncio.sleep(5)
                    if not self._is_alive():
                        self.is_connected = False

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Ошибка в AMI-цикле reconnect")
                self.is_connected = False

            if self._stop:
                break

            logger.warning("AMI отключён, повтор через %s сек", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)  # cap = 60 сек

    def _is_alive(self) -> bool:
        """Проверяет, что panoramisk-транспорт ещё жив."""
        try:
            return bool(
                self._manager
                and self._manager.protocol
                and not self._manager.protocol.transport.is_closing()
            )
        except Exception:
            return False

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
                logger.exception("Ошибка в обработчике события звонка")

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
                logger.exception("Ошибка в обработчике события звонка")

    async def _handle_cdr(self, manager, message):
        """CDR — полная запись о звонке после завершения.
        Добавляем user_field из AMI-поля UserField (пробрасывает DID из dialplan).
        """
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
            # DID входящего звонка — пробрасывается через Set(CDR(userfield)=${EXTEN}) в dialplan
            "user_field": message.get("UserField"),
        }
        for handler in self._call_handlers:
            try:
                await handler(event_data)
            except Exception:
                logger.exception("Ошибка в обработчике события звонка")

    async def disconnect(self):
        """Останавливает reconnect-цикл и закрывает соединение с AMI."""
        self._stop = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._manager:
            try:
                self._manager.close()
            except Exception:
                pass
        self.is_connected = False
        logger.info("Отключено от Asterisk AMI")

    async def originate_call(self, number: str, extension: str, context: str = "from-internal"):
        """Инициирует исходящий звонок (для callback-виджета).
        Raises RuntimeError, если AMI не подключён — вызывающая сторона
        должна вернуть 503 пользователю.
        """
        if not self.is_connected or not self._manager:
            raise RuntimeError("AMI not connected")
        response = await self._manager.send_action({
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
