# ARCH-P2-architecture — блок архитектурного долга KuroTrack

Автор: CTO (Opus). Исполнители: Sonnet-разработчики.
Репозиторий: `/Users/baigelenov/projects/kurotrack`, ветка `master` (базовый HEAD `8127793`).
Прод: `sshpass -p '...' ssh kuro-server`, репо `/home/alisher/kurotrack` (на master; HEAD прода `04d57f5` — П2.13+П2.15 уже в проде).

> ВАЖНО для исполнителей:
> - Комментарии в коде — **на русском**.
> - SQL в коде — **только параметризованный** (asyncpg через SQLAlchemy `text(...)` с bind-параметрами `:name`). f-string/конкатенация в SQL запрещены.
> - Не рефакторить лишнего. Каждый пункт — отдельная задача (см. JSON-блок в конце).
> - Все пути в этой спеке даны от корня репо `/Users/baigelenov/projects/kurotrack/`. **Источник истины — прод-репо `/home/alisher/kurotrack` (master, HEAD 04d57f5). Локальный worktree неполный — код читать НА СЕРВЕРЕ.**
> - Прод-факты (проверены при разведке 2026-07-02):
>   - Postgres: `127.0.0.1:5433`, БД `kurotrack`, user `kuro`. **`max_connections=100`** (проверено `SHOW max_connections` на 5433; сейчас реально занято ~19). Поднять НЕЛЬЗЯ (это docker restart контейнера Postgres — нет docker-прав).
>   - Redis: `127.0.0.1:6380/0`.
>   - Воркер: systemd **user**-юнит `~/.config/systemd/user/kurotrack-worker.service`, запускает `uvicorn app.main:app --host 127.0.0.1 --port 8102 --limit-max-requests 1000`, `EnvironmentFile=/home/alisher/kurotrack/backend/.env.worker`, `WorkingDirectory=/home/alisher/kurotrack/backend`, `venv` = `/home/alisher/kurotrack/backend/venv` (НЕ `.venv`!). `KURO_ROLE` в юните НЕ задан → роль `all`.
>   - nginx проксирует `/api/` → `127.0.0.1:8102`, `/healthz` → `127.0.0.1:8102/api/v1/health`. **Конфиг `/etc/nginx/conf.d/kurotrack.conf` — root-owned symlink; `nginx -t`/`reload` требуют sudo. НЕТ sudo, НЕТ docker-прав, админ (Умид) ничего делать не будет. nginx НЕ ТРОГАЕМ → порт 8102 для backend неизменен.**
>   - Управление: `systemctl --user ...` (не root). Reboot-persist через `loginctl enable-linger` уже настроен (юнит `WantedBy=default.target`).

---

## 1. Summary

Закрываем 3 пункта архитектурного долга (П2.13–П2.15) на воркере обработки звонков KuroTrack.

- **П2.13 (в проде)** — персистентный журнал AMI-событий в Postgres (таблица `ami_events`). Сырое событие сразу пишется в `ami_events` быстрым INSERT (status=`pending`), обрабатывается прежним хендлером, помечается `done`. При старте воркера — replay всех `pending`. Идемпотентность гарантирована дедупликацией по `uniqueid` (`ix_calls_uniqueid` UNIQUE).
- **П2.15 (в проде)** — AMO-polling: окно уменьшено с 720 ч до 4 ч, добавлено ограничение параллелизма (semaphore + пауза под rate-limit AMO). Webhook (`/api/v1/amo/webhook`) — основной канал real-time.
- **П2.14 (текущая волна, самый инвазивный)** — разделение единого процесса на роли через ENV-флаг `KURO_ROLE=api|worker|all` (дефолт `all` = полная обратная совместимость). `api` поднимает только HTTP-роуты (можно `--workers N`) и **остаётся на порту 8102** (nginx неприкосновенен); `worker` — AMI + фоновые воркеры + replay журнала, **уезжает на внутренний порт 8104**. Пошаговый бездаунтаймовый план миграции прода **без sudo, без docker, без правки nginx**, с откатом.

Результат: воркер переживает рестарт/краш без потери звонков (журнал + replay), AMO-polling укладывается в интервал и не бьётся об rate-limit, а нагрузку API можно масштабировать (`--workers`) отдельно от единственного процесса-обработчика звонков — при этом nginx не трогается и суммарный пул БД укладывается в лимит 100.

---

## 2. Acceptance Criteria

1. Таблица `ami_events` существует (миграция `0006`), поля: `id BIGSERIAL PK`, `event_type TEXT`, `uniqueid TEXT NULL`, `payload JSONB`, `status TEXT` (pending/done/failed), `received_at TIMESTAMPTZ`, `processed_at TIMESTAMPTZ NULL`, `attempts INT DEFAULT 0`, `last_error TEXT NULL`. Есть частичный индекс на `status IN ('pending','failed')` и индекс на `received_at`.
2. Каждое `cdr` / `new_call` / `hangup` событие СНАЧАЛА пишется в `ami_events` (INSERT `pending`), ТОЛЬКО ПОТОМ обрабатывается. После успешной обработки — `status='done'`, `processed_at=now()`. При исключении — `status='failed'`, `attempts+=1`, `last_error`.
3. При старте воркера `replay_pending_events()` берёт все `pending`/`failed` (attempts < 5) события, отсортированные по `received_at ASC`, и переобрабатывает их через тот же `process_call_event`. Replay НЕ создаёт дублей звонков (проверка `ix_calls_uniqueid`) и дублей AMO-лидов (существующая 3-уровневая дедупликация). **Replay запускается ТОЛЬКО в `is_worker()`** (иначе при API с `--workers N` было бы N параллельных replay).
4. Ретеншн: `done`-события старше 7 дней удаляются фоновым циклом раз в час.
5. `_LOOKBACK_HOURS` в `amo_poll.py` = 4 (было 720). `sync_recent_leads` ограничивает параллелизм семафором на 5 одновременных `sync_lead` и держит паузу так, чтобы не превышать ~5 req/s к AMO.
6. Webhook `/api/v1/amo/webhook` не менялся по контракту и остаётся основным каналом (проверяется существующими вызовами в коде).
7. `KURO_ROLE` читается из ENV (дефолт `all`). При `KURO_ROLE=api` lifespan НЕ поднимает `ami_client`, `run_cleanup_loop`, `run_reconciliation_loop`, `run_amo_poll_loop`, `replay_pending_events`, `run_journal_cleanup_loop`. При `KURO_ROLE=worker` lifespan поднимает всё перечисленное; роуты монтируются только `/health` и `/api/v1/health`. При `KURO_ROLE=all` — как сейчас (всё + все роуты).
8. `GET /health` (отдаёт поле `role`) и `GET /api/v1/health` работают во ВСЕХ трёх ролях.
9. **Раздельный пул БД по роли в `database.py`:** `worker` → pool_size=25 + max_overflow=15 (до 40); `api` → 15 + 15 (до 30 на uvicorn-воркер); `all` → 30 + 40 (до 70, как в монолите). Суммарный потолок api+worker ≤ 70 при `--workers 1`, ≤ 100 при `--workers 2` (см. R1). `max_connections` НЕ меняется.
10. **Два systemd user-юнита готовы:** `kurotrack-api.service` (порт **8102**, `KURO_ROLE=api`, `--workers 2`) и обновлённый `kurotrack-worker.service` (порт **8104**, `KURO_ROLE=worker`). **nginx НЕ меняется** (`/api/` и `/healthz` остаются на 8102 — теперь их обслуживает API-процесс).
11. `scripts/smoke_test.sh` обновлён под split: worker health на 8104, api/DNI на 8102, public API через nginx (kt.aiplus.kz) — без изменений. `bash scripts/smoke_test.sh` проходит (exit 0, 0 FAIL) после миграции.
12. Unit-тесты: журнал (INSERT pending → done), replay идемпотентен (не плодит звонки), ретеншн-запрос, `sync_recent_leads` не превышает лимит параллелизма, role helpers, роль→пул. Все проходят.

