import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import tracking, calls, projects, numbers, callback, auth
from app.core.config import settings
from app.services.ami_client import ami_client
from app.services.webhook import webhook_sender
from app.services.pool_sync import sync_pool_from_db
from app.workers.call_processor import process_call_event
from app.workers.number_cleanup import run_cleanup_loop

logging.basicConfig(level=logging.INFO if not settings.debug else logging.DEBUG)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: подключение к Asterisk AMI
    ami_client.on_call_event(process_call_event)
    try:
        await ami_client.connect()
    except Exception:
        logger.warning(
            "Failed to connect to Asterisk AMI — call tracking will not work until AMI is available"
        )

    # Синхронизация пула номеров из БД в Redis
    try:
        await sync_pool_from_db()
    except Exception:
        logger.warning("Failed to sync number pool from DB — pool may be empty")

    # Запуск фонового worker для очистки просроченных сессий
    cleanup_task = asyncio.create_task(run_cleanup_loop())

    yield

    # Shutdown
    cleanup_task.cancel()
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
    allow_origins=["*"],  # TODO: ограничить доменами проектов в production
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


@app.get("/health")
async def health():
    return {"status": "ok", "service": "kurotrack"}
