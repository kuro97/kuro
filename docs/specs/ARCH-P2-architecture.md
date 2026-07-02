# ARCH-P2-architecture — блок архитектурного долга KuroTrack

Автор: CTO (Opus). Исполнители: Sonnet-разработчики.
Репозиторий: `/Users/baigelenov/projects/kurotrack`, ветка `master` (базовый HEAD `8127793`).
Прод: `sshpass -p '...' ssh kuro-server`, репо `/home/alisher/kurotrack` (на master, идентичен).

> ВАЖНО для исполнителей:
> - Комментарии в коде — **на русском**.
> - SQL в коде — **только параметризованный** (asyncpg через SQLAlchemy `text(...)` с bind-параметрами `:name`). f-string/конкатенация в SQL запрещены.
> - Не рефакторить лишнего. Каждый пункт — отдельная задача (см. JSON-блок в конце).
> - Все пути в этой спеке даны от корня репо `/Users/baigelenov/projects/kurotrack/`.
> - Прод-факты (проверены при разведке 2026-07-02):
>   - Postgres: `127.0.0.1:5433`, БД `kurotrack`, user `kuro`. (в docker-compose локально — 5432, на проде — 5433).
>   - Redis: `127.0.0.1:6380/0`.
>   - Воркер: systemd **user**-юнит `~/.config/systemd/user/kurotrack-worker.service`, запускает `uvicorn app.main:app --host 127.0.0.1 --port 8102 --limit-max-requests 1000`, `EnvironmentFile=/home/alisher/kurotrack/backend/.env.worker`, `WorkingDirectory=/home/alisher/kurotrack/backend`, `venv` = `/home/alisher/kurotrack/backend/venv` (НЕ `.venv`!).
>   - nginx проксирует `/api/` → `127.0.0.1:8102`, `/healthz` → `127.0.0.1:8102/api/v1/health`.
>   - Управление: `systemctl --user ...` (не root). Reboot-persist через `loginctl enable-linger` уже настроен (юнит `WantedBy=default.target`).

---

## 1. Summary

Закрываем 3 пункта архитектурного долга (П2.13–П2.15) на воркере обработки звонков KuroTrack.

- **П2.13 (главный, wave 1)** — персистентный журнал AMI-событий в Postgres (таблица `ami_events`). Сейчас `Cdr`/`Newchannel` обрабатываются in-flight: если процесс упал/рестартовал между приёмом `Cdr` и `commit`, звонок теряется навсегда (Asterisk не переотправляет). Решение: сырое событие сразу пишется в `ami_events` быстрым INSERT (status=`pending`), обрабатывается прежним хендлером, помечается `done`. При старте воркера — replay всех `pending`. Идемпотентность гарантирована существующей дедупликацией по `uniqueid` (`ix_calls_uniqueid` UNIQUE) — replay не плодит дубли звонков/лидов.
- **П2.15 (wave 1, параллельно)** — AMO-polling: окно уменьшается с 720 ч (30 дней) до 4 ч, добавляется ограничение параллелизма (semaphore + пауза под rate-limit AMO ~7 req/s). Webhook (`/api/v1/amo/webhook`) остаётся основным каналом real-time и ловит поздние изменения (оплата через неделю), polling — быстрый догон пропущенных webhook за последние часы.
- **П2.14 (wave 2, самый инвазивный)** — разделение единого процесса на роли через ENV-флаг `KURO_ROLE=api|worker|all` (дефолт `all` = полная обратная совместимость). `api` поднимает только HTTP-роуты (можно `--workers N`), `worker` — AMI + фоновые воркеры + replay журнала. Пошаговый бездаунтаймовый план миграции прода с откатом.

Результат: воркер переживает рестарт/краш без потери звонков (журнал + replay), AMO-polling укладывается в интервал и не бьётся об rate-limit, а нагрузку API можно горизонтально масштабировать отдельно от единственного процесса-обработчика звонков.

---

## 2. Acceptance Criteria

1. Таблица `ami_events` существует (миграция `0006`), поля: `id BIGSERIAL PK`, `event_type TEXT`, `uniqueid TEXT NULL`, `payload JSONB`, `status TEXT` (pending/done/failed), `received_at TIMESTAMPTZ`, `processed_at TIMESTAMPTZ NULL`, `attempts INT DEFAULT 0`, `last_error TEXT NULL`. Есть частичный индекс на `status='pending'` и индекс на `received_at`.
2. Каждое `cdr` / `new_call` / `hangup` событие СНАЧАЛА пишется в `ami_events` (INSERT `pending`), ТОЛЬКО ПОТОМ обрабатывается. После успешной обработки — `status='done'`, `processed_at=now()`. При исключении — `status='failed'`, `attempts+=1`, `last_error`.
3. При старте воркера `replay_pending_events()` берёт все `pending`/`failed` (attempts < 5) события, отсортированные по `received_at ASC`, и переобрабатывает их через тот же `process_call_event`. Replay НЕ создаёт дублей звонков (проверка `ix_calls_uniqueid`) и дублей AMO-лидов (существующая 3-уровневая дедупликация).
4. Ретеншн: `done`-события старше 7 дней удаляются фоновым циклом раз в час.
5. `_LOOKBACK_HOURS` в `amo_poll.py` = 4 (было 720). `sync_recent_leads` ограничивает параллелизм семафором на 5 одновременных `sync_lead` и держит паузу так, чтобы не превышать ~5 req/s к AMO.
6. Webhook `/api/v1/amo/webhook` не менялся по контракту и остаётся основным каналом (проверяется существующими вызовами в коде).
7. `KURO_ROLE` читается из ENV (дефолт `all`). При `KURO_ROLE=api` lifespan НЕ поднимает `ami_client`, `run_cleanup_loop`, `run_reconciliation_loop`, `run_amo_poll_loop`, `replay_pending_events`. При `KURO_ROLE=worker` lifespan поднимает всё перечисленное; роуты монтируются только `/health` и `/api/v1/health`. При `KURO_ROLE=all` — как сейчас (всё + все роуты).
8. `GET /health` и `GET /api/v1/health` работают во ВСЕХ трёх ролях.
9. Два systemd user-юнита готовы: `kurotrack-api.service` (порт **8102**, `KURO_ROLE=api`, `--workers 1`, пул 8/12) и обновлённый `kurotrack-worker.service` (порт **8104**, `KURO_ROLE=worker`, пул 25/25). nginx **НЕ трогается** (нет root): api сел на уже-проксируемый 8102, worker уехал на 8104. Размеры пула БД заданы через ENV `KURO_DB_POOL_SIZE`/`KURO_DB_MAX_OVERFLOW`, суммарный максимум коннектов = 70 при `max_connections=100`.
10. `bash scripts/smoke_test.sh` проходит (exit 0, 0 FAIL) после каждого шага миграции.
11. Unit-тесты: журнал (INSERT pending → done), replay идемпотентен (не плодит звонки), ретеншн-запрос, `sync_recent_leads` не превышает лимит параллелизма. Все проходят.

---

## 3. Files to Create

| Path | Purpose | Key Functions / содержимое |
|------|---------|----------------------------|
| `backend/app/models/ami_event.py` | ORM-модель журнала событий | class `AmiEvent(Base)` — маппинг на таблицу `ami_events` |
| `backend/migrations/versions/0006_ami_events.py` | Миграция таблицы журнала | `upgrade()` / `downgrade()`; revision `0006`, down_revision `0005` |
| `backend/app/services/ami_journal.py` | Сервис журнала (запись/replay/ретеншн) | `async def record_event(event: dict) -> int`, `async def mark_done(event_id: int) -> None`, `async def mark_failed(event_id: int, error: str) -> None`, `async def replay_pending_events(handler) -> int`, `async def cleanup_old_events(retention_days: int = 7) -> int`, `async def run_journal_cleanup_loop() -> None` |
| `backend/app/core/role.py` | Определение роли процесса | `KURO_ROLE: str` (константа из ENV), helpers `is_worker() -> bool`, `is_api() -> bool` |
| `infra/systemd/kurotrack-api.service` | systemd user-юнит для API-роли | текст юнита (см. §5 / §П2.14) |
| `infra/systemd/kurotrack-worker.service` | Эталонный текст worker-юнита (`KURO_ROLE=worker`) | текст юнита (см. §П2.14) |
| `backend/tests/test_ami_journal.py` | Unit-тесты журнала и replay | тесты см. §9 |

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