---

## 3. Files to Create

| Path | Purpose | Key Functions / содержимое |
|------|---------|----------------------------|
| `backend/app/models/ami_event.py` | ORM-модель журнала событий | class `AmiEvent(Base)` — маппинг на таблицу `ami_events` |
| `backend/migrations/versions/0006_ami_events.py` | Миграция таблицы журнала | `upgrade()` / `downgrade()`; revision `0006`, down_revision `0005` |
| `backend/app/services/ami_journal.py` | Сервис журнала (запись/replay/ретеншн) | `async def record_event(event: dict) -> int`, `async def mark_done(event_id: int) -> None`, `async def mark_failed(event_id: int, error: str) -> None`, `async def replay_pending_events(handler) -> int`, `async def cleanup_old_events(retention_days: int = 7) -> int`, `async def run_journal_cleanup_loop() -> None` |
| `backend/app/core/role.py` | Определение роли процесса | `KURO_ROLE: str` (константа из ENV), helpers `is_worker() -> bool`, `is_api() -> bool` |
| `infra/systemd/kurotrack-api.service` | systemd user-юнит для API-роли (порт 8102) | текст юнита (см. §П2.14) |
| `infra/systemd/kurotrack-worker.service` | Эталонный текст worker-юнита (`KURO_ROLE=worker`, порт 8104) | текст юнита (см. §П2.14) |
| `backend/tests/test_ami_journal.py` | Unit-тесты журнала и replay | тесты см. §9 |

> П2.13 (журнал) и П2.15 (amo-poll) — **уже в проде** (HEAD 04d57f5). Секции §3-§5, §8-§9 по ним оставлены для полноты/истории. Активная работа этой волны — **П2.14** (§4 database.py + main.py, §П2.14). Если файлы П2.13/П2.15 уже существуют на сервере в нужном виде — сверить, не переписывать.

### Точные сигнатуры (`backend/app/services/ami_journal.py`)

```python
"""Персистентный журнал AMI-событий (защита от потери звонков).

Каждое сырое событие звонка сначала пишется в таблицу ami_events (status=pending),
затем обрабатывается прежним хендлером process_call_event. Если процесс упал/рестартовал
между приёмом Cdr и commit — событие остаётся pending и будет переобработано при старте
(replay_pending_events). Идемпотентность обеспечена дедупликацией по calls.uniqueid.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from sqlalchemy import text

from app.core.database import async_session

logger = logging.getLogger(__name__)

# Максимум попыток обработки одного события до пометки окончательно failed.
_MAX_ATTEMPTS = 5
# Интервал цикла ретеншна (секунды).
_CLEANUP_INTERVAL_SEC = 3600
# Сколько дней хранить done-события.
_RETENTION_DAYS = 7


async def record_event(event: dict) -> int | None:
    """Пишет сырое событие в журнал (status=pending) и возвращает его id.

    Быстрый одиночный INSERT, отдельная короткоживущая сессия. При ошибке БД
    логирует и возвращает None — обработка события всё равно продолжится
    (журнал — это страховка, а не блокер основного пути).
    """


async def mark_done(event_id: int) -> None:
    """Помечает событие обработанным: status='done', processed_at=now()."""


async def mark_failed(event_id: int, error: str) -> None:
    """Помечает событие проваленным: status='failed', attempts+=1, last_error=error."""


async def replay_pending_events(
    handler: Callable[[dict], Awaitable[None]],
) -> int:
    """Переобрабатывает все зависшие события при старте воркера.

    Берёт события со status IN ('pending','failed') и attempts < _MAX_ATTEMPTS,
    сортирует по received_at ASC, для каждого вызывает handler(payload).
    Успех → mark_done, исключение → mark_failed. Возвращает число успешно
    переобработанных событий. Дубли звонков/лидов не создаются (дедуп по uniqueid).
    """


async def cleanup_old_events(retention_days: int = _RETENTION_DAYS) -> int:
    """Удаляет done-события старше retention_days. Возвращает число удалённых строк."""


async def run_journal_cleanup_loop() -> None:
    """Бесконечный фоновый цикл ретеншна журнала (раз в _CLEANUP_INTERVAL_SEC)."""
```

### `backend/app/core/role.py`

```python
"""Роль процесса KuroTrack: api | worker | all.

KURO_ROLE читается из ENV напрямую (os.environ), а НЕ через pydantic Settings,
потому что роль влияет на lifespan и на выбор пула БД (database.py) ДО/во время
инициализации FastAPI, и импортируется в database.py — тащить туда весь Settings
не нужно и создаёт циклы импорта.

- all    — (дефолт) поднимает и роуты, и AMI + все воркеры. Обратная совместимость.
- api    — только HTTP-роуты (можно uvicorn --workers N). НЕ поднимает AMI/воркеры.
- worker — AMI + фоновые воркеры + replay журнала. Роуты: только health.
"""

import os

KURO_ROLE: str = os.environ.get("KURO_ROLE", "all").strip().lower()

# Защита от опечаток: неизвестное значение трактуем как all (безопасный дефолт).
if KURO_ROLE not in ("api", "worker", "all"):
    KURO_ROLE = "all"


def is_worker() -> bool:
    """True для ролей worker и all (нужно поднимать AMI + воркеры + replay)."""
    return KURO_ROLE in ("worker", "all")


def is_api() -> bool:
    """True для ролей api и all (нужно монтировать все бизнес-роуты)."""
    return KURO_ROLE in ("api", "all")
```

### `backend/app/models/ami_event.py`

```python
"""ORM-модель журнала AMI-событий. Таблица создаётся миграцией 0006."""

from datetime import datetime

from sqlalchemy import BigInteger, Integer, String, Text, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AmiEvent(Base):
    """Сырое AMI-событие звонка. pending → done | failed."""

    __tablename__ = "ami_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # cdr | new_call | hangup (значения из process_call_event)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # uniqueid звонка — для дебага и корреляции (может быть None у некоторых событий)
    uniqueid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Сырой словарь события как пришёл в process_call_event
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # pending | done | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
```

> ВАЖНО: добавь импорт `AmiEvent` в `backend/app/models/__init__.py`, чтобы модель регистрировалась в `Base.metadata`. См. §4.

---

## 4. Files to Modify

