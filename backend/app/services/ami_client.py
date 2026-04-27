"""Клиент для Asterisk Manager Interface (AMI).
Слушает события звонков в реальном времени, автоматически переподключается
при разрыве соединения (exponential backoff 2→60 сек).
"""

import asyncio
import logging

from panoramisk import Manager
from sqlalchemy import select

from app.core.config import settings
from app.core.database import async_session
from app.core.phone import normalize_phone
from app.core.redis import redis_client
from app.models.tracking_number import TrackingNumber

logger = logging.getLogger(__name__)

# Кеш наших DID (нормализованные phone_normalized). Заполняется при старте
# и при каждом успешном reconnect. Используется в _handle_newchannel для
# опознания "наш это входящий или нет" независимо от имени Channel/Context.
_our_dids: set[str] = set()


async def _reload_our_dids() -> None:
    """Перечитывает список активных DID из БД в кеш."""
    global _our_dids
    try:
        async with async_session() as db:
            rows = await db.execute(
                select(TrackingNumber.phone_normalized).where(
                    TrackingNumber.is_active.is_(True)
                )
            )
            _our_dids = {r[0] for r in rows.all() if r[0]}
        logger.info("Loaded %s our DIDs for inbound detection: %s", len(_our_dids), _our_dids)
    except Exception:
        logger.exception("Не удалось загрузить наши DID из БД")


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

                # Перечитываем список наших DID — это кеш для _handle_newchannel.
                # На каждом reconnect синхронизируемся на случай если номера добавили.
                await _reload_our_dids()

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
        """Новый канал — начало звонка.

        Если канал пришёл из транка (from-trunk/from-pstn или Channel начинается
        с SIP/trunk_/PJSIP/trunk_/Local/trunk_), захватываем DID из Exten и
        сохраняем в Redis с TTL 300 сек. Это нужно потому что входящие Inbound
        Routes идут напрямую в Queue, минуя kurotrack-inbound, поэтому
        CDR(userfield) не заполняется dialplan-ом.
        """
        context = message.get("Context", "")
        channel = message.get("Channel", "")
        uniqueid = message.get("Uniqueid", "")
        linkedid = message.get("Linkedid", "")
        exten = message.get("Exten", "")

        # Главный сигнал: Exten совпадает с одним из наших tracking-номеров.
        # Это работает независимо от имени Channel/Context — FreePBX может
        # называть транки по-разному (SIP/trunk_X, PJSIP/X, или вообще без
        # префикса trunk_), но Exten в первых Newchannel всегда равен DID.
        did_norm = normalize_phone(exten) if exten and not exten.startswith(("s", "h", "i")) else ""
        is_our_did = bool(did_norm and did_norm in _our_dids)

        # Старый эвристический триггер оставляем как fallback — вдруг наш DID
        # ещё не в кеше (добавили после старта), но канал явно из транка.
        is_inbound_trunk = (
            context in ("from-trunk", "from-pstn")
            or channel.startswith("SIP/trunk_")
            or channel.startswith("PJSIP/trunk_")
            or channel.startswith("Local/trunk_")
        )

        if (is_our_did or is_inbound_trunk) and did_norm:
            try:
                # Сохраняем DID по uniqueid и linkedid (TTL 5 минут)
                await redis_client.set(f"inbound_did:{uniqueid}", did_norm, ex=300)
                if linkedid and linkedid != uniqueid:
                    await redis_client.set(f"inbound_did:{linkedid}", did_norm, ex=300)
                logger.info(
                    "inbound DID captured: uniqueid=%s linkedid=%s did=%s channel=%s",
                    uniqueid, linkedid, did_norm, channel,
                )
            except Exception:
                logger.exception(
                    "Ошибка сохранения inbound DID в Redis: uniqueid=%s did=%s",
                    uniqueid, did_norm,
                )

        event_data = {
            "event": "new_call",
            "uniqueid": uniqueid,
            "channel": channel,
            "caller_id_num": message.get("CallerIDNum"),
            "caller_id_name": message.get("CallerIDName"),
            "exten": exten,
            "context": context,
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
            "uniqueid": message.get("UniqueID") or message.get("Uniqueid"),
            # linkedid нужен и для Redis-корреляции, и для дедупликации legs.
            # panoramisk отдаёт ключ в нескольких регистрах — пробуем все варианты.
            "linkedid": (
                message.get("LinkedID")
                or message.get("Linkedid")
                or message.get("linkedid")
            ),
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