KURO_ROLE читается из ENV (pydantic-settings уже даёт префикс KURO_, но роль
влияет на lifespan ДО инициализации FastAPI, поэтому читаем напрямую os.environ
чтобы не тащить весь Settings в этот модуль).

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
    """True для ролей worker и all (нужно поднимать AMI + воркеры)."""
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

> ВАЖНО: модель `AmiEvent` НЕ добавляется в `backend/app/models/__init__.py` `__all__` только если это ломает alembic autogenerate — но у нас миграции пишутся руками, поэтому **добавь** импорт в `__init__.py`, чтобы модель регистрировалась в `Base.metadata` (env.py делает `from app.models import *`). См. §4.

---

## 4. Files to Modify

| Path | Что меняется | Строки (примерно) |
|------|--------------|-------------------|
| `backend/app/services/ami_client.py` | Внутри `_handle_newchannel`/`_handle_hangup`/`_handle_cdr` перед вызовом хендлеров — записать событие в журнал и обернуть обработку в mark_done/mark_failed | новый общий helper + правки L204-208, L219-223, L257-261 |
| `backend/app/workers/call_processor.py` | `process_call_event` принимает опциональный `event_id` и не трогает журнал сам (журнал ведёт ami_client). Изменений минимум — см. ниже | L75-84 |
| `backend/app/main.py` | Условный lifespan по `KURO_ROLE`; условное монтирование роутов; запуск `replay_pending_events` и `run_journal_cleanup_loop` в worker-роли | L23-79 |
| `backend/app/models/__init__.py` | Добавить `AmiEvent` в импорты и `__all__` | L1-7 |
| `backend/app/workers/amo_poll.py` | `_LOOKBACK_HOURS = 4` | L21 |
| `backend/app/services/amo_sync.py` | `sync_recent_leads`: семафор + rate-limit пауза | L298-332 |
| `backend/app/core/config.py` | (П2.14) Добавить `db_pool_size: int = 30` и `db_max_overflow: int = 40` в `Settings` (читаются как `KURO_DB_POOL_SIZE`/`KURO_DB_MAX_OVERFLOW`) | рядом с блоком PostgreSQL, ~L13 |
| `backend/app/core/database.py` | (П2.14) `pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow` вместо литералов 30/40. Остальные параметры engine НЕ трогать | L11-12 |
| `scripts/smoke_test.sh` | (П2.14) `WORKER_URL` c `127.0.0.1:8102` → `127.0.0.1:8104` (воркер переехал; публичный API через nginx остаётся 8102) | L35 |
| `infra/nginx/kurotrack.conf` | **НЕ ТРОГАЕТСЯ** (нет root для reload). api-роль садится на уже-проксируемый 8102 — nginx работает без изменений (см. §П2.14) | — |

### Точная интеграция в `ami_client.py` (П2.13)

Добавь наверху файла импорт (после существующих импортов, ~L16):

```python
from app.services import ami_journal
```

Добавь приватный метод в класс `AMIClient` (например после `_handle_cdr`, до `disconnect`):

```python
async def _dispatch_with_journal(self, event_data: dict) -> None:
    """Пишет событие в журнал, затем прогоняет через все хендлеры.

    Порядок: INSERT pending → handler(event_data) → mark_done.
    Если handler бросил — mark_failed (событие переобработается при старте).
    Журнал — страховка: если record_event вернул None (сбой БД), обработка
    всё равно идёт, просто без страховки для этого события.
    """
    event_id = await ami_journal.record_event(event_data)
    ok = True
    for handler in self._call_handlers:
        try:
            await handler(event_data)
        except Exception:
            ok = False
            logger.exception("Ошибка в обработчике события звонка")
    if event_id is not None:
        try:
            if ok:
                await ami_journal.mark_done(event_id)
            else:
                await ami_journal.mark_failed(event_id, "handler raised")
        except Exception:
            logger.exception("Не удалось обновить статус события журнала id=%s", event_id)
```

В `_handle_newchannel` замени финальный блок (текущие L204-208):

```python
        for handler in self._call_handlers:
            try:
                await handler(event_data)
            except Exception:
                logger.exception("Ошибка в обработчике события звонка")
```

на:

```python
        await self._dispatch_with_journal(event_data)
```

Аналогично в `_handle_hangup` (текущие L219-223) и `_handle_cdr` (текущие L257-261) — замени соответствующие циклы `for handler in self._call_handlers:` на `await self._dispatch_with_journal(event_data)`.

> Причина такого места интеграции: `event_data` — это уже нормализованный словарь, который передаётся в `process_call_event`. Именно этот словарь и надо сохранять/replay-ить. Redis-обогащение (`inbound_did`, `linkedid_for`) происходит в `_handle_newchannel`/`_handle_cdr` ДО формирования `event_data` — оно остаётся в Redis (TTL 3600-7200с), так что при replay в пределах TTL данные ещё доступны; за пределами TTL DID возьмётся из `user_field`/`dst` fallback (логика `_resolve_did` уже это умеет). Это допустимая деградация: без журнала звонок терялся полностью, с журналом — восстанавливается хотя бы частично атрибутированным.

### Правка `call_processor.py` (П2.13) — минимальная

`process_call_event` НЕ трогает журнал (журналом заведует `ami_client._dispatch_with_journal`). Единственное требование — функция должна корректно отрабатывать при **повторном** вызове (replay). Она уже идемпотентна:
- `_handle_cdr` проверяет дубль по `uniqueid` (L495-500) и ловит `IntegrityError` на `ix_calls_uniqueid` (L503-513).
- AMO-дедуп трёхуровневый (`_push_to_amo`).

**Никаких изменений в `call_processor.py` для журнала не требуется.** Задача P2.13 не модифицирует этот файл (важно для file-ownership — см. §10).

> Единственный нюанс: при replay `active_calls` (in-memory кеш, L30) пуст, поэтому `started_at` возьмётся из `datetime.now(timezone.utc)` (L465-467) — это допустимо, время звонка сместится максимум на длительность даунтайма. CDR всё равно сохранится, лид создастся. Документируй это комментарием в `replay_pending_events`.

### Правка `main.py` (П2.14) — условный lifespan

Полностью заменяемый блок — L1-52 (импорты + lifespan). Новый вид:

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

    # --- worker-роль: AMI + фоновые воркеры + replay журнала ---
    if is_worker():
        # Регистрируем обработчик и запускаем reconnect-цикл AMI
        ami_client.on_call_event(process_call_event)
        await ami_client.start()

        # Синхронизация пула номеров из БД в Redis
        try:
            await sync_pool_from_db()
        except Exception:
            logger.warning("Failed to sync number pool from DB — pool may be empty")

        # REPLAY: переобрабатываем зависшие AMI-события (защита от потери звонков)
        try:
            replayed = await ami_journal.replay_pending_events(process_call_event)
            if replayed:
                logger.info("AMI journal replay: reprocessed %d events", replayed)
        except Exception:
            logger.exception("AMI journal replay failed")

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
```

Ниже (L55-79) — условное монтирование роутов:

```python
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