| Path | Что меняется | Строки (примерно) |
|------|--------------|-------------------|
| `backend/app/services/ami_client.py` | (П2.13) helper `_dispatch_with_journal`, замена циклов `for handler` в newchannel/hangup/cdr | L204-208, L219-223, L257-261 |
| `backend/app/workers/call_processor.py` | (П2.13) НЕ меняется — journal ведёт ami_client, `process_call_event` уже идемпотентен | — |
| `backend/app/main.py` | (П2.14) Условный lifespan по `KURO_ROLE`; условное монтирование роутов; replay+cleanup ТОЛЬКО в worker-роли; поле `role` в `/health` | весь блок импортов + lifespan + монтирование |
| `backend/app/core/database.py` | **(П2.14) Раздельный пул БД по `KURO_ROLE`** (worker 25+15 / api 15+15 / all 30+40) | блок `create_async_engine` (L1-22 на сервере) |
| `backend/app/models/__init__.py` | (П2.13) Добавить `AmiEvent` в импорты и `__all__` | L1-7 |
| `backend/app/workers/amo_poll.py` | (П2.15) `_LOOKBACK_HOURS = 4` | L21 |
| `backend/app/services/amo_sync.py` | (П2.15) `sync_recent_leads`: семафор + rate-limit пауза | L298-332 |
| `scripts/smoke_test.sh` | **(П2.14) worker health → 8104, api/DNI → 8102** (см. §П2.14 «Обновление смоука») | L47, секции B/E |
| `infra/nginx/kurotrack.conf` | **НЕ МЕНЯЕТСЯ** (изменение относительно старой спеки — nginx неприкосновенен) | — |

### Правка `main.py` (П2.14) — условный lifespan + монтирование по роли

На проде (HEAD 04d57f5) `main.py` уже запускает replay/cleanup, но БЕЗ гейтинга по роли. Нужно обернуть всё в `is_worker()`/`is_api()`. Полностью заменяемый вид (импорты + lifespan + монтирование):

```python
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import tracking, calls, projects, numbers, callback, auth, health as health_router
from app.api.v1 import amo_webhook
from app.core.config import settings
from app.core.role import KURO_ROLE, is_api, is_worker
from app.services.ami_client import ami_client
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
    logger.info("KuroTrack starting with KURO_ROLE=%s", KURO_ROLE)
    background_tasks: list[asyncio.Task] = []

    # --- worker-роль (worker/all): AMI + фоновые воркеры + replay журнала ---
    # КРИТИЧНО: весь этот блок ТОЛЬКО в is_worker(). Если бы replay запускался в api
    # с uvicorn --workers N — было бы N параллельных replay одного журнала.
    if is_worker():
        # Регистрируем обработчик и запускаем reconnect-цикл AMI (не блокирует старт)
        ami_client.on_call_event(process_call_event)
        await ami_client.start()

        # Синхронизация пула номеров из БД в Redis
        try:
            await sync_pool_from_db()
        except Exception:
            logger.warning("Failed to sync number pool from DB — pool may be empty")

        # REPLAY: переобрабатываем зависшие AMI-события (защита от потери звонков).
        # Идемпотентно — дубли звонков/лидов не создаются (дедуп по calls.uniqueid).
        try:
            replayed = await ami_journal.replay_pending_events(process_call_event)
            if replayed:
                logger.info("AMI journal replay: reprocessed %d events", replayed)
        except Exception:
            logger.exception("AMI journal replay failed")

        # Фоновые воркеры — только в worker-роли
        background_tasks.append(asyncio.create_task(run_cleanup_loop()))
        background_tasks.append(asyncio.create_task(run_reconciliation_loop()))
        background_tasks.append(asyncio.create_task(run_amo_poll_loop()))
        background_tasks.append(asyncio.create_task(ami_journal.run_journal_cleanup_loop()))

    yield

    # Shutdown
    for task in background_tasks:
        task.cancel()
    if is_worker():
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

# Бизнес-роуты монтируем только в api/all ролях. В worker-роли — только health.
if is_api():
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(tracking.router, prefix="/api/v1")
    app.include_router(calls.router, prefix="/api/v1")
    app.include_router(projects.router, prefix="/api/v1")
    app.include_router(numbers.router, prefix="/api/v1")
    app.include_router(callback.router, prefix="/api/v1")
    app.include_router(amo_webhook.router, prefix="/api/v1")

# Health монтируется ВСЕГДА (нужен смоуку и nginx /healthz в любой роли).
app.include_router(health_router.router, prefix="/api/v1")

import psutil as _psutil
_START_TIME = __import__('time').time()
_PROC = _psutil.Process(__import__('os').getpid())

@app.get("/health")
async def simple_health():
    return {
        "status": "ok",
        "role": KURO_ROLE,
        "rss_mb": _PROC.memory_info().rss // 1024 // 1024,
        "uptime_s": int(__import__('time').time() - _START_TIME),
    }
```

> **ВНИМАНИЕ по callback-виджету (регрессия в split-режиме):** `callback.router` использует `ami_client.originate_call`. В роли `api` роут смонтирован (он на порту 8102, куда идёт nginx), но `ami_client` НЕ подключён → `originate_call` бросит `RuntimeError("AMI not connected")` → 503. **Раньше (монолит на 8102) callback работал, т.к. был AMI.** Теперь callback обслуживается API-процессом без AMI. Это **известное ограничение**, задокументировано как R5. Callback-трафик (виджет обратного звонка) околонулевой; приём входящих звонков и дашборд-чтение критичнее. Проксирование callback на worker потребовало бы правки nginx (sudo) → out of scope.

### Правка `database.py` (П2.14) — раздельный пул БД по роли

**Это новое относительно прошлой спеки.** Один `engine`, но параметры пула зависят от `KURO_ROLE`. Полностью заменяемый блок `create_async_engine` (текущие L1-22 на сервере). Готово к копипасту:

```python
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings
from app.core.role import KURO_ROLE


def _pool_params(role: str) -> tuple[int, int]:
    """Возвращает (pool_size, max_overflow) по роли процесса.

    Общий Postgres, max_connections=100 (прод, 5433). Поднять НЕЛЬЗЯ (нет docker/root),
    поэтому суммарный пул api+worker держим низким, оставляя запас на служебные psql/смоук.
      worker — тяжёлый по БД: journal INSERT на каждое AMI-событие, CDR, replay при старте,
               reconciliation-loop, amo-poll → больше пул.
      api    — читающий дашборд (списки calls/projects), короткие транзакции; пул делится
               между uvicorn --workers (каждый форк создаёт свой engine с этими цифрами).
      all    — монолит (обратная совместимость): полный пул 30+40=70, как было до разделения.
    """
    if role == "worker":
        return 25, 15   # итого до 40
    if role == "api":
        return 15, 15   # итого до 30 (НА КАЖДЫЙ uvicorn-воркер)
    return 30, 40       # all: итого до 70 (как в монолите)


_pool_size, _max_overflow = _pool_params(KURO_ROLE)

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_size=_pool_size,
    max_overflow=_max_overflow,
    pool_timeout=30,         # даём время дождаться свободного слота на пике
    pool_pre_ping=True,
    pool_recycle=1800,
    connect_args={
        "timeout": 15,           # таймаут на установку TCP-соединения (asyncpg)
        "command_timeout": 300,  # таймаут на выполнение SQL-команды (asyncpg)
    },
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    # rollback в finally завершает любую открытую транзакцию (idle in transaction leak).
    # Если endpoint сам сделал commit — rollback станет no-op. Для read-only это снимает
    # зависшие коннекты которые иначе забивают pool и приводят к 504.
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.rollback()
```

> **Про `--workers 2` и общий лимит:** SQLAlchemy `engine` создаётся отдельно в КАЖДОМ uvicorn-воркере (форки процесса). api с `--workers 2` держит до `2 × 30 = 60` коннектов в пике. worker держит до 40. Суммарно worker(40)+api(60)=**100** = ровно лимит → теоретический риск `TooManyConnectionsError`. На практике 2 read-only воркера дашборда почти никогда не выберут пул полностью (короткие транзакции + `get_db` rollback чистит idle). **Безопасный рычаг без правки кода — `--workers 1` в api-юните** (тогда api=30, суммарно 70 гарантированно). Мониторить `pg_stat_activity` на выкате (см. R1).

