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
9. Два systemd user-юнита готовы: `kurotrack-api.service` (порт 8103, `KURO_ROLE=api`, `--workers 2`) и обновлённый `kurotrack-worker.service` (порт 8102, `KURO_ROLE=worker`). nginx переключён на `127.0.0.1:8103` для `/api/` (после миграции).
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
| `infra/nginx/kurotrack.conf` | (только на шаге миграции П2.14) `proxy_pass` 8102 → 8103 в `location /api/`; `/healthz` оставить на воркере 8102 или тоже 8103 (см. §П2.14) | L34, L52 |

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

`GET /api/v1/health` (роутер `health.py`) — контракт БЕЗ изменений (`HealthResponse`: status, service, ami_connected, db_ok, redis_ok). В роли `api` поле `ami_connected` будет `false` (AMI не поднят) — это ожидаемо, смоук проверяет `ami_connected` только для воркера через `127.0.0.1:8102`.

Ошибки: без изменений. `/health` никогда не 5xx.

---

## 7. Frontend Contract

Изменений во фронтенде НЕТ. Дашборд обращается к тем же `/api/v1/*` эндпоинтам через nginx. После миграции П2.14 nginx проксирует `/api/` на api-процесс (8103) вместо воркера (8102) — для фронтенда прозрачно, URL не меняется.

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

## П2.14 — детальный план миграции прода (БЕЗ даунтайма приёма звонков)

### Проблема
Один процесс держит и HTTP-API дашборда, и AMI-обработку. Нельзя масштабировать API и нельзя рестартовать API-часть, не роняя приём звонков.

### Решение
Роль через ENV. Прод разводим на два user-юнита: `kurotrack-worker.service` (8102, `KURO_ROLE=worker`, держит AMI+воркеры) и `kurotrack-api.service` (8103, `KURO_ROLE=api`, `--workers 2`, обслуживает дашборд). nginx `/api/` → 8103.

### systemd-юниты (текст готов к копипасту)

`infra/systemd/kurotrack-worker.service` (обновлённый — добавлен `KURO_ROLE=worker`):

```ini
[Unit]
Description=KuroTrack AMI Worker (uvicorn, role=worker)
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/alisher/kurotrack/backend
EnvironmentFile=/home/alisher/kurotrack/backend/.env.worker
Environment=KURO_ROLE=worker
LimitNOFILE=65536
MemoryHigh=500M
MemoryMax=700M
ExecStart=/home/alisher/kurotrack/backend/venv/bin/uvicorn app.main:app \
    --host 127.0.0.1 --port 8102 \
    --limit-max-requests 1000
StandardOutput=append:/home/alisher/kurotrack/logs/worker.log
StandardError=append:/home/alisher/kurotrack/logs/worker.log
SyslogIdentifier=kurotrack-worker
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

`infra/systemd/kurotrack-api.service` (новый):

```ini
[Unit]
Description=KuroTrack API (uvicorn, role=api)
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
    --host 127.0.0.1 --port 8103 \
    --workers 2 --limit-max-requests 1000
StandardOutput=append:/home/alisher/kurotrack/logs/api.log
StandardError=append:/home/alisher/kurotrack/logs/api.log
SyslogIdentifier=kurotrack-api
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

> Оба используют один `.env.worker` (там DATABASE_URL/REDIS_URL/AMI/AMO). `KURO_ROLE` задан через `Environment=` в юните (переопределяет ENV из файла, если бы там был). Раздельные пулы БД получаются автоматически: это два разных процесса, каждый создаёт свой `engine` (pool_size=30+overflow=40). Итого при обоих запущенных: worker до 70 + api до 70 = 140 коннектов теоретический максимум. **Прод Postgres `max_connections`**: проверь перед выкатом (`SHOW max_connections;` на 5433). docker-compose ставит 200 — если прод так же, запас есть. Если меньше — уменьши `pool_size` api-юнита через отдельный ENV (out of scope, отметить в отчёте).

### nginx (правка только на шаге 5 миграции)

`infra/nginx/kurotrack.conf`:
- L34: `proxy_pass http://127.0.0.1:8102;` → `proxy_pass http://127.0.0.1:8103;` (location `/api/`).
- L52: `/healthz` → оставить на воркере `http://127.0.0.1:8102/api/v1/health` (чтобы healthz отражал состояние AMI/воркера, который критичнее). Не менять.

Применение (требует root, делает админ — см. `infra/ADMIN-DEPLOY.md`): `sudo nginx -t && sudo systemctl reload nginx`. Если symlink — правка файла в репо + reload.

### Пошаговый план выката (каждый шаг — с проверкой и откатом)

> Все команды на проде под `alisher`, БЕЗ sudo (кроме nginx reload — эскалация к админу). Управление — `systemctl --user`.