> ВНИМАНИЕ по callback-виджету: `callback.router` использует `ami_client.originate_call`. В роли `api` роут смонтирован, но `ami_client` НЕ подключён → `originate_call` бросит `RuntimeError("AMI not connected")` → 503. Это **известное ограничение**: callback-originate работает только в `all` или должен ходить в worker. Для текущей миграции (api берёт read-трафик дашборда, worker держит AMI) callback остаётся на `all`-совместимости через worker если понадобится. Документируй, не блокируй — прод-трафик callback околонулевой, дашборд-чтение критичнее. Порт для callback в api-роли отдаёт 503 честно.

### Правка `amo_poll.py` (П2.15)

L21 — заменить:

```python
_LOOKBACK_HOURS = 24 * 30
```

на:

```python
# Polling — страховочный догон за webhook (основной канал real-time).
# 4 часа с запасом покрывают любые кратковременные сбои доставки webhook.
# Поздние изменения (оплата через неделю) ловит webhook, не polling.
_LOOKBACK_HOURS = 4
```

Комментарий про 30 дней (L18-20) убрать/переписать под новую логику.

### Правка `amo_sync.py` (П2.15) — rate-limit в `sync_recent_leads`

Добавь наверху файла (после существующих импортов):

```python
import asyncio
```

Добавь модульные константы (рядом с `_FIELD_CITY`, ~L34):

```python
# Ограничение параллелизма polling к AMO API. AMO лимит ~7 req/s.
# Один sync_lead делает 1-2 HTTP-запроса (GET lead + опц. GET statuses),
# поэтому 5 одновременных + пауза держат нас безопасно ниже лимита.
_POLL_CONCURRENCY = 5
# Пауза между запусками sync_lead после захвата слота семафора (сек).
_POLL_PAUSE_SEC = 0.2
```

Замени тело `sync_recent_leads` (L298-332). Новый вид цикла (сохрани сигнатуру и docstring, поменяй только цикл обработки):

```python
    async def sync_recent_leads(self, hours_back: int = 4) -> int:
        """Берёт все Call.amo_lead_id за последние N часов и зовёт sync_lead для каждого.

        Параллелизм ограничен семафором _POLL_CONCURRENCY + пауза _POLL_PAUSE_SEC,
        чтобы не превысить rate-limit AMO (~7 req/s). Возвращает число успешно
        обновлённых записей.
        """
        if not self._is_configured():
            logger.warning("AMO CRM не настроен — sync_recent_leads пропускаем")
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

        async with async_session() as db:
            rows = await db.execute(
                select(Call.amo_lead_id).where(
                    Call.amo_lead_id.is_not(None),
                    Call.started_at >= cutoff,
                )
            )
            lead_ids: list[int] = [r[0] for r in rows.all()]

        if not lead_ids:
            return 0

        # Дедуп: один и тот же лид может быть у нескольких leg-звонков.
        lead_ids = list(dict.fromkeys(lead_ids))

        semaphore = asyncio.Semaphore(_POLL_CONCURRENCY)
        updated = 0

        async def _bounded_sync(lid: int) -> bool:
            async with semaphore:
                # Пауза внутри слота растягивает поток запросов под rate-limit.
                await asyncio.sleep(_POLL_PAUSE_SEC)
                try:
                    return await self.sync_lead(lid)
                except Exception:
                    logger.exception("sync_recent_leads: ошибка при sync_lead(%d)", lid)
                    return False

        results = await asyncio.gather(
            *[_bounded_sync(lid) for lid in lead_ids],
            return_exceptions=False,
        )
        updated = sum(1 for r in results if r)

        return updated
```

> Обоснование значений: при 5 слотах и паузе 0.2с эффективная скорость ≈ 5 запросов за 0.2с в худшем пике, но реальный throughput ограничен сетью до AMO (timeout=10с на запрос) — практически поток не превысит ~7 req/s. Изменения статусов «поздних» лидов (оплата спустя неделю) НЕ теряются: их ловит webhook `leads[status]`/`leads[update]` → `sync_lead` (см. `amo_webhook.py` L63-91). Polling лишь дублирует webhook за последние 4 часа на случай недоставки.

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

---

## 5. Database Changes

### Миграция `backend/migrations/versions/0006_ami_events.py`

Стиль — как `0005` (`op.execute` с сырым DDL, revision-строки). Готово к копипасту:

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
    # Персистентный журнал сырых AMI-событий: защита от потери звонков при
    # рестарте/краше процесса между приёмом Cdr и commit.
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
    # Частичный индекс: replay при старте берёт только незавершённые события.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_ami_events_pending
        ON ami_events (received_at)
        WHERE status IN ('pending', 'failed')
    """)
    # Индекс для ретеншна done-событий по времени.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_ami_events_status_received
        ON ami_events (status, received_at)
    """)
    # Индекс по uniqueid для корреляции/дебага.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_ami_events_uniqueid
        ON ami_events (uniqueid)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ami_events")
```

> Прод-применение: миграция накатывается на воркере (у него DATABASE_URL на 5433):
> `cd /home/alisher/kurotrack/backend && venv/bin/alembic upgrade head`
> Таблица пустая на старте, `CREATE TABLE IF NOT EXISTS` идемпотентен — повторный запуск безопасен.

### SQL внутри `ami_journal.py` (параметризованный, через `text()`)

```python
# record_event
INSERT INTO ami_events (event_type, uniqueid, payload, status, received_at)
VALUES (:event_type, :uniqueid, CAST(:payload AS JSONB), 'pending', now())
RETURNING id
# bind: event_type=event.get("event"), uniqueid=event.get("uniqueid"),
#        payload=json.dumps(event, default=str)

# mark_done
UPDATE ami_events SET status='done', processed_at=now() WHERE id = :id

# mark_failed
UPDATE ami_events
SET status='failed', attempts = attempts + 1, last_error = :error
WHERE id = :id

# replay: выборка
SELECT id, payload FROM ami_events
WHERE status IN ('pending','failed') AND attempts < :max_attempts
ORDER BY received_at ASC