### Правка `models/__init__.py` (П2.13)

```python
from app.models.project import Project
from app.models.tracking_number import TrackingNumber
from app.models.call import Call
from app.models.session import VisitorSession
from app.models.user import User
from app.models.ami_event import AmiEvent

__all__ = ["Project", "TrackingNumber", "Call", "VisitorSession", "User", "AmiEvent"]
```

> П2.13 (ami_client интеграция, journal-сервис) и П2.15 (amo_poll/amo_sync) — уже в проде. Их подробные правки (ниже, сокращённо) применять НЕ нужно, если код уже на месте — сверить факт на сервере.

### Интеграция в `ami_client.py` (П2.13 — уже в проде, для истории)

Helper `_dispatch_with_journal` в классе `AMIClient`: `record_event(event_data)` → прогон всех хендлеров → `mark_done` при успехе / `mark_failed("handler raised")` при исключении; при `event_id is None` (сбой БД) обработка идёт без страховки. Циклы `for handler in self._call_handlers:` в `_handle_newchannel`/`_handle_hangup`/`_handle_cdr` заменены на `await self._dispatch_with_journal(event_data)`.

### `amo_poll.py` / `amo_sync.py` (П2.15 — уже в проде, для истории)

`_LOOKBACK_HOURS = 4`; в `sync_recent_leads` — семафор `_POLL_CONCURRENCY = 5` + пауза `_POLL_PAUSE_SEC = 0.2` + дедуп `dict.fromkeys`, `asyncio.gather` по слотам.

---

## 5. Database Changes

### Миграция `backend/migrations/versions/0006_ami_events.py` (уже накатана в проде)

```python
"""ami events journal

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-02
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS ami_events (
            id           BIGSERIAL PRIMARY KEY,
            event_type   VARCHAR(32)  NOT NULL,
            uniqueid     VARCHAR(64),
            payload      JSONB        NOT NULL,
            status       VARCHAR(16)  NOT NULL DEFAULT 'pending',
            received_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
            processed_at TIMESTAMPTZ,
            attempts     INTEGER      NOT NULL DEFAULT 0,
            last_error   TEXT
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_ami_events_pending
        ON ami_events (received_at)
        WHERE status IN ('pending', 'failed')
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_ami_events_status_received
        ON ami_events (status, received_at)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_ami_events_uniqueid
        ON ami_events (uniqueid)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ami_events")
```

### SQL внутри `ami_journal.py` (параметризованный, через `text()`) — уже в проде

```sql
-- record_event
INSERT INTO ami_events (event_type, uniqueid, payload, status, received_at)
VALUES (:event_type, :uniqueid, CAST(:payload AS JSONB), 'pending', now())
RETURNING id;

-- mark_done
UPDATE ami_events SET status='done', processed_at=now() WHERE id = :id;

-- mark_failed
UPDATE ami_events SET status='failed', attempts = attempts + 1, last_error = :error WHERE id = :id;

-- replay: выборка
SELECT id, payload FROM ami_events
WHERE status IN ('pending','failed') AND attempts < :max_attempts
ORDER BY received_at ASC;

-- cleanup_old_events
DELETE FROM ami_events
WHERE status='done' AND processed_at < now() - make_interval(days => :days);
```

**П2.14 миграций БД не добавляет** — только код (`role.py`, `main.py`, `database.py`) и systemd/смоук.

---

## 6. API Contract

Новых HTTP-эндпоинтов НЕТ. Меняется только `GET /health` (добавлено поле `role`):

```python
# GET /health (не через роутер, определён в main.py)
{
    "status": "ok",       # str, всегда "ok" если процесс жив
    "role": "worker",     # str, KURO_ROLE: api | worker | all  (НОВОЕ поле)
    "rss_mb": 210,        # int, потребление RSS в МБ
    "uptime_s": 3600      # int, аптайм процесса в секундах
}
```

`GET /api/v1/health` (роутер `health.py`) — контракт БЕЗ изменений (`HealthResponse`: status, service, ami_connected, db_ok, redis_ok). В роли `api` поле `ami_connected` = `false` (AMI не поднят) — ожидаемо: смоук проверяет `ami_connected` только у воркера через `127.0.0.1:8104`.

Ошибки: без изменений. `/health` никогда не 5xx.

---

## 7. Frontend Contract

Изменений во фронтенде НЕТ. Дашборд обращается к тем же `/api/v1/*` эндпоинтам через nginx. **nginx не меняется** — `/api/` по-прежнему проксирует на `127.0.0.1:8102`; после миграции на 8102 отвечает API-процесс вместо монолита. Для фронтенда полностью прозрачно, URL не меняется. TypeScript-типы не затрагиваются.

---

## 8. Edge Cases & Error Handling

### `ami_journal.record_event`
- **БД недоступна при INSERT** → лог `exception`, возврат `None`. Обработка события продолжается БЕЗ журнала. Не бросаем.
- **payload не сериализуется** → `json.dumps(..., default=str)`; при остаточной ошибке — лог + `None`.

### `ami_journal.replay_pending_events`
- **Дубль звонка при replay** → `_handle_cdr` находит существующий `Call` по `uniqueid` ИЛИ ловит `IntegrityError` → `return`, дубль не создаётся. Событие `done`.
- **handler бросил при replay** → `mark_failed`, `attempts+=1`; при следующем старте попробуем снова пока `attempts < 5`.
- **Пустой журнал** → возврат 0.
- **Огромный журнал** → replay последовательный по `received_at ASC` в lifespan ДО `yield`; логировать прогресс каждые 100 событий (out of scope — батчинг).

### `main.py` lifespan по роли
- **`KURO_ROLE` не задан** → `all` (обратная совместимость).
- **`KURO_ROLE` = мусор** → `role.py` нормализует в `all`.
- **api-роль, запрос `/api/v1/calls`** → работает (роут смонтирован, БД доступна).
- **api-роль, callback originate** → 503 `AMI not connected` (R5, известное ограничение).
- **worker-роль, запрос `/api/v1/calls`** → 404 (роут не смонтирован). Ожидаемо.
- **worker-роль, `/health` и `/api/v1/health`** → 200 (health монтируется всегда).

### `database.py` роль→пул
- **`KURO_ROLE=worker`** → пул 25+15=40.
- **`KURO_ROLE=api`** → пул 15+15=30 на каждый uvicorn-воркер.
- **`KURO_ROLE=all` / мусор** → пул 30+40=70 (монолит).
- **Пул исчерпан на пике** → `pool_timeout=30` даёт ждать слот; при переполнении смоук ловит `db connections >= 80` (см. R1).

---

## 9. Test Scenarios

