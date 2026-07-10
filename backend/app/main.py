import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import tracking, calls, projects, numbers, callback, auth, health as health_router
from app.api.v1 import amo_webhook
from app.core.config import settings
from app.services.ami_client import ami_client, run_did_refresh_loop
from app.services.webhook import webhook_sender
from app.services.pool_sync import sync_pool_from_db
from app.services import ami_journal
from app.workers.call_processor import process_call_event
from app.workers.number_cleanup import run_cleanup_loop
from app.workers.reconciliation import run_reconciliation_loop
from app.workers.amo_poll import run_amo_poll_loop

logging.basicConfig(level=logging.INFO if not settings.debug else logging.DEBUG)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: регистрируем обработчик событий и запускаем reconnect-цикл AMI
    ami_client.on_call_event(process_call_event)
    # start() не блокирует — спавнит фоновую задачу с reconnect-loop
    await ami_client.start()

    # Синхронизация пула номеров из БД в Redis
    try:
        await sync_pool_from_db()
    except Exception:
        logger.warning("Failed to sync number pool from DB — pool may be empty")

    # REPLAY: переобрабатываем зависшие AMI-события (защита от потери звонков
    # при рестарте/краше между приёмом Cdr и commit). Идемпотентно — дубли
    # звонков/лидов не создаются (дедуп по calls.uniqueid и AMO-дедуп).
    try:
        replayed = await ami_journal.replay_pending_events(process_call_event)
        if replayed:
            logger.info("AMI journal replay: reprocessed %d events", replayed)
    except Exception:
        logger.exception("AMI journal replay failed")

    # Запуск фонового worker для очистки просроченных сессий
    cleanup_task = asyncio.create_task(run_cleanup_loop())

    # Запуск reconciliation worker: восстанавливает атрибуцию потерянных звонков
    reconciliation_task = asyncio.create_task(run_reconciliation_loop())

    # Запуск AMO poll worker: страховочная синхронизация лидов каждые 10 минут
    amo_poll_task = asyncio.create_task(run_amo_poll_loop())

    # Запуск ретеншна журнала AMI-событий: чистит done-события старше 7 дней раз в час
    journal_cleanup_task = asyncio.create_task(ami_journal.run_journal_cleanup_loop())

    # Периодическое обновление кеша наших DID (новый активный номер станет
    # захватываемым без ожидания реконнекта AMI).
    did_refresh_task = asyncio.create_task(run_did_refresh_loop())

    yield

    # Shutdown: останавливаем reconnect-цикл и закрываем соединения
    cleanup_task.cancel()
    reconciliation_task.cancel()
    amo_poll_task.cancel()
    journal_cleanup_task.cancel()
    did_refresh_task.cancel()
    await ami_client.disconnect()
    await webhook_sender.close()


app = FastAPI(
    title="KuroTrack",
    description="Call tracking platform built on Asterisk/FreePBX",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://kt.aiplus.kz"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(tracking.router, prefix="/api/v1")
app.include_router(calls.router, prefix="/api/v1")
app.include_router(projects.router, prefix="/api/v1")
app.include_router(numbers.router, prefix="/api/v1")
app.include_router(callback.router, prefix="/api/v1")
# AMO CRM webhook — real-time обновление данных лида
app.include_router(amo_webhook.router, prefix="/api/v1")
# Health endpoint вынесен в отдельный роутер для чистоты
app.include_router(health_router.router, prefix="/api/v1")

import psutil as _psutil
_START_TIME = __import__('time').time()
_PROC = _psutil.Process(__import__('os').getpid())

@app.get("/health")
async def simple_health():
    return {
        "status": "ok",
        "rss_mb": _PROC.memory_info().rss // 1024 // 1024,
        "uptime_s": int(__import__('time').time() - _START_TIME),
    }