# cleanup_old_events
DELETE FROM ami_events
WHERE status='done' AND processed_at < now() - make_interval(days => :days)
```

> Примечание по `payload`: колонка `JSONB`. asyncpg не сериализует dict в JSONB автоматически через bind-параметр `text()` — передавай **строку** `json.dumps(event, default=str)` и оборачивай `CAST(:payload AS JSONB)`. При чтении в replay asyncpg отдаст JSONB как готовый `dict` (или строку — тогда `json.loads`; исполнитель проверит тип: `payload if isinstance(payload, dict) else json.loads(payload)`).

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

`GET /api/v1/health` (роутер `health.py`) — контракт БЕЗ изменений (`HealthResponse`: status, service, ami_connected, db_ok, redis_ok). В роли `api` поле `ami_connected` будет `false` (AMI не поднят) — это ожидаемо, смоук проверяет `ami_connected` только для воркера через `127.0.0.1:8104` (после миграции П2.14). Публичный `/healthz` через nginx бьёт в api-процесс на 8102 и покажет `ami_connected:false` — это корректно (см. §П2.14, R7).

Ошибки: без изменений. `/health` никогда не 5xx.

---

## 7. Frontend Contract

Изменений во фронтенде НЕТ. Дашборд обращается к тем же `/api/v1/*` эндпоинтам через nginx. После миграции П2.14 nginx **не меняется**: `/api/` по-прежнему идёт на `127.0.0.1:8102`, только теперь этот порт слушает выделенный api-процесс (роль `api`), а не монолит. Для фронтенда полностью прозрачно, URL не меняется.

TypeScript-типы не затрагиваются.

---

## 8. Edge Cases & Error Handling

### `ami_journal.record_event`
- **БД недоступна при INSERT** → лог `exception`, возврат `None`. Обработка события продолжается БЕЗ журнала (страховка не сработала для этого события, но звонок не блокируется). Не бросаем — иначе потеряем событие целиком.
- **payload не сериализуется** → `json.dumps(..., default=str)` покрывает datetime/UUID; при остаточной ошибке — лог + `None`.

### `ami_journal.replay_pending_events`
- **Дубль звонка при replay** → `_handle_cdr` находит существующий `Call` по `uniqueid` (L495-500) ИЛИ ловит `IntegrityError` (L503-513) → `return`, дубль не создаётся. Событие помечается `done`.
- **Redis-ключи `inbound_did`/`linkedid_for` протухли (TTL истёк за время даунтауна)** → `_resolve_did` откатится на `user_field`/`dst`. Звонок сохранится, атрибуция может деградировать. Приемлемо.
- **handler бросил при replay** → `mark_failed`, `attempts+=1`. При следующем старте попробуем снова, пока `attempts < 5`. После 5 — событие остаётся `failed` навсегда (ручной разбор через SQL).
- **Пустой журнал** → возврат 0, лог не пишется.
- **Огромный журнал (тысячи pending после долгого даунтайма)** → replay последовательный по `received_at ASC`; выполняется в lifespan ДО `yield`, т.е. блокирует старт приёма новых событий. Это осознанно: сначала догоняем старое, потом принимаем новое (порядок сохраняется). Если это неприемлемо по времени — исполнитель НЕ оптимизирует в этой задаче (out of scope), только логирует прогресс каждые 100 событий.

### `ami_client._dispatch_with_journal`
- **`record_event` вернул None** → `event_id is None` → mark_done/mark_failed пропускаются, обработка идёт как раньше (fully backward-compatible путь).
- **handler бросил** → `ok=False` → `mark_failed`. Событие переживёт рестарт.

### `amo_sync.sync_recent_leads`
- **AMO вернул 401 (токен протух)** → `sync_lead` вернёт False (существующая логика L219-224), лид не обновится. Semaphore/пауза не спасают от 401 — это ловит смоук (секция F). Не наша задача.
- **Пустой список lead_ids** → return 0.
- **Дубли lead_id** → дедуплицируются через `dict.fromkeys`.

### `main.py` lifespan по роли
- **`KURO_ROLE` не задан** → `all` (обратная совместимость, текущее поведение прода).
- **`KURO_ROLE` = мусор** → `role.py` нормализует в `all`.
- **api-роль, запрос к `/api/v1/calls`** → работает (роут смонтирован, БД доступна).
- **api-роль, callback originate** → 503 `AMI not connected` (известное ограничение, §4).
- **worker-роль, запрос к `/api/v1/calls`** → 404 (роут не смонтирован). Ожидаемо: воркер не обслуживает дашборд.

---

## 9. Test Scenarios

Файл: `backend/tests/test_ami_journal.py`. Стиль — как `test_calc_qualified_won.py` (sys.path.insert, pytest). Async-тесты требуют `pytest-asyncio` — он уже в dev-deps. **Добавь в `backend/pyproject.toml` секцию** (иначе async-тесты не запустятся в новой версии pytest-asyncio):

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

> Проверь: если существующие async-тесты (`amo_webhook` не тестируется, но на будущее) требуют этого — режим `auto` безопасен и не ломает синхронные тесты.

Тесты БД-логики journal мокают `async_session` (не поднимают реальную БД — смоук проверяет живую). Мокаем через monkeypatch на `app.services.ami_journal.async_session`.

| Test | Input | Expected | Type |
|------|-------|----------|------|
| `test_record_event_returns_id` | event dict, мок сессии возвращает id=7 | вернул 7, выполнен INSERT с pending | unit |
| `test_record_event_db_error_returns_none` | мок сессии бросает Exception | вернул None, не бросил | unit |
| `test_dispatch_journals_and_marks_done` | handler успешен | record_event → все хендлеры → mark_done | unit |
| `test_dispatch_marks_failed_on_handler_error` | handler бросает | mark_failed вызван, mark_done НЕ вызван | unit |
| `test_replay_calls_handler_per_event` | 3 pending события | handler вызван 3 раза с payload, mark_done ×3, возврат 3 | unit |
| `test_replay_idempotent_no_duplicate` | replay события, для которого `_handle_cdr` находит дубль по uniqueid | handler не бросает, mark_done, звонок НЕ создаётся повторно (проверяется через мок `_handle_cdr` / отсутствие второго INSERT в calls) | unit |
| `test_replay_failed_marks_failed` | handler бросает на 1 событии | mark_failed вызван для него, остальные done | unit |
| `test_cleanup_builds_correct_delete` | retention_days=7 | DELETE с `status='done'` и параметром days=7 | unit |
| `test_sync_recent_leads_dedups_and_limits` | 12 lead_ids c дублями, мок sync_lead | sync_lead вызван по числу уникальных, параллелизм ≤ 5 (счётчик пиковых одновременных) | unit |
| `test_role_default_all` | ENV без KURO_ROLE | `KURO_ROLE=="all"`, is_api и is_worker True | unit |
| `test_role_api` | KURO_ROLE=api | is_api True, is_worker False | unit |
| `test_role_worker` | KURO_ROLE=worker | is_api False, is_worker True | unit |
| `test_role_garbage_defaults_all` | KURO_ROLE=xxx | нормализуется в all | unit |

> Тесты роли: `role.py` читает ENV на импорте. Для теста используй `importlib.reload` после `monkeypatch.setenv("KURO_ROLE", ...)`, либо вынеси чтение в функцию `_read_role()` и тестируй её. Проще: тестировать `is_api()/is_worker()` через патч модульной переменной `app.core.role.KURO_ROLE`.

**Команда запуска тестов** (локально из `backend/`): `python -m pytest -q`. На проде смоук сам гоняет pytest (секция A, использует `venv/bin/python -m pytest`).

---

## П2.14 — детальный план миграции прода (БЕЗ даунтайма приёма звонков) — РЕВИЗИЯ 2026-07-02 под жёсткие прод-ограничения

> ⚠️ Эта версия ПОЛНОСТЬЮ заменяет предыдущий план П2.14. Изменения обязательны из-за прод-ограничений (проверено при разведке 2026-07-02, HEAD прода `04d57f5`).

### Прод-факты и ограничения (проверены на живом сервере)
- **`SHOW max_connections;` на 127.0.0.1:5433 → `100`** (НЕ 200 как в docker-compose). Постгрес — в docker-контейнере, пересоздать/переконфигурировать его нельзя без root/docker. Значит `max_connections` остаётся **100** навсегда в рамках этой задачи.
- **sudo/root НЕДОСТУПЕН вообще.** Следствия: nginx трогать нельзя (`nginx -t`/`reload` требуют root), системные сервисы не рестартить, постгрес-контейнер не пересоздавать. Работаем только `systemctl --user` под `alisher`.
- Текущая загрузка БД в покое: `SELECT count(*) FROM pg_stat_activity WHERE datname='kurotrack'` ≈ **14** коннектов. Один процесс с пулом 70 (30+40) в пике доходил до ~70.
- nginx уже проксирует `/api/` → `127.0.0.1:8102` и `/healthz` → `127.0.0.1:8102/api/v1/health`. **Не трогаем ни одну строку nginx.**
- Пул БД сейчас **захардкожен** в `backend/app/core/database.py`: `pool_size=30, max_overflow=40` (итого 70). Не читается из ENV. Чтобы дать двум процессам разные пулы — ДЕЛАЕМ размеры пула ENV-управляемыми (см. §4, правка `config.py` + `database.py`).

### Ключевое решение координатора (жёсткое, не обсуждается)
1. **API остаётся на порту 8102.** nginx уже смотрит туда — не меняем nginx вообще (нет root для reload). Значит именно **api-роль** должна слушать 8102.
2. **Worker уезжает на внутренний порт 8104** (публичный порт воркеру не нужен — nginx на него не ходит; 8104 нужен только для локального health-check воркера и смоука).
3. **Итог по ролям:**
   - `kurotrack-api.service` → порт **8102**, `KURO_ROLE=api`, обслуживает дашборд через уже настроенный nginx. `--workers 1` (обоснование ниже).
   - `kurotrack-worker.service` → порт **8104**, `KURO_ROLE=worker`, держит AMI + все фоновые воркеры + replay журнала. Публично не виден.

> Разворот портов относительно старого плана: раньше worker хотели оставить на 8102, а api вынести на 8103 и переключить nginx. Теперь nginx трогать НЕЛЬЗЯ (нет root), поэтому на «прибитый» к nginx порт 8102 садится тот, кто обслуживает публичный `/api/` — то есть api-роль. Worker переезжает на новый локальный 8104.

### Пулы БД под лимит 100 (утверждено с корректировкой)
Суммарный максимум коннектов обоих процессов должен быть ≤ 70 (тот же потолок, что у одного процесса сегодня), оставляя ~30 в резерве под сам постгрес, бэкапы (`pg_dump16`), psql-мониторинг и смоук.

Предложение координатора: worker `pool_size=25, max_overflow=25` (=50), api `pool_size=8, max_overflow=12` (=20). Итого 70.

**Критика (обязательная, CTO):**
- **Проблема:** uvicorn с `--workers N` форкает **N отдельных ОС-процессов**, каждый импортирует `app.main` заново → каждый создаёт **свой** SQLAlchemy `engine` со своим пулом. То есть api с `--workers 2` дал бы `2 × (8+12) = 40` коннектов, а не 20. Тогда worker 50 + api 40 = **90** — впритык к 100, любой всплеск воркера + бэкап + мониторинг = `TooManyConnectionsError`. Это скрытая мина в исходном предложении.
- **Решение:** api-роль запускаем **`--workers 1`** (один процесс). Дашборд смотрят 1-2 человека, read-only запросы лёгкие — одного uvicorn-воркера с пулом 20 более чем достаточно (async-конкурентность внутри одного процесса покрывает десятки одновременных запросов). Тогда api = ровно 8+12 = 20.
- **Итог (утверждено):** worker `pool_size=25, max_overflow=25` (=50) + api `pool_size=8, max_overflow=12` (=20) при `--workers 1` = **максимум 70 коннектов**. В покое реально будет ~14-20. Резерв до `max_connections=100` — 30. Смоук-порог `>= 80` не достигается даже в пике.

Значения задаются через ENV `KURO_DB_POOL_SIZE` / `KURO_DB_MAX_OVERFLOW`, выставляются в каждом systemd-юните через `Environment=` (см. §4 и юниты ниже). Дефолт в коде (если ENV не задан) — прежние 30/40 (=70), чтобы монолит `all` вёл себя как раньше.

### Изменения по коду относительно предыдущей версии спеки
Основной код П2.14 (`core/role.py`, условный lifespan и монтирование роутов в `main.py`, поле `role` в `/health`) — **без изменений**, см. §3 (`role.py`) и §4 (правка `main.py`). Дополнительно к §4 добавляются:
- `backend/app/core/config.py` — две новые настройки `db_pool_size: int = 30`, `db_max_overflow: int = 40` (префикс `KURO_` уже есть → читаются как `KURO_DB_POOL_SIZE` / `KURO_DB_MAX_OVERFLOW`).
- `backend/app/core/database.py` — `create_async_engine(... pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow, ...)` вместо литералов 30/40. Остальные параметры (pool_timeout, pre_ping, recycle, connect_args) НЕ трогать.

> Файл-оунершип: обе правки (`config.py`, `database.py`) идут одной задачей `P2.14-db-pool-env` (owner `sonnet-backend`), см. §10. Они не пересекаются с `main.py`/`role.py` — можно параллельно в той же волне.

### systemd-юниты (текст готов к копипасту)

`infra/systemd/kurotrack-worker.service` (обновлённый — `KURO_ROLE=worker`, порт **8104**, пул 25/25):

```ini
[Unit]
Description=KuroTrack AMI Worker (uvicorn, role=worker)
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/alisher/kurotrack/backend
EnvironmentFile=/home/alisher/kurotrack/backend/.env.worker
Environment=KURO_ROLE=worker
Environment=KURO_DB_POOL_SIZE=25
Environment=KURO_DB_MAX_OVERFLOW=25
LimitNOFILE=65536
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

`infra/systemd/kurotrack-api.service` (новый — `KURO_ROLE=api`, порт **8102**, `--workers 1`, пул 8/12):

```ini
[Unit]
Description=KuroTrack API (uvicorn, role=api)
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/alisher/kurotrack/backend
EnvironmentFile=/home/alisher/kurotrack/backend/.env.worker
Environment=KURO_ROLE=api
Environment=KURO_DB_POOL_SIZE=8
Environment=KURO_DB_MAX_OVERFLOW=12
LimitNOFILE=65536
MemoryHigh=400M
MemoryMax=600M
ExecStart=/home/alisher/kurotrack/backend/venv/bin/uvicorn app.main:app \
    --host 127.0.0.1 --port 8102 \
    --workers 1 --limit-max-requests 1000
StandardOutput=append:/home/alisher/kurotrack/logs/api.log
StandardError=append:/home/alisher/kurotrack/logs/api.log
SyslogIdentifier=kurotrack-api
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

> `--workers 1` в api-юните — ОСОЗНАННО (см. критику пулов выше): не менять на 2+, иначе каждый uvicorn-воркер создаст свой пул и суммарный лимит коннектов будет превышен. Горизонтальное масштабирование API (если когда-то понадобится) — отдельная задача с пересчётом пулов, здесь out of scope.
>
> Оба юнита используют один `.env.worker` (там DATABASE_URL/REDIS_URL/AMI/AMO — БЕЗ pool-переменных). Роль и размеры пула заданы через `Environment=` в самом юните и переопределяют что угодно из файла. Раздельные пулы получаются автоматически: это два разных процесса, engine создаётся при импорте с ENV-значениями своего юнита.

### nginx — НЕ ТРОГАЕМ (нет root)

`infra/nginx/kurotrack.conf` остаётся как есть: `/api/` → `127.0.0.1:8102`, `/healthz` → `127.0.0.1:8102/api/v1/health`. Никаких правок, никакого reload. Именно поэтому api-роль села на 8102 — чтобы nginx продолжал работать без изменений.

> Последствие для `/healthz`: nginx-эндпоинт `/healthz` теперь отражает состояние **api-процесса** (8102), а не воркера. `HealthResponse.ami_connected` там будет `false` (AMI живёт в воркере). Это ОК: `/healthz` для nginx/uptime-мониторинга проверяет «жив ли публичный API дашборда» — а он живёт именно в api-процессе на 8102. Живость воркера и AMI проверяет смоук через `127.0.0.1:8104` (см. правку смоука ниже) и отдельный монитор из P2.13-followup.

### Правка смоук-теста `scripts/smoke_test.sh` (обязательно в рамках П2.14)
Смоук сейчас проверяет worker health через `WORKER_URL="http://127.0.0.1:8102"` и требует `ami_connected:true`. После миграции на 8102 будет api-роль (без AMI), а воркер — на 8104. Нужно:
- L35: `WORKER_URL="http://127.0.0.1:8102"` → `WORKER_URL="http://127.0.0.1:8104"` (health/AMI-проверки секции B теперь ходят к воркеру на 8104).
- Публичный API (секция D, `PUBLIC_URL=https://kt.aiplus.kz`) остаётся без изменений — он через nginx попадёт в api-процесс на 8102, `ami_connected` там не проверяется.
- Порог `db connections >= 80` (L127) оставить как есть — при потолке 70 он не сработает, но служит ранним сигналом деградации.

> Файл-оунершип смоука: правка идёт задачей `P2.14-ops-configs`, owner `sonnet-infra`. Файл `scripts/smoke_test.sh` не трогается никакой другой задачей.

### Пошаговый план выката (каждый шаг — с проверкой и откатом)

> Все команды на проде под `alisher`, БЕЗ sudo. Управление — `systemctl --user`. nginx НЕ трогаем ни на одном шаге.

**Шаг 0 — подготовка (без влияния на прод).**
- Код P2.13+P2.15 уже в master (HEAD `04d57f5`). Смёржить P2.14 (role-split + ENV-пулы + новые юниты + правка смоука) в master, `git pull` на проде.
- Миграция `0006_ami_events` уже накачена в рамках P2.13 — новых миграций П2.14 не вводит. Проверка: `cd /home/alisher/kurotrack/backend && venv/bin/alembic current` → `0006`.
- Проверить `max_connections`: `PGPASSWORD=... psql -h 127.0.0.1 -p 5433 -U kuro -d kurotrack -Atc "SHOW max_connections;"` → должно быть `100`. Зафиксировать текущее число коннектов: `... "SELECT count(*) FROM pg_stat_activity WHERE datname='kurotrack';"` (ожидаем ~14-20).
- Откат этого шага: `git checkout <prev>` — кода на прод ещё не применяли (юниты не переставляли).

**Шаг 1 — перекатить код в текущем воркере, роль оставить `all` (пул тоже прежний 70).**
- Цель шага: убедиться, что новый код (role.py + условный lifespan + ENV-пулы) работает в режиме полной обратной совместимости, НЕ меняя топологию (всё ещё один процесс на 8102, nginx доволен).
- Обновить `~/.config/systemd/user/kurotrack-worker.service` из репо `infra/systemd/kurotrack-worker.service`, **НО** на этом шаге временно оставить старую конфигурацию: порт **8102**, БЕЗ `Environment=KURO_ROLE=worker` (или `=all`), БЕЗ pool-переменных (тогда дефолт 30/40=70). Проще: не переставлять юнит, а только `git pull` кода и `systemctl --user restart kurotrack-worker.service` — старый юнит запустит новый код в роли `all` на 8102.
- Проверка: `curl -s 127.0.0.1:8102/health` → `{"status":"ok","role":"all",...}`. В логах при старте: `KuroTrack starting with KURO_ROLE=all` и `AMI journal replay: reprocessed N events`. Смоук на этом шаге запускать НЕ из master (там уже 8104) — проверяй health вручную: `curl -s 127.0.0.1:8102/api/v1/health` → `ami_connected:true`.
- Приём звонков не прерывался (рестарт <5с, Asterisk держит TCP AMI, panoramisk переподключится; звонки в окно рестарта попадут в журнал `ami_events` при следующем событии — это уже работает с P2.13).
- Откат: `git checkout <prev> && systemctl --user restart kurotrack-worker.service`. Журнал `ami_events` остаётся (старый код его не трогает — он появился в P2.13, уже в master).

**Шаг 2 — развязка порта 8102: воркер съезжает на 8104, api занимает 8102.**

Оба процесса не могут слушать 8102 одновременно, поэтому шаги 2a→2b идут подряд без пауз. Единственное окно недоступности ДАШБОРДА — секунды между 2a и 2b. Приём ЗВОНКОВ не страдает (AMI в воркере, который в окне уже поднят).

**Шаг 2a — переставить воркер на 8104 в роль worker.**
- Скопировать финальный `infra/systemd/kurotrack-worker.service` (порт 8104, `KURO_ROLE=worker`, пул 25/25) → `~/.config/systemd/user/kurotrack-worker.service`.
- `systemctl --user daemon-reload && systemctl --user restart kurotrack-worker.service`.
- Проверка воркера: `curl -s 127.0.0.1:8104/health` → `{"role":"worker",...}`. `curl -s 127.0.0.1:8104/api/v1/health` → через ~2-5с `ami_connected:true`. `curl 127.0.0.1:8104/api/v1/calls/...` → 404 (бизнес-роуты сняты — ожидаемо). Логи: `KuroTrack starting with KURO_ROLE=worker`, `AMI journal replay: ...`.

**Шаг 2b — сразу поднять api-процесс на 8102.**
- Скопировать `infra/systemd/kurotrack-api.service` (порт 8102, `KURO_ROLE=api`, `--workers 1`, пул 8/12) → `~/.config/systemd/user/`.
- `systemctl --user daemon-reload && systemctl --user enable --now kurotrack-api.service`.
- Проверка: `curl -s 127.0.0.1:8102/health` → `{"role":"api",...}`. `curl -s 127.0.0.1:8102/api/v1/health` → JSON (`ami_connected:false` — норм для api). `curl "127.0.0.1:8102/api/v1/calls/?project_id=<id>&limit=1"` без токена → 401. `curl https://kt.aiplus.kz/api/v1/health` (через nginx) → 200. Дашборд открывается.
- Проверка пула БД: `psql ... "SELECT count(*) FROM pg_stat_activity WHERE datname='kurotrack';"` — должно быть заметно < 70 (реально ~20-40). Если ≥ 70 — стоп, разбор (возможна утечка коннектов или забыли ENV-пул).

> Нулевое окно недоступности дашборда недостижимо без правки nginx (нет root): два процесса не сядут на 8102 одновременно. Дашборд-даунтайм в секунды приемлем (его смотрят 1-2 человека, не клиенты). Приём звонков — главная ценность — не страдает.

**Шаг 3 — финальный смоук (уже с WORKER_URL=8104).**
- В master уже лежит смоук с `WORKER_URL=http://127.0.0.1:8104` (правка внесена в рамках P2.14). `git pull` на проде был на шаге 0 → файл актуален.
- `bash scripts/smoke_test.sh` → 0 FAIL. Секция B проверяет воркер на 8104 (`ami_connected:true`), секция D — публичный API через nginx (api-процесс на 8102).
- Проверка приёма звонков: в `logs/worker.log` идут `CDR saved: ...`. `SELECT count(*) FROM calls WHERE started_at > now() - interval '10 min';` растёт.

**Финальная проверка после всех шагов:**
- `bash scripts/smoke_test.sh` → 0 FAIL.
- `curl -s 127.0.0.1:8102/health` → `role: api`; `curl -s 127.0.0.1:8104/health` → `role: worker`.
- Дашборд через `https://kt.aiplus.kz` открывается, данные грузятся.
- 15 минут наблюдения `tail -f logs/worker.log` — `CDR saved` идут, `ami_connected:true`.
- `SELECT count(*) FROM calls WHERE started_at > now() - interval '15 min';` растёт.
- `SELECT status, count(*) FROM ami_events GROUP BY status;` — `done` растёт, `failed` ≈ 0.
- `SELECT count(*) FROM pg_stat_activity WHERE datname='kurotrack';` < 70 (обычно ~20-40).

**Полный откат к монолиту (в любой момент):**
1. `systemctl --user disable --now kurotrack-api.service`.
2. Вернуть `~/.config/systemd/user/kurotrack-worker.service` к старой версии: порт **8102**, роль `all` (убрать `Environment=KURO_ROLE=worker` и pool-переменные).
3. `systemctl --user daemon-reload && systemctl --user restart kurotrack-worker.service`.
- Через ~30с прод вернётся к исходному монолиту на 8102, nginx доволен (порт 8102 снова у процесса с бизнес-роутами). nginx не трогали ни на выкате, ни на откате.

### Риски П2.14 (самый инвазивный пункт)
- **R1 — двойной пул БД исчерпает Postgres (`max_connections=100`).** Митигация: ENV-пулы 25/25 (worker) + 8/12 (api) = максимум 70; api строго `--workers 1` (иначе пул удваивается на процесс). Смоук ловит `db connections >= 80`. Откат — disable api-юнит, вернуть воркер в `all`.
- **R2 — окно 502 на дашборде между шагами 2a и 2b (порт 8102 свободен).** Митигация: 2a и 2b выполняются подряд без пауз, окно — секунды; приём звонков не страдает (воркер уже поднят). Нулевое окно недостижимо без правки nginx (нет root).
- **R3 — api с `--workers 2+` превысит лимит коннектов.** Митигация: юнит зафиксирован на `--workers 1`; в спеке явный запрет менять без пересчёта пулов.
- **R4 — callback-originate в api-роли даёт 503** (`ami_client` не подключён в api). Митигация: known limitation, callback-трафик околонулевой; при необходимости callback проксировать на воркер 8104 (out of scope). Роут честно отдаёт 503, не молча падает.
- **R5 — AMI-реконнект после рестарта воркера теряет звонки в окне рестарта.** Митигация: журнал P2.13 (событие пишется при получении); окно рестарта <5с, разовое.
- **R6 — забыли ENV-пул → api взял дефолт 30/40, worker тоже.** Тогда суммарно 140 > 100 → `TooManyConnectionsError`. Митигация: `Environment=KURO_DB_POOL_SIZE/MAX_OVERFLOW` жёстко прописаны в ОБОИХ юнитах; проверка после шага 2b (`pg_stat_activity` < 70).
- **R7 — `/healthz` через nginx теперь бьёт в api (8102), `ami_connected:false`.** Митигация: это ожидаемо и корректно (`/healthz` = «жив ли публичный API»). Живость воркера/AMI отслеживает смоук на 8104 + монитор P2.13-followup. Задокументировано.

---

## П2.13 — выбор варианта (обоснование)

**Выбран Вариант А: таблица `ami_events` в Postgres.** Почему не Redis Stream (Вариант Б):
1. **Транзакционная согласованность.** Звонок и его статус живут в Postgres. Журнал в той же БД → один источник правды, replay читает ту же транзакционную БД, никаких рассинхронов Redis↔Postgres.
2. **Durability by default.** Postgres на диске с WAL. Redis на проде — `redisdata` volume, но AOF/RDB настройки неизвестны; для «не потерять звонок = деньги» диск-durable Postgres надёжнее без доп. конфигурации.
3. **Стек уже готов.** `async_session`, `text()`, JSONB, миграции alembic — всё есть. Redis Streams (XADD/consumer group/XAUTOCLAIM) — новый паттерн, больше кода и краевых случаев (pending entries list, ack, claim зависших) при том же результате.
4. **Отладка.** `SELECT * FROM ami_events WHERE status='failed'` — тривиальный разбор. В Redis Stream — сложнее.
5. **Нагрузка.** ~1500-6700 звонков/сутки ≈ до 3 событий на звонок (newchannel/hangup/cdr) ≈ макс ~20k INSERT/сутки ≈ <1 INSERT/сек в среднем, пики десятки/сек. Одиночный индексированный INSERT в Postgres это переваривает с запасом. Ретеншн 7 дней держит таблицу <150k строк.

Минус Варианта А (доп. запись в БД на каждое событие) компенсируется тем, что INSERT одиночный, в отдельной короткой сессии, с частичным индексом только по pending. Пул БД уже расширен (70 коннектов) и защищён retry (`_retry_handle_cdr`).

---

## Как проверить (сводно, + смоук)

### П2.13
- `venv/bin/alembic upgrade head` → таблица `ami_events` есть.
- Реальный звонок → `SELECT status, count(*) FROM ami_events GROUP BY status;` → `done` растёт.
- Тест рестарта: остановить воркер сразу после INSERT события (или вручную вставить `pending`-строку с валидным cdr-payload), запустить воркер → в логах `AMI journal replay: reprocessed N`, звонок появился в `calls` без дубля.
- `SELECT count(*) FROM ami_events WHERE status='failed';` ≈ 0.
- pytest: `python -m pytest tests/test_ami_journal.py -q` → зелёный.
- Смоук: `bash scripts/smoke_test.sh` → 0 FAIL (секция C проверяет свежие звонки).

### П2.15
- В логах воркера `AMO poll: synced N leads` идёт, итерация укладывается в 600с (нет наложения).
- AMO API не отдаёт 429 (rate-limit). Смоук секция F: AMO token 200.
- pytest: `test_sync_recent_leads_dedups_and_limits` зелёный.

### П2.14
- `curl 127.0.0.1:8102/health` → `role:api`; `curl 127.0.0.1:8104/health` → `role:worker`.
- Дашборд через `https://kt.aiplus.kz` работает.
- `bash scripts/smoke_test.sh` → 0 FAIL после КАЖДОГО шага миграции.
- `SELECT count(*) FROM pg_stat_activity WHERE datname='kurotrack';` < 80.

---

## Out of Scope
- Оптимизация массового replay (батчинг) при многочасовом даунтайме.
- Проксирование callback-originate на воркер в api-роли.
- Redis Streams (отклонён, см. обоснование).
- Изменение `max_connections` Postgres (требует docker/root; остаётся 100).
- Правка nginx / переключение портов через nginx (нет root; поэтому api сел на 8102).
- Горизонтальное масштабирование api (`--workers 2+`) — требует пересчёта пулов под лимит 100 (сейчас зафиксировано `--workers 1`).

> ВНИМАНИЕ: ENV-управляемые пулы БД (`KURO_DB_POOL_SIZE`/`KURO_DB_MAX_OVERFLOW`) и раздельные значения 25/25 (worker) + 8/12 (api) — теперь **В SCOPE** (задача `P2.14-db-pool-env`), в отличие от прошлой версии спеки.

---

## 10. Tasks JSON Block

```json
{
  "tasks": [
    {
      "id": "P2.13-migration",
      "description": "Миграция 0006_ami_events + ORM-модель AmiEvent + регистрация в models/__init__",
      "files": [
        "backend/migrations/versions/0006_ami_events.py",
        "backend/app/models/ami_event.py",
        "backend/app/models/__init__.py"
      ],
      "owner": "sonnet-backend",
      "wave": 1,
      "depends_on": [],
      "risk": "Низкий: новая пустая таблица, IF NOT EXISTS идемпотентно. Не трогает существующие таблицы.",
      "estimated_turns": 15,
      "acceptance": ["alembic upgrade head проходит", "revision 0006 down_revision 0005", "частичный индекс на pending", "AmiEvent в Base.metadata"],
      "status": "done"
    },
    {
      "id": "P2.13-journal-service",
      "description": "Сервис ami_journal: record_event/mark_done/mark_failed/replay_pending_events/cleanup_old_events/run_journal_cleanup_loop",
      "files": ["backend/app/services/ami_journal.py"],
      "owner": "sonnet-backend",
      "wave": 1,
      "depends_on": ["P2.13-migration"],
      "risk": "Средний: параметризованный SQL через text(), JSONB CAST, идемпотентность replay. Не бросать при сбое БД в record_event.",
      "estimated_turns": 30,
      "acceptance": ["Только параметризованный SQL", "record_event не бросает при сбое БД (возврат None)", "replay сортирует по received_at ASC, attempts<5", "cleanup удаляет done старше 7 дней"],
      "status": "done"
    },
    {
      "id": "P2.13-ami-integration",
      "description": "Интеграция журнала в ami_client: helper _dispatch_with_journal, замена циклов for handler в newchannel/hangup/cdr",
      "files": ["backend/app/services/ami_client.py"],
      "owner": "sonnet-backend",
      "wave": 2,
      "depends_on": ["P2.13-journal-service"],
      "risk": "Средний: точка входа всех событий. Ошибка ломает приём звонков. Journal — страховка, обработка не должна блокироваться при None event_id.",
      "estimated_turns": 20,
      "acceptance": ["event пишется в журнал ДО обработки", "handler-исключение → mark_failed", "успех → mark_done", "event_id None не ломает обработку"],
      "status": "done"
    },
    {
      "id": "P2.15-amo-polling",
      "description": "AMO polling: _LOOKBACK_HOURS=4, семафор+пауза в sync_recent_leads под rate-limit",
      "files": ["backend/app/workers/amo_poll.py", "backend/app/services/amo_sync.py"],
      "owner": "sonnet-backend-2",
      "wave": 1,
      "depends_on": [],
      "risk": "Низкий: изолированные файлы (amo_*), не пересекаются с ami_client/call_processor. Webhook — основной канал, не трогается.",
      "estimated_turns": 20,
      "acceptance": ["_LOOKBACK_HOURS=4", "параллелизм <=5 через семафор", "дедуп lead_ids", "пауза _POLL_PAUSE_SEC под rate-limit"],
      "status": "done"
    },
    {
      "id": "P2.14-db-pool-env",
      "description": "ENV-управляемые размеры пула БД: config.py (db_pool_size/db_max_overflow) + database.py (pool_size/max_overflow из settings вместо литералов 30/40)",
      "files": ["backend/app/core/config.py", "backend/app/core/database.py"],
      "owner": "sonnet-backend-2",
      "wave": 3,
      "depends_on": [],
      "risk": "Средний: меняет создание engine (глобальный ресурс). Дефолт 30/40=70 сохраняет поведение монолита. Раздельные пулы (25/25 worker, 8/12 api) задаются ENV в systemd-юнитах, не в коде.",
      "estimated_turns": 15,
      "acceptance": ["config.py: db_pool_size=30, db_max_overflow=40 (дефолт)", "database.py читает pool_size/max_overflow из settings", "прочие параметры engine (pool_timeout/pre_ping/recycle/connect_args) не тронуты", "без ENV поведение = прежнее 70"],
      "status": "pending"
    },
    {
      "id": "P2.14-role-lifespan",
      "description": "core/role.py + условный lifespan и монтирование роутов в main.py по KURO_ROLE; replay+cleanup в worker-роли; поле role в /health",
      "files": ["backend/app/core/role.py", "backend/app/main.py"],
      "owner": "sonnet-backend",
      "wave": 3,
      "depends_on": ["P2.13-ami-integration", "P2.13-journal-service"],
      "risk": "Высокий: меняет запуск всего приложения. Дефолт all = полная обратная совместимость. Health монтируется всегда.",
      "estimated_turns": 30,
      "acceptance": ["KURO_ROLE дефолт all", "api не поднимает AMI/воркеры", "worker монтирует только health", "replay+cleanup запускаются в worker/all", "/health отдаёт role"],
      "status": "pending"
    },
    {
      "id": "P2.14-ops-configs",
      "description": "systemd-юниты kurotrack-api (8102, role=api, --workers 1, пул 8/12) и kurotrack-worker (8104, role=worker, пул 25/25) с Environment=KURO_DB_POOL_SIZE/MAX_OVERFLOW + правка smoke_test.sh WORKER_URL 8102->8104. nginx НЕ трогается (нет root).",
      "files": [
        "infra/systemd/kurotrack-api.service",
        "infra/systemd/kurotrack-worker.service",
        "scripts/smoke_test.sh"
      ],
      "owner": "sonnet-infra",
      "wave": 3,
      "depends_on": ["P2.14-role-lifespan", "P2.14-db-pool-env"],
      "risk": "Средний: конфиги в репо (не применяются автоматически на прод). Реальный выкат — по пошаговому плану миграции человеком. nginx НЕ трогаем — api сел на уже-проксируемый 8102. Забыть ENV-пул -> суммарно 140>100 коннектов (R6).",
      "estimated_turns": 15,
      "acceptance": ["api-юнит: порт 8102, role=api, --workers 1, KURO_DB_POOL_SIZE=8 KURO_DB_MAX_OVERFLOW=12", "worker-юнит: порт 8104, role=worker, KURO_DB_POOL_SIZE=25 KURO_DB_MAX_OVERFLOW=25", "smoke_test.sh WORKER_URL=127.0.0.1:8104", "infra/nginx/kurotrack.conf НЕ изменён"],
      "status": "pending"
    },
    {
      "id": "P2-tests",
      "description": "Unit-тесты: журнал (record/dispatch/replay идемпотентность/cleanup), sync_recent_leads лимиты, role helpers; pytest asyncio_mode=auto",
      "files": ["backend/tests/test_ami_journal.py", "backend/pyproject.toml"],
      "owner": "sonnet-tester",
      "wave": 4,
      "depends_on": ["P2.13-ami-integration", "P2.15-amo-polling", "P2.14-role-lifespan"],
      "risk": "Низкий: мокаем async_session, реальную БД не поднимаем. Не менять существующие тесты.",
      "estimated_turns": 30,
      "acceptance": ["replay идемпотентен (не плодит звонки)", "sync_recent_leads параллелизм<=5", "role helpers покрыты", "asyncio_mode=auto добавлен", "python -m pytest -q зелёный"],
      "status": "pending"
    }
  ]
}
```

### Раскладка волн (пояснение к JSON)

- **Wave 1 (фундамент, параллельно):** `P2.13-migration`, `P2.13-journal-service` (зависит от migration, но в той же волне последовательно — один owner `sonnet-backend`), `P2.15-amo-polling` (owner `sonnet-backend-2`, другие файлы: `amo_poll.py`+`amo_sync.py` — **не пересекаются** с `ami_client.py`/`call_processor.py`). Приоритет №1 — журнал: максимум надёжности при изолированном риске.
- **Wave 2:** `P2.13-ami-integration` (`ami_client.py`) — зависит от сервиса журнала.
- **Wave 3:** `P2.14-db-pool-env` (`config.py`+`database.py`, owner `sonnet-backend-2`), `P2.14-role-lifespan` (`main.py`+`role.py`, owner `sonnet-backend`) и `P2.14-ops-configs` (`infra/systemd/*` + `scripts/smoke_test.sh`, owner `sonnet-infra`). Файлы не пересекаются между тремя owner'ами. `P2.14-ops-configs` зависит от обоих кодовых (юниты используют ENV-пулы и роли). nginx НЕ трогается.
- **Wave 4:** `P2-tests` — после всего кода.

### File Ownership (нет конфликтов внутри волны)
- Wave 1: `sonnet-backend` владеет `0006_*.py`, `ami_event.py`, `models/__init__.py`, `ami_journal.py`; `sonnet-backend-2` владеет `amo_poll.py`, `amo_sync.py`. Пересечений нет.
- Wave 2: `sonnet-backend` — `ami_client.py`.
- Wave 3: `sonnet-backend` — `main.py`, `role.py`; `sonnet-backend-2` — `core/config.py`, `core/database.py`; `sonnet-infra` — `infra/systemd/*`, `scripts/smoke_test.sh`. Пересечений нет (`config.py`/`database.py` не трогают `main.py`/`role.py`). `infra/nginx/kurotrack.conf` НЕ модифицируется ни одной задачей.
- Wave 4: `sonnet-tester` — `test_ami_journal.py`, `pyproject.toml`.
- `call_processor.py` НЕ модифицируется ни одной задачей (журнал ведёт ami_client; process_call_event уже идемпотентен) — исключает конфликт с P1-правками.
```