Файл: `backend/tests/test_ami_journal.py`. Async-тесты требуют `pytest-asyncio`. Добавь в `backend/pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

Тесты мокают `async_session` (не поднимают реальную БД — смоук проверяет живую). Мок через monkeypatch на `app.services.ami_journal.async_session`.

| Test | Input | Expected | Type |
|------|-------|----------|------|
| `test_record_event_returns_id` | event dict, мок сессии возвращает id=7 | вернул 7, выполнен INSERT с pending | unit |
| `test_record_event_db_error_returns_none` | мок сессии бросает Exception | вернул None, не бросил | unit |
| `test_dispatch_journals_and_marks_done` | handler успешен | record_event → все хендлеры → mark_done | unit |
| `test_dispatch_marks_failed_on_handler_error` | handler бросает | mark_failed вызван, mark_done НЕ вызван | unit |
| `test_replay_calls_handler_per_event` | 3 pending события | handler ×3 с payload, mark_done ×3, возврат 3 | unit |
| `test_replay_idempotent_no_duplicate` | replay события с дублем uniqueid | handler не бросает, mark_done, звонок НЕ создаётся повторно | unit |
| `test_replay_failed_marks_failed` | handler бросает на 1 событии | mark_failed для него, остальные done | unit |
| `test_cleanup_builds_correct_delete` | retention_days=7 | DELETE с `status='done'` и параметром days=7 | unit |
| `test_sync_recent_leads_dedups_and_limits` | 12 lead_ids c дублями | sync_lead по числу уникальных, параллелизм ≤ 5 | unit |
| `test_role_default_all` | ENV без KURO_ROLE | `KURO_ROLE=="all"`, is_api и is_worker True | unit |
| `test_role_api` | KURO_ROLE=api | is_api True, is_worker False | unit |
| `test_role_worker` | KURO_ROLE=worker | is_api False, is_worker True | unit |
| `test_role_garbage_defaults_all` | KURO_ROLE=xxx | нормализуется в all | unit |
| `test_pool_params_by_role` | role=worker/api/all | (25,15)/(15,15)/(30,40) соответственно | unit |

> Тесты роли/пула: `role.py` и `database.py` читают ENV на импорте. Для теста роли — патч модульной переменной `app.core.role.KURO_ROLE` + вызов `is_api()/is_worker()`. Для теста пула — вызывать чистую функцию `database._pool_params(role)` напрямую (не reload модуля с engine).

**Команда запуска** (из `backend/`): `python -m pytest -q`. На проде смоук гоняет `venv/bin/python -m pytest` (секция A).

---

## П2.14 — разделение API/worker БЕЗ sudo, БЕЗ docker, БЕЗ правки nginx (ПЕРЕРАБОТАНО)

> **ЭТОТ РАЗДЕЛ ПОЛНОСТЬЮ ПЕРЕПИСАН 2026-07-02 под новые жёсткие ограничения прода.**
> **Что изменилось относительно предыдущей версии спеки и ПОЧЕМУ:**
> | Было (старая спека) | Стало (сейчас) | Почему |
> |---------------------|----------------|--------|
> | API уезжает на порт **8103**, nginx `/api/` переключается 8102→8103 | **API остаётся на 8102**; на другой порт (**8104, только `/health`**) уезжает **WORKER** | nginx-конфиг — root-owned symlink в `/etc/nginx`, `nginx -t && systemctl reload nginx` требует **sudo**. Админ (Умид) ничего делать не будет. Порт nginx→backend (8102) трогать НЕЛЬЗЯ. Инвертируем: worker уходит, API занимает 8102. |
> | `max_connections=200` (docker-compose поднимает), пулы 70+70=140 | **`max_connections=100`** (факт прода, `SHOW max_connections` на 5433), суммарный пул **≤ 70** (api 30 + worker 40) | Поднять `max_connections` нельзя — это `docker restart` контейнера Postgres = **нет docker-прав**. Живём в лимите 100 (реально занято ~19). |
> | Один `engine` с фикс. пулом 30+40 на оба процесса | Пул **зависит от роли** в `database.py` (api 15+15=30; worker 25+15=40; all 30+40=70) | Оба процесса делят один Postgres со 100 коннектами. Раздельные пулы по роли держат суммарный потолок в лимите. |
> | nginx `/healthz` остаётся на воркере 8102 | `/healthz` **не трогаем** — он на 8102, где теперь **API** (у API есть `/api/v1/health`) | nginx не редактируем. `/healthz`→`8102/api/v1/health` работает, отвечает API-процесс. AMI-состояние воркера смоук проверяет напрямую по `127.0.0.1:8104/health`. |
> | `replay_pending_events` в lifespan безусловно | replay **строго `is_worker()`-гейтинг** (§4 main.py) | Приёмка журнала (П2.13): API с `--workers N` вызвал бы N параллельных replay. Replay обязан идти ТОЛЬКО в role=worker. |

### Проблема
Один процесс (`KURO_ROLE` не задан = `all`, порт 8102) держит и HTTP-API дашборда, и AMI-обработку звонков. Нельзя масштабировать API (`--workers`) и нельзя рестартовать API-часть, не роняя приём звонков.

### Решение (при ограничениях: нет sudo, нет docker, nginx неприкосновенен)
Роль через ENV `KURO_ROLE`. Прод разводим на два `systemctl --user`-юнита:
- **`kurotrack-api.service`** — `KURO_ROLE=api`, порт **8102** (тот, куда nginx уже проксирует), `--workers 2`. Только HTTP-роуты дашборда. AMI/воркеры/replay **НЕ** поднимает. Пул БД: 15+15=30 на воркер.
- **`kurotrack-worker.service`** — `KURO_ROLE=worker`, порт **8104** (внутренний, только для `/health`-пробы), без `--workers`. Держит AMI + фоновые воркеры + replay журнала. Роуты монтирует только health. Пул БД: 25+15=40.

nginx **НЕ трогаем**: `/api/` и `/healthz` как были на `127.0.0.1:8102`. Теперь на 8102 отвечает API-процесс — для nginx и фронтенда прозрачно.

Суммарный пул БД: **api ≤ 30/воркер + worker ≤ 40**. При `--workers 1`: 30+40=70 ≤ 100 гарантированно. При `--workers 2`: до 60+40=100 в пике (см. R1). Смоук-порог `db connections < 80`.

### systemd-юниты (текст готов к копипасту, оба в репо `infra/systemd/`)

**`infra/systemd/kurotrack-worker.service`** (порт 8104, `KURO_ROLE=worker`):

```ini
[Unit]
Description=KuroTrack AMI Worker (uvicorn, role=worker, port 8104)
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/alisher/kurotrack/backend
EnvironmentFile=/home/alisher/kurotrack/backend/.env.worker
Environment=KURO_ROLE=worker
LimitNOFILE=65536
# Safety net: soft-лимит поджимает кэши, hard убивает+рестарт (Restart=always)
MemoryHigh=500M
MemoryMax=700M
ExecStart=/home/alisher/kurotrack/backend/venv/bin/uvicorn app.main:app \
    --host 127.0.0.1 --port 8104 \
    --limit-max-requests 1000
StandardOutput=append:/home/alisher/kurotrack/logs/worker.log
StandardError=append:/home/alisher/kurotrack/logs/worker.log
SyslogIdentifier=kurotrack-worker
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

**`infra/systemd/kurotrack-api.service`** (порт 8102 — тот, куда проксирует nginx; `KURO_ROLE=api`):