**Шаг 0 — подготовка (без влияния на прод).**
- Смёржить код P2.13+P2.15+P2.14 в master, `git pull` на проде.
- Накатить миграцию: `cd /home/alisher/kurotrack/backend && venv/bin/alembic upgrade head`.
- Проверка: `venv/bin/alembic current` показывает `0006`. `psql ... -c "\d ami_events"` — таблица есть.
- Откат: `venv/bin/alembic downgrade 0005` (дропнет пустую таблицу).

**Шаг 1 — перезапустить существующий воркер в роли worker (журнал + новый lifespan, всё ещё один процесс отдаёт и API).**
- Обновить юнит: скопировать `infra/systemd/kurotrack-worker.service` → `~/.config/systemd/user/kurotrack-worker.service`. **НО**: на этом шаге НЕ ставить `KURO_ROLE=worker` — оставить дефолт `all`, чтобы дашборд продолжал работать через 8102. То есть на шаге 1 применяем ТОЛЬКО код (журнал+lifespan), роль остаётся `all`. Для этого временно НЕ добавляй `Environment=KURO_ROLE=worker` (или задай `=all`).
- `systemctl --user daemon-reload && systemctl --user restart kurotrack-worker.service`.
- Проверка: `curl -s 127.0.0.1:8102/health` → `role: all`. `bash scripts/smoke_test.sh` → 0 FAIL. В логах: `AMI journal replay: reprocessed N events` (может быть 0). Приём звонков не прерывался (рестарт <5с, Asterisk держит TCP AMI, panoramisk переподключится; звонки в момент рестарта — если были — попадут в журнал при следующем событии или потеряются только те, что пришли в окно рестарта — но это разовое окно, и оно уже было при любом прошлом рестарте).
- Откат: `git checkout <prev> && restart`. Журнал остаётся (не мешает старому коду — старый код таблицу не трогает).

**Шаг 2 — поднять api-процесс на 8103 параллельно (nginx ещё на 8102).**
- Скопировать `infra/systemd/kurotrack-api.service` → `~/.config/systemd/user/`.
- `systemctl --user daemon-reload && systemctl --user enable --now kurotrack-api.service`.
- Проверка: `curl -s 127.0.0.1:8103/health` → `role: api`. `curl -s 127.0.0.1:8103/api/v1/health` → JSON (`ami_connected:false` — норм для api). `curl "127.0.0.1:8103/api/v1/calls/?project_id=<id>&limit=1"` без токена → 401 (авторизация жива). `curl 127.0.0.1:8103/api/v1/projects` (с токеном) → данные.
- Проверка пула БД: `psql ... -c "SELECT count(*) FROM pg_stat_activity WHERE datname='kurotrack';"` — должно быть < 80 (смоук порог). Если близко к лимиту — стоп, разбор.
- Откат: `systemctl --user disable --now kurotrack-api.service`.

**Шаг 3 — переключить воркер в чистую роль worker (перестаёт отдавать бизнес-роуты).**
- ВНИМАНИЕ: сначала должен быть готов шаг 4 (nginx на 8103), иначе дашборд отвалится. Поэтому шаг 3 и 4 делаем в связке, но nginx-reload — последним.
- Обновить `~/.config/systemd/user/kurotrack-worker.service`: раскомментировать/добавить `Environment=KURO_ROLE=worker`.
- **НЕ рестартить воркер, пока nginx смотрит на 8102** — иначе `/api/` через воркер начнёт отдавать 404. Порядок: сперва шаг 4 (nginx → 8103), затем рестарт воркера в роль worker.

**Шаг 4 — переключить nginx на api-процесс (8103).**
- Правка `infra/nginx/kurotrack.conf` L34 → 8103 (см. выше). `/healthz` оставить 8102.
- Эскалация админу (root): `sudo nginx -t && sudo systemctl reload nginx`.
- Проверка: `curl -s https://kt.aiplus.kz/api/v1/health` → 200. Дашборд открывается. `curl https://kt.aiplus.kz/api/v1/calls/?...` без токена → 401.
- Откат: вернуть L34 → 8102, `sudo systemctl reload nginx`.

**Шаг 5 — рестарт воркера в роль worker (после того как nginx уже на 8103).**
- `systemctl --user daemon-reload && systemctl --user restart kurotrack-worker.service`.
- Проверка: `curl -s 127.0.0.1:8102/health` → `role: worker`. `curl 127.0.0.1:8102/api/v1/calls/...` → 404 (роуты сняты — ок). `curl 127.0.0.1:8102/api/v1/health` → 200 с `ami_connected:true` (после реконнекта AMI, ~2-5с). `bash scripts/smoke_test.sh` → 0 FAIL (смоук проверяет worker health на 8102 и public API на kt.aiplus.kz который теперь через 8103).
- Проверка приёма звонков: в `logs/worker.log` появляются `CDR saved: ...`. В БД свежие звонки: `SELECT count(*) FROM calls WHERE started_at > now() - interval '10 min';`.
- Откат (полный откат к монолиту): вернуть worker-юнит на `KURO_ROLE=all` (или убрать `Environment=`), nginx L34 → 8102, `systemctl --user disable --now kurotrack-api.service`, оба reload. Через 30с прод вернётся к исходному состоянию.