```ini
[Unit]
Description=KuroTrack API (uvicorn, role=api, port 8102)
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/alisher/kurotrack/backend
EnvironmentFile=/home/alisher/kurotrack/backend/.env.worker
Environment=KURO_ROLE=api
LimitNOFILE=65536
MemoryHigh=400M
MemoryMax=600M
ExecStart=/home/alisher/kurotrack/backend/venv/bin/uvicorn app.main:app \
    --host 127.0.0.1 --port 8102 \
    --workers 2 --limit-max-requests 1000
StandardOutput=append:/home/alisher/kurotrack/logs/api.log
StandardError=append:/home/alisher/kurotrack/logs/api.log
SyslogIdentifier=kurotrack-api
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

> Оба используют один `.env.worker` (DATABASE_URL/REDIS_URL/AMI/AMO — все ключи с префиксом `KURO_`). `KURO_ROLE` задаётся через `Environment=` в юните и переопределяет любое значение из файла. **Раздельный пул БД обеспечивается кодом `database.py` (по `KURO_ROLE`)** — каждый процесс читает свою роль на импорте и берёт свой размер пула.
>
> **`SyslogIdentifier=kurotrack-worker` в worker-юните сохранён намеренно** — смоук (секция B) делает `systemctl --user is-active kurotrack-worker.service`, имя юнита не меняется. Оба процесса пишут в разные логи (`worker.log` / `api.log`).

### nginx — НЕ ТРОГАЕМ (ключевое отличие от старой спеки)

`infra/nginx/kurotrack.conf` **остаётся без изменений**. Проверено на сервере:
- `location /api/` → `proxy_pass http://127.0.0.1:8102;` — остаётся 8102, теперь отвечает API-процесс.
- `location = /healthz` → `proxy_pass http://127.0.0.1:8102/api/v1/health;` — остаётся 8102, отвечает API-процесс (у него смонтирован `/api/v1/health`).

**Не редактировать `infra/nginx/kurotrack.conf`, не запускать `nginx -t`, не делать `systemctl reload nginx`** — всё требует root/sudo, которого нет, и админ ничего делать не будет. Файл в `/etc/nginx/conf.d/kurotrack.conf` — root-owned symlink, править его нельзя.

### Пошаговый план выката (БЕЗ даунтайма приёма звонков, БЕЗ sudo, БЕЗ nginx)

> Все команды на проде под `alisher`, БЕЗ sudo. Управление — `systemctl --user`. Linger включён — юниты переживают выход из ssh и reboot.
> Текущее состояние прода: один юнит `kurotrack-worker.service` на порту **8102**, `KURO_ROLE` не задан = `all`. Именно к нему сейчас проксирует nginx.

**Шаг 0 — подготовка (без влияния на прод).**
- Смёржить код П2.14 (role.py + main.py role-gating + database.py role-pool + оба юнита + смоук) в master; на проде `cd /home/alisher/kurotrack && git pull`.
- Проверить факт лимита БД: `PGPASSWORD=kuro psql -h 127.0.0.1 -p 5433 -U kuro -d kurotrack -c "SHOW max_connections"` → **100**. `... -c "SELECT count(*) FROM pg_stat_activity WHERE datname='kurotrack'"` → ~19.
- **БЭКАП живого юнита ДО перезаписи (обязательно для отката):** `cp ~/.config/systemd/user/kurotrack-worker.service ~/kurotrack-worker.service.bak-$(date +%F)`.
- Скопировать оба новых юнита в `~/.config/systemd/user/`: `cp infra/systemd/kurotrack-worker.service infra/systemd/kurotrack-api.service ~/.config/systemd/user/`.
- `systemctl --user daemon-reload`.
- ВАЖНО: `cp` перезаписал файл `kurotrack-worker.service`, но живой процесс продолжает крутиться на СТАРОЙ конфигурации (8102, роль all) до рестарта. Проверка: `systemctl --user show kurotrack-worker -p ExecStart` — всё ещё показывает `--port 8102` (живой), а `systemctl --user cat kurotrack-worker` — уже новый текст (8104). Расхождение — нормально до Шага 2.
- Откат шага 0: восстановить `.bak`, `daemon-reload`. Влияния на прод нет (ничего не рестартили).

**Шаг 1 — зафиксировать baseline (без разделения).**
- Пока не разделяем. Провалидировать новый код монолитом лучше на dev/локально ДО выката: с пустым `KURO_ROLE` → роль `all`, `is_api()==is_worker()==True`, пул 30+40 — всё поднимается как раньше.
- На проде Шаг 1 = прогон текущего (ещё не обновлённого) `bash scripts/smoke_test.sh` (он бьёт 8102) → 0 FAIL, фиксируем эталон ДО переключения. Живой монолит на 8102 не трогаем.

**Шаг 2 — переключение: стоп монолита на 8102 → старт API на 8102 (короткое окно 502 дашборда).**
Порядок команд критичен, выполняется одной пачкой БЫСТРО:
```
systemctl --user stop kurotrack-worker.service     # стоп монолита; 8102 освобождён, nginx→502 на /api/ (несколько сек)
systemctl --user start kurotrack-api.service       # API поднимается на 8102 (role=api, --workers 2), ~2-4с → nginx снова 200
```
- **Почему это НЕ потеря звонков массово:** звонки идут через AMI (TCP к Asterisk), НЕ через http/nginx. Окно 502 бьёт только по дашборду (read-only, «мигнёт»).
- **Окно без AMI:** старый all остановлен, worker на 8104 ещё не поднят (Шаг 3) → никто не слушает AMI 5-15 сек. Asterisk не переотправит эти события, журнал их не запишет (записывать некому). Разовая потеря — только звонки, чьё событие пришло ровно в эти секунды. Эквивалент любого прошлого рестарта воркера. **Минимизируется тем, что Шаг 3 идёт сразу за Шагом 2.**
- Проверка шага 2: `curl -s 127.0.0.1:8102/health` → `{"status":"ok","role":"api",...}`. `curl -s 127.0.0.1:8102/api/v1/health` → JSON, `ami_connected:false` (норм для api). `curl -s https://kt.aiplus.kz/api/v1/health` → 200 (nginx→8102→api). Дашборд открывается. `curl "127.0.0.1:8102/api/v1/calls/?project_id=<id>&limit=1"` без токена → 401.

**Шаг 3 — НЕМЕДЛЕННО поднять worker на 8104 (восстанавливает приём звонков + replay).**
```
systemctl --user start kurotrack-worker.service    # юнит уже с конфигом 8104 + KURO_ROLE=worker
```
- worker поднимает AMI (реконнект ~2-5с), запускает `replay_pending_events` (догоняет `pending`/`failed` из журнала — то, что записалось ДО Шага 2 и не успело обработаться; звонки окна без AMI в журнал не попали), запускает `run_cleanup_loop`/`run_reconciliation_loop`/`run_amo_poll_loop`/`run_journal_cleanup_loop`.
- `run_reconciliation_loop` дополнительно восстанавливает атрибуцию звонков без source — частично компенсирует окно.
- Проверка шага 3: `curl -s 127.0.0.1:8104/health` → `role:worker`. `curl -s 127.0.0.1:8104/api/v1/health` → 200, `ami_connected:true` (после реконнекта). `curl -s 127.0.0.1:8104/api/v1/calls/...` → 404 (бизнес-роуты сняты — так и надо). В `logs/worker.log`: `KuroTrack starting with KURO_ROLE=worker`, затем `AMI journal replay: reprocessed N`, затем `CDR saved: ...`.
- Приём звонков: `PGPASSWORD=kuro psql ... -c "SELECT count(*) FROM calls WHERE started_at > now() - interval '10 min';"` — растёт.

**Шаг 4 — обновить и прогнать смоук + финальная проверка (nginx НЕ трогали ни разу).**
- Смоук уже обновлён под split (см. §«Обновление смоука»). `bash scripts/smoke_test.sh` → 0 FAIL.
- `curl -s 127.0.0.1:8102/health` → `role:api`; `curl -s 127.0.0.1:8104/health` → `role:worker`.
- `curl -s https://kt.aiplus.kz/api/v1/health` → 200.
- 15 минут `tail -f logs/worker.log` — `CDR saved` идут, `ami_connected:true`.
- `PGPASSWORD=kuro psql ... -c "SELECT count(*) FROM pg_stat_activity WHERE datname='kurotrack';"` — **< 80**. Если ≥ 80 → уменьшить api до `--workers 1` в юните, `daemon-reload`, `restart kurotrack-api` (см. R1).
- `PGPASSWORD=kuro psql ... -c "SELECT status, count(*) FROM ami_events GROUP BY status;"` — `done` растёт, `failed` ≈ 0.