**Финальная проверка после всех шагов:**
- `bash scripts/smoke_test.sh` → 0 FAIL.
- 15 минут наблюдения `tail -f logs/worker.log` — `CDR saved` идут, `ami_connected:true`.
- `SELECT count(*) FROM calls WHERE started_at > now() - interval '15 min';` растёт.
- `SELECT status, count(*) FROM ami_events GROUP BY status;` — `done` растёт, `failed` ≈ 0.

### Риски П2.14 (самый инвазивный пункт)
- **R1 — двойной пул БД исчерпает Postgres.** Митигация: проверить `max_connections` на 5433 перед шагом 2; смоук ловит `db connections >= 80`. Откат — disable api-юнит.
- **R2 — nginx переключён раньше, чем api готов.** Митигация: строгий порядок шагов (api поднят и проверен на шаге 2 ДО nginx-reload на шаге 4).
- **R3 — воркер в роли worker перестал отдавать `/api/`, а nginx ещё на 8102.** Митигация: шаг 5 (рестарт воркера в worker) строго ПОСЛЕ шага 4 (nginx→8103).
- **R4 — callback-originate в api-роли даёт 503.** Митигация: known limitation, callback-трафик околонулевой; при необходимости callback-роут проксировать на воркер (out of scope).
- **R5 — AMI-реконнект после рестарта воркера теряет звонки в окне рестарта.** Митигация: журнал P2.13 (событие пишется при получении); окно рестарта <5с, разовое.

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
- `curl 127.0.0.1:8102/health` → `role:worker`; `curl 127.0.0.1:8103/health` → `role:api`.
- Дашборд через `https://kt.aiplus.kz` работает.
- `bash scripts/smoke_test.sh` → 0 FAIL после КАЖДОГО шага миграции.
- `SELECT count(*) FROM pg_stat_activity WHERE datname='kurotrack';` < 80.

---

## Out of Scope
- Оптимизация массового replay (батчинг) при многочасовом даунтайме.
- Проксирование callback-originate на воркер в api-роли.
- Redis Streams (отклонён, см. обоснование).
- Изменение `max_connections` Postgres (требует docker/root).
- Тюнинг раздельных пулов БД под api/worker (отдельные ENV-переменные для pool_size).

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
      "status": "pending"
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
      "status": "pending"
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
      "status": "pending"
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
      "id": "P2.14-systemd-nginx",
      "description": "systemd-юниты kurotrack-api/kurotrack-worker (эталонный текст в infra/systemd) + правка nginx proxy_pass на 8103 (применяется при миграции)",
      "files": [
        "infra/systemd/kurotrack-api.service",
        "infra/systemd/kurotrack-worker.service",
        "infra/nginx/kurotrack.conf"
      ],
      "owner": "sonnet-infra",
      "wave": 3,
      "depends_on": ["P2.14-role-lifespan"],
      "risk": "Средний: конфиги в репо (не применяются автоматически на прод). Реальный выкат — по пошаговому плану миграции человеком/деплоером. nginx-reload требует root (админ).",
      "estimated_turns": 15,
      "acceptance": ["api-юнит порт 8103 role=api --workers 2", "worker-юнит порт 8102 role=worker", "nginx /api/ -> 8103, /healthz -> 8102"],
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
- **Wave 3:** `P2.14-role-lifespan` (`main.py`+`role.py`) и `P2.14-systemd-nginx` (infra) — последний, самый инвазивный; зависит от стабильности журнала (worker-роль должна корректно поднимать replay).
- **Wave 4:** `P2-tests` — после всего кода.

### File Ownership (нет конфликтов внутри волны)
- Wave 1: `sonnet-backend` владеет `0006_*.py`, `ami_event.py`, `models/__init__.py`, `ami_journal.py`; `sonnet-backend-2` владеет `amo_poll.py`, `amo_sync.py`. Пересечений нет.
- Wave 2: `sonnet-backend` — `ami_client.py`.
- Wave 3: `sonnet-backend` — `main.py`, `role.py`; `sonnet-infra` — `infra/systemd/*`, `nginx`. Пересечений нет.
- Wave 4: `sonnet-tester` — `test_ami_journal.py`, `pyproject.toml`.
- `call_processor.py` НЕ модифицируется ни одной задачей (журнал ведёт ami_client; process_call_event уже идемпотентен) — исключает конфликт с P1-правками.
```