**Полный откат к монолиту:**
```
systemctl --user stop kurotrack-api.service kurotrack-worker.service
cp ~/kurotrack-worker.service.bak-YYYY-MM-DD ~/.config/systemd/user/kurotrack-worker.service   # монолит: порт 8102, роль all
systemctl --user daemon-reload
systemctl --user start kurotrack-worker.service
curl -s 127.0.0.1:8102/api/v1/health   # → 200, ami_connected:true. nginx→8102→монолит, всё как было
```
Если `.bak` не сняли — восстановить монолит-юнит из git: `git show <prev>:infra/systemd/kurotrack-worker.service` (старый: порт 8102, без `KURO_ROLE`).

> **Почему выбран порядок «стоп all → старт api → старт worker», а не «сначала worker на 8104»:** альтернатива (поднять worker на 8104 ПЕРВЫМ, пока монолит ещё держит 8102) даёт ДВА одновременных AMI-коннекта → двойная обработка каждого события (безопасно по данным — дедуп по uniqueid и AMO-дедуп, но двойная нагрузка + двойной replay). Выбранный порядок даёт единственное короткое окно БЕЗ AMI (проще рассуждать, потеря разовая как при рестарте), а не окно с двойной обработкой.

### Обновление смоука `scripts/smoke_test.sh` (ОТДЕЛЬНАЯ ЗАДАЧА)

Точечные правки (файл `/home/alisher/kurotrack/scripts/smoke_test.sh`):

1. Константы (сейчас L47 `WORKER_URL="http://127.0.0.1:8102"`):
   - `WORKER_URL="http://127.0.0.1:8104"` — worker health теперь на 8104.
   - Добавить `API_URL="http://127.0.0.1:8102"` — локальный API на 8102.
2. Секция **B (Live worker health)** — AMI/DB/Redis-проба на `$WORKER_URL/api/v1/health` (=8104): только у воркера `ami_connected:true`. `systemctl --user is-active kurotrack-worker.service` остаётся.
3. Добавить проверку API-процесса (в секцию B или новую): `curl -sf "$API_URL/health"` → `role:api`; `systemctl --user is-active kurotrack-api.service` → `active`.
4. Секция **E (DNI get-number)** сейчас бьёт `$WORKER_URL/api/v1/tracking/get-number`. Роут `tracking` в role=worker **НЕ смонтирован (404!)**. Заменить в секции E `$WORKER_URL` → `$API_URL` (DNI обслуживает API-процесс на 8102).
5. Секция **D (public API через nginx)** — БЕЗ изменений (kt.aiplus.kz → nginx → 8102 → api).
6. Секция **C** (`db connections < 80`) — БЕЗ изменений, порог актуален (суммарный пул 70).

> После правок: сначала выкат (шаги 0-3), потом прогон обновлённого смоука (он ожидает split 8104/8102). `bash scripts/smoke_test.sh` → 0 FAIL.

### Риски П2.14 (переработано под новые ограничения)
- **R1 — суммарный пул БД упрётся в `max_connections=100`.** api `--workers 2` × 30 = до 60 + worker 40 = до 100 в пике. Митигация: (а) api-пул консервативный 15+15; (б) `get_db` rollback чистит idle; (в) смоук ловит `db connections >= 80` как FAIL; (г) **безопасный рычаг без правки кода — уменьшить api до `--workers 1`** (тогда 30+40=70). Мониторить `pg_stat_activity` на шагах 2-4. Поднять `max_connections` НЕЛЬЗЯ (нет docker/root).
- **R2 — окно 502 на дашборде при переключении (Шаг 2).** Несколько секунд, пока API поднимается на 8102. НЕ потеря звонков (звонки через AMI). Дашборд «мигнёт». Приемлемо.
- **R3 — окно без AMI между Шагом 2 и Шагом 3.** Старый all остановлен, worker на 8104 ещё не поднят → никто не слушает AMI 5-15 сек; звонки окна теряются (Asterisk не переотправляет, журнал не спасает). Митигация: держать окно минимальным (Шаг 3 сразу за Шагом 2), reconciliation частично восстановит атрибуцию. Разовое окно = обычный рестарт воркера.
- **R4 — коллизия имён юнита `kurotrack-worker.service` (старый монолит vs новый worker).** `cp` перезаписывает файл, живой процесс до рестарта работает по старому конфигу. Митигация: строгий порядок (не рестартить worker пока API не занял 8102), бэкап живого юнита на Шаге 0, полный откат восстанавливает монолит из `.bak`/git.
- **R5 — callback-originate попал на API-процесс без AMI → 503.** Регрессия callback-виджета в split-режиме (раньше монолит имел AMI). Митигация: callback-трафик околонулевой; вынос callback на worker требует правки nginx (sudo) → out of scope. Задокументировано, не блокирует. См. §4, §8.
- **R6 — nginx случайно затронут.** Митигация: в этом плане nginx НЕ редактируется, `sudo` не вызывается ни разу. Если исполнитель порывается тронуть nginx/sudo — СТОП, это ошибка (порт остаётся 8102).

---

## П2.13 — выбор варианта (обоснование, для истории)

**Выбран Вариант А: таблица `ami_events` в Postgres** (не Redis Stream):
1. **Транзакционная согласованность** — журнал и звонки в одной БД, replay читает ту же транзакционную БД.
2. **Durability by default** — Postgres на диске с WAL; Redis-durability на проде не гарантирована.
3. **Стек уже готов** — `async_session`, `text()`, JSONB, alembic. Redis Streams — новый паттерн, больше кода/краевых случаев.
4. **Отладка** — `SELECT * FROM ami_events WHERE status='failed'` тривиально.
5. **Нагрузка** — до ~20k INSERT/сутки, <1/сек в среднем; индексированный INSERT переваривает с запасом. Ретеншн 7 дней держит таблицу <150k строк.

---

## Как проверить (сводно, + смоук)

### П2.13 (в проде)
- Реальный звонок → `SELECT status, count(*) FROM ami_events GROUP BY status;` → `done` растёт.
- Рестарт-тест: вставить `pending`-строку с валидным cdr-payload, рестарт воркера → в логах `AMI journal replay: reprocessed N`, звонок в `calls` без дубля.
- `SELECT count(*) FROM ami_events WHERE status='failed';` ≈ 0.
- pytest: `python -m pytest tests/test_ami_journal.py -q` → зелёный.

### П2.15 (в проде)
- В логах воркера `AMO poll: synced N leads`, итерация укладывается в 600с.
- Смоук секция F: AMO token 200 (нет 429).

### П2.14 (текущая волна)
- `curl 127.0.0.1:8102/health` → `role:api`; `curl 127.0.0.1:8104/health` → `role:worker`.
- Дашборд через `https://kt.aiplus.kz` работает (nginx→8102→api, nginx не трогали).
- `curl 127.0.0.1:8104/api/v1/calls/...` → 404 (роуты сняты у воркера); `curl 127.0.0.1:8102/api/v1/calls/...` → 401 без токена (api обслуживает).
- `bash scripts/smoke_test.sh` (обновлённый) → 0 FAIL.
- `SELECT count(*) FROM pg_stat_activity WHERE datname='kurotrack';` < 80.
- `SHOW max_connections;` = 100 (не менялся).

---

## Out of Scope
- Оптимизация массового replay (батчинг) при многочасовом даунтайме.
- Проксирование callback-originate на worker в split-режиме (требует правки nginx = sudo → недоступно).
- Redis Streams (отклонён).
- **Изменение `max_connections` Postgres** (требует docker/root — недоступно; живём в лимите 100).
- Правка nginx-конфига любого рода (требует sudo → недоступно; порт backend остаётся 8102).
- Вынос `pool_size`/`--workers` в отдельные ENV-переменные (сейчас пул зашит по роли в `database.py`, `--workers` — в systemd-юните).

---

## 10. Tasks JSON Block

```json
{
  "tasks": [
    {
      "id": "P2.14-role-database-pool",
      "description": "core/role.py (KURO_ROLE + is_api/is_worker) + database.py: раздельный пул БД по роли через чистую функцию _pool_params (worker 25+15 / api 15+15 / all 30+40)",
      "files": ["backend/app/core/role.py", "backend/app/core/database.py"],
      "owner": "sonnet-backend",
      "wave": 1,
      "depends_on": [],
      "risk": "Средний: database.py импортируется всем приложением. Дефолт all=монолит-пул 30+40 (обратная совместимость). Ошибка в импорте role.py уронит старт. Проверить: нет циклов импорта (role.py НЕ импортирует config/database).",
      "estimated_turns": 20,
      "acceptance": ["KURO_ROLE дефолт all, мусор->all", "is_api/is_worker корректны для 3 ролей", "_pool_params: worker=25/15, api=15/15, all=30/40", "engine использует _pool_params(KURO_ROLE)", "нет циклов импорта"],
      "status": "pending"
    },
    {
      "id": "P2.14-main-role-gating",
      "description": "main.py: весь AMI+воркеры+replay+journal-cleanup в блок if is_worker(); монтирование бизнес-роутов только if is_api(); health монтируется всегда; /health отдаёт поле role",
      "files": ["backend/app/main.py"],
      "owner": "sonnet-backend",
      "wave": 2,
      "depends_on": ["P2.14-role-database-pool"],
      "risk": "Высокий: меняет запуск всего приложения. КРИТИЧНО: replay ТОЛЬКО в is_worker() (иначе N replay при api --workers N). Дефолт all = полная обратная совместимость. Health всегда смонтирован.",
      "estimated_turns": 25,
      "acceptance": ["replay+cleanup+ami+воркеры только в is_worker()", "бизнес-роуты только в is_api()", "health монтируется во всех ролях", "/health отдаёт role", "all поднимает всё как раньше"],
      "status": "pending"
    },
    {
      "id": "P2.14-systemd-units",
      "description": "Два systemd user-юнита в infra/systemd: kurotrack-api.service (порт 8102, role=api, --workers 2) и kurotrack-worker.service (порт 8104, role=worker). nginx НЕ трогать.",
      "files": ["infra/systemd/kurotrack-api.service", "infra/systemd/kurotrack-worker.service"],
      "owner": "sonnet-infra",
      "wave": 2,
      "depends_on": [],
      "risk": "Низкий: конфиги в репо, не применяются автоматически. Реальный выкат — по пошаговому плану человеком/деплоером. nginx НЕ редактируется (нет sudo). Имя worker-юнита не менять (смоук/systemctl завязаны).",
      "estimated_turns": 12,
      "acceptance": ["api-юнит: --port 8102, KURO_ROLE=api, --workers 2", "worker-юнит: --port 8104, KURO_ROLE=worker, без --workers", "SyslogIdentifier=kurotrack-worker сохранён", "оба EnvironmentFile=.env.worker", "nginx.conf НЕ изменён"],
      "status": "pending"
    },
    {
      "id": "P2.14-smoke-split",
      "description": "scripts/smoke_test.sh: WORKER_URL->8104, добавить API_URL=8102; секция B — worker health на 8104 + проверка api-процесса на 8102; секция E (DNI) -> API_URL 8102; секции C/D без изменений",
      "files": ["scripts/smoke_test.sh"],
      "owner": "sonnet-infra",
      "wave": 3,
      "depends_on": ["P2.14-main-role-gating", "P2.14-systemd-units"],
      "risk": "Низкий: смоук read-only. Важно: DNI-роут в worker=404, перевести E на 8102. Не ломать секции C/D/F/G.",
      "estimated_turns": 15,
      "acceptance": ["WORKER_URL=8104, API_URL=8102", "B бьёт ami_connected на 8104", "B проверяет api role на 8102 + systemctl kurotrack-api active", "E (DNI) на 8102", "D (nginx public) без изменений", "смоук синтаксически валиден (bash -n)"],
      "status": "pending"
    },
    {
      "id": "P2.14-tests",
      "description": "Unit-тесты role helpers (3 роли + мусор) и роль->пул (_pool_params); закрепить журнал/replay/sync_recent_leads если тесты ещё не в проде; pyproject.toml asyncio_mode=auto",
      "files": ["backend/tests/test_ami_journal.py", "backend/pyproject.toml"],
      "owner": "sonnet-tester",
      "wave": 3,
      "depends_on": ["P2.14-role-database-pool", "P2.14-main-role-gating"],
      "risk": "Низкий: мокаем async_session, реальную БД не поднимаем. Не менять существующие тесты. Тест роли/пула через чистые функции/патч модульной переменной.",
      "estimated_turns": 20,
      "acceptance": ["role helpers покрыты (all/api/worker/garbage)", "_pool_params(role) покрыта для 3 ролей", "asyncio_mode=auto добавлен", "python -m pytest -q зелёный", "существующие тесты не тронуты"],
      "status": "pending"
    }
  ]
}
```

### Раскладка волн (пояснение к JSON)

- **Wave 1 (фундамент):** `P2.14-role-database-pool` — `role.py` + `database.py` (раздельный пул). Всё остальное зависит от `role.py`.
- **Wave 2 (параллельно, разные владельцы):** `P2.14-main-role-gating` (`main.py`, owner `sonnet-backend`) и `P2.14-systemd-units` (`infra/systemd/*`, owner `sonnet-infra`). Файлы не пересекаются.
- **Wave 3 (параллельно):** `P2.14-smoke-split` (`scripts/smoke_test.sh`, owner `sonnet-infra`) и `P2.14-tests` (`tests/*`+`pyproject.toml`, owner `sonnet-tester`). Файлы не пересекаются.

### File Ownership (нет конфликтов внутри волны)
- Wave 1: `sonnet-backend` — `role.py`, `database.py`.
- Wave 2: `sonnet-backend` — `main.py`; `sonnet-infra` — `infra/systemd/kurotrack-api.service`, `infra/systemd/kurotrack-worker.service`. Пересечений нет.
- Wave 3: `sonnet-infra` — `scripts/smoke_test.sh`; `sonnet-tester` — `tests/test_ami_journal.py`, `pyproject.toml`. Пересечений нет.
- `infra/nginx/kurotrack.conf` НЕ модифицируется НИ ОДНОЙ задачей (nginx неприкосновенен, нет sudo).
- `ami_client.py`, `call_processor.py`, `amo_poll.py`, `amo_sync.py`, `ami_journal.py`, миграция `0006` — П2.13/П2.15, уже в проде (HEAD 04d57f5), в этой волне НЕ трогаются.
```
