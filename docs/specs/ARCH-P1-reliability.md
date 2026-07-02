# ARCH-P1-reliability — блок надёжности KuroTrack

Автор: CTO (Opus). Исполнители: Sonnet-разработчики.
Ветка: `fix/call-tracking-persistence` (базовый коммит `fc30d03`).
Репозиторий: `/Users/baigelenov/projects/kurotrack`.

> ВАЖНО для исполнителей: комментарии в коде — на русском. SQL только параметризованный.
> Не рефакторить лишнего. Каждый пункт — отдельная задача (см. JSON-блок в конце).

---

## 1. Summary

Закрываем 7 пунктов надёжности (П1.6–П1.12) на воркере обработки звонков KuroTrack:
расширяем retry на все транзиентные сбои БД (не только `TooManyConnections`), убираем
`sleep(3)` из-под CDR-семафора (залп legs забивал слоты), делаем детерминированный
Redis-fallback для round-robin города (сейчас все лиды при сбое Redis валятся в «Алматы»),
переносим логи из `/tmp` в постоянную папку с ротацией без root, добавляем тесты
(round-robin, retry, smoke-контракт amocrm_client), удаляем мёртвые cron-скрипты и
выполняем merge fix-ветки в master с планом отката.

Результат: воркер не теряет CDR при кратковременных проблемах пула БД, не давится
собственными паузами, равномерно раскидывает лиды по городам при сбое Redis, пишет
логи в надёжное место с ограниченным размером, покрыт регрессионными тестами, а
кодовая база очищена от мёртвого кода и влита в master.

---

## 2. Acceptance Criteria

1. `_retry_handle_cdr` ретраит `TooManyConnectionsError`, `sqlalchemy.exc.TimeoutError`,
   `sqlalchemy.exc.OperationalError`, `asyncpg.exceptions.ConnectionDoesNotExistError`,
   `asyncpg.exceptions.PostgresConnectionError` — включая случаи, когда asyncpg-исключение
   обёрнуто SQLAlchemy в `DBAPIError` (проверяется через `e.orig`). `IntegrityError` НЕ
   ретраится (пробрасывается / считается «дубль обработан»).
2. Внутри `_cdr_semaphore` больше НЕТ `await asyncio.sleep(3)`. Оба ожидания промаха
   lock-а заменены на короткий SQL-поллинг (3 итерации × 1 с) с ранним выходом при
   нахождении лида.
3. `_next_round_robin_city(caller)` при исключении Redis возвращает
   `_ROUND_ROBIN_CITIES[crc32(caller) % 5]` (детерминированно и стабильно между
   процессами). Все вызовы передают `caller`.
4. Логи воркера и скриптов пишутся в `/home/alisher/kurotrack/logs/` (не в `/tmp`).
   systemd-юнит воркера пишет в `append:/home/alisher/kurotrack/logs/worker.log`.
   `monitor.py` при чтении лога использует новый путь и усекает файл до 20 МБ, если
   он превысил 50 МБ.
5. Есть 3 файла тестов: `test_round_robin_city.py`, `test_retry_handle_cdr.py`,
   `test_amocrm_interface.py`. `pytest backend/tests` проходит (включая существующие).
6. Удалены файлы `scripts/auto_assign_leads.py`, `scripts/run_auto_assign.sh`,
   `scripts/cleanup_drugoy_city.py`, `scripts/run_cleanup_drugoy.sh`. Нет ссылок на них
   в live-коде (кроме `.claude/worktrees/` — это чужие изолированные копии, их не трогаем).
7. `master` содержит все коммиты fix-ветки (fast-forward), запушен в GitHub, на сервере
   воркер перезапущен и `/health` отдаёт 200. Есть письменный план отката.

---

## 3. Files to Create

| Path | Purpose | Ключевое содержимое |
|------|---------|--------------------|
| `backend/tests/test_round_robin_city.py` | Unit-тест `_next_round_robin_city` | `async def test_*`, мок `redis_client.incr` через `AsyncMock`, проверка fallback по crc32 |
| `backend/tests/test_retry_handle_cdr.py` | Unit-тест `_retry_handle_cdr` | Мок `_handle_cdr` (`AsyncMock` с `side_effect`), мок `asyncio.sleep`, проверка числа попыток по типу исключения |
| `backend/tests/test_amocrm_interface.py` | Smoke-контракт: все `amocrm_client.X(` из воркеров существуют | AST-парсинг `call_processor.py` + `reconciliation.py`, `assert hasattr(AmoCRMClient, name)` |

Новых production-файлов не создаём. `logs/` создаётся на сервере командой (см. П1.9),
в git добавляется через `.gitignore` + `.gitkeep` (см. П1.9).

---

## 4. Files to Modify

| Path | Что меняется | Строки (ориентир) |
|------|-------------|-------------------|
| `backend/app/workers/call_processor.py` | П1.6: импорты + тело `_retry_handle_cdr`. П1.7: убрать `sleep(3)` в `_push_to_amo` (L253-254) и в `_handle_cdr` (L393-398), заменить на поллинг | L24-25, L248-277, L321-345, L386-398 |
| `backend/app/services/amocrm.py` | П1.8: `import zlib` (или `binascii`), сигнатура `_next_round_robin_city(self, caller: str)`, fallback crc32; правка единственного вызова на L337 | L7-9, L164-173, L335-337 |
| `backend/pyproject.toml` | П1.10: секция `[tool.pytest.ini_options]` c `asyncio_mode = "auto"` | после L27 (конец `dev` deps) |
| `.gitignore` | П1.9: игнор `logs/*.log`, но хранить папку через `.gitkeep` | добавить в конец |
| `scripts/monitor.py` | П1.9: путь лога `/home/alisher/kurotrack/logs/worker.log` + trim >50МБ | L141, +новая функция |
| `scripts/backup_db.sh` | П1.9 (г): вывод контроля в лог новой папки не требуется (пишет в stdout cron) — НЕ меняем, см. примечание | — |

Файлы к удалению — см. П1.11.

---

## 5. Database Changes

Отсутствуют. Ни один пункт П1.6–П1.12 не меняет схему БД, не добавляет таблиц,
индексов или миграций. Раздел оставлен пустым намеренно.

---

## 6. API Contract

Изменений внешнего HTTP-API нет. Затрагиваются только внутренние функции воркера и
CRM-клиента. Health-эндпоинт `/health` (в `backend/app/main.py`, L85-91) используется
как критерий готовности при merge (П1.12) — его контракт не меняется:

```json
{"status": "ok", "rss_mb": 000, "uptime_s": 000}
```

---

## 7. Frontend Contract

Не затрагивается. Фронтенд-изменений нет.

---

## П1.6 — Retry на все сбои БД

### Контекст (реальный код)

`backend/app/workers/call_processor.py`:
- L24-25:
  ```python
  # asyncpg исключения для retry-логики на TooManyConnections
  from asyncpg.exceptions import TooManyConnectionsError
  ```
- L321-345 — текущая `_retry_handle_cdr`, ловит ТОЛЬКО `TooManyConnectionsError`.

Движок БД (`backend/app/core/database.py`): asyncpg + SQLAlchemy async, `pool_timeout=30`,
`pool_pre_ping=True`. Ошибки пула SQLAlchemy бросает как `sqlalchemy.exc.TimeoutError`
(истёк `pool_timeout`). Ошибки драйвера (asyncpg) при выполнении SQL SQLAlchemy оборачивает
в `sqlalchemy.exc.DBAPIError` (частный случай — `OperationalError`), где оригинальное
asyncpg-исключение лежит в атрибуте `e.orig`. Поэтому `TooManyConnectionsError` может
прилететь как «голое» (при установке соединения на pre-ping — редко) ИЛИ обёрнутое в
`OperationalError` с `orig=TooManyConnectionsError(...)`. Ретраим оба варианта.

### Требуемое поведение

Ретраятся (транзиентные): `asyncpg.TooManyConnectionsError`,
`asyncpg.ConnectionDoesNotExistError`, `asyncpg.PostgresConnectionError`,
`sqlalchemy.exc.TimeoutError`, `sqlalchemy.exc.OperationalError`.
`sqlalchemy.exc.IntegrityError` НЕ ретраится (дубль — это норма, `_handle_cdr` его уже
глотает внутри; но если долетит — пробрасываем без ретраев).

Замечание: `asyncpg.PostgresConnectionError` — базовый класс для
`ConnectionDoesNotExistError` и `CannotConnectNowError`; перечисляем его отдельно, потому
что `TooManyConnectionsError` НЕ является его подклассом (он потомок
`InsufficientResourcesError`). `OperationalError` — потомок `DBAPIError`, а `TimeoutError`
(sqlalchemy) — потомок `SQLAlchemyError`, но НЕ `DBAPIError`, поэтому оба указываем явно.
Важный нюанс: `IntegrityError` — тоже потомок `DBAPIError`, поэтому НЕЛЬЗЯ ловить широкий
`DBAPIError`; ловим точечно и дополнительно проверяем `e.orig`.

### Точный код (заменить L24-25 и L321-345)

Заменить блок импортов asyncpg (L24-25) на:

```python
# Исключения БД для retry-логики: транзиентные сбои пула/соединения ретраим,
# IntegrityError (дубль) — НЕТ.
from asyncpg.exceptions import (
    TooManyConnectionsError,
    ConnectionDoesNotExistError,
    PostgresConnectionError,
)
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    OperationalError,
    TimeoutError as SATimeoutError,
)
```

> Примечание: `sqlalchemy.exc.TimeoutError` переименован в импорте в `SATimeoutError`,
> чтобы не затенять встроенный `TimeoutError`. В `_handle_cdr` (L508) уже есть локальный
> `from sqlalchemy.exc import IntegrityError` внутри `except` — его можно оставить как есть
> (не трогаем `_handle_cdr`), дублирующий импорт на уровне модуля не мешает.

Заменить всю функцию `_retry_handle_cdr` (L321-345) на:

```python
# Транзиентные исключения БД, которые имеет смысл ретраить.
# ВАЖНО: IntegrityError сюда НЕ входит — дубль это норма, ретрай его не исправит.
_RETRIABLE_DB_ERRORS: tuple[type[Exception], ...] = (
    TooManyConnectionsError,
    ConnectionDoesNotExistError,
    PostgresConnectionError,
    OperationalError,
    SATimeoutError,
)


def _is_retriable_db_error(exc: Exception) -> bool:
    """Транзиентный ли это сбой БД (стоит ретраить)?

    IntegrityError (дубль) НЕ ретраим — сразу False, даже если он потомок DBAPIError.
    Иначе проверяем сам exc и распакованный e.orig (asyncpg-исключение, обёрнутое
    SQLAlchemy в DBAPIError/OperationalError).
    """
    if isinstance(exc, IntegrityError):
        return False
    if isinstance(exc, _RETRIABLE_DB_ERRORS):
        return True
    # SQLAlchemy оборачивает драйверные ошибки в DBAPIError, оригинал — в .orig
    if isinstance(exc, DBAPIError) and exc.orig is not None:
        return isinstance(exc.orig, _RETRIABLE_DB_ERRORS)
    return False


async def _retry_handle_cdr(event: dict, max_attempts: int = 3) -> None:
    """Retry-обёртка для _handle_cdr с backoff на транзиентных сбоях БД.

    Ретраим: TooManyConnections, TimeoutError пула, OperationalError,
    ConnectionDoesNotExist, PostgresConnectionError (в т.ч. обёрнутые в DBAPIError.orig).
    НЕ ретраим: IntegrityError (дубль) и любые другие исключения — пробрасываем сразу.

    Семафор ограничивает параллелизм: max 25 CDR одновременно.
    Backoff: 1, 2, 4 сек между попытками.
    """
    async with _cdr_semaphore:
        for attempt in range(max_attempts):
            try:
                await _handle_cdr(event)
                return
            except Exception as exc:
                # Нетранзиентная ошибка (в т.ч. IntegrityError) — не ретраим
                if not _is_retriable_db_error(exc):
                    raise
                if attempt < max_attempts - 1:
                    wait_secs = 2 ** attempt  # 1, 2, 4 сек
                    logger.warning(
                        "Транзиентный сбой БД %s (попытка %d/%d), retry через %ds: uniqueid=%s",
                        type(exc).__name__, attempt + 1, max_attempts, wait_secs,
                        event.get("uniqueid"),
                    )
                    await asyncio.sleep(wait_secs)
                    continue
                logger.error(
                    "CDR потерян после %d попыток (%s): uniqueid=%s",
                    max_attempts, type(exc).__name__, event.get("uniqueid"),
                )
                raise
```

### Риски и как проверить

- Риск: широкий `except Exception` мог бы проглотить логические баги. Митигируем: любой
  нетранзиентный exc (включая `IntegrityError`) немедленно `raise` — поведение как раньше,
  просто расширился список ретраибельных.
- Риск: двойной импорт `IntegrityError` (модульный + локальный в `_handle_cdr`). Безвреден,
  Python допускает; `_handle_cdr` не трогаем.
- Проверка: unit-тест `test_retry_handle_cdr.py` (П1.10.2) — по одному кейсу на каждый тип.
- Проверка компиляции: `python -c "import ast; ast.parse(open('backend/app/workers/call_processor.py').read())"`.

---

## П1.7 — sleep(3) из-под семафора

### Контекст (реальный код)

Оба `await asyncio.sleep(3)` исполняются ВНУТРИ `_cdr_semaphore` (захват в
`_retry_handle_cdr`, L327), т.к. `_handle_cdr` → `_push_to_amo` вызываются под ним.
Залп из N legs одного/разных звонков занимает слоты семафора на 3 секунды простоя,
из-за чего 25 слотов быстро кончаются.

Место 1 — `_push_to_amo`, L248-277 (промах `amo_lead_lock`):
```python
lock_acquired = await redis_client.set(lock_key, "1", nx=True, ex=60)
if not lock_acquired:
    # Другой leg держит lock прямо сейчас — ждём пока он создаст лид.
    await asyncio.sleep(3)                      # ← L254
    post_wait_lead_id = await _find_existing_lead_for_caller(
        db, call.caller_number, call.id, window_minutes=30
    )
    if post_wait_lead_id:
        ...
```

Место 2 — `_handle_cdr`, L386-398 (промах `call_lock`):
```python
call_lock_acquired = await redis_client.set(call_lock_key, "1", nx=True, ex=120)
if not call_lock_acquired:
    logger.info("call_lock: caller=%s ... — ждём 3с", normalized_caller)
    await asyncio.sleep(3)                        # ← L397
    # После паузы продолжаем — SQL pre-check найдёт лид от первого leg-а
```

### Выбранный вариант: (б) короткий SQL-поллинг вместо sleep(3)

Обоснование выбора (из трёх предложенных):
- Вариант (а) «выпускать семафор на время sleep» требует переструктуризации
  `async with _cdr_semaphore` в `_retry_handle_cdr` в ручной acquire/release с try/finally
  и прокидывания флага «сейчас ждём» вглубь двух функций — инвазивно, легко словить утечку
  слота при исключении.
- Вариант (в) «вынести ожидание ДО захвата семафора» невозможно чисто: DID/caller
  определяются уже внутри `_handle_cdr` (после захвата), а ключи lock зависят от caller.
- Вариант (б) минимально-инвазивен: заменяем «слепой» `sleep(3)` на активный поллинг SQL
  3×1с с ранним выходом. Слот семафора всё ещё занят, НО: (1) в среднем ждём меньше 3с
  (выходим как только сосед-leg записал лид/запись, обычно 1-2с), (2) поллинг короче под
  нагрузкой, (3) для `call_lock` (место 2) SQL pre-check делается по `caller_number` в
  таблице `calls` — как только сосед-leg дошёл до `db.commit()` записи Call, мы это видим и
  сразу продолжаем.

Итог по throughput: слот занят не фиксированные 3с, а до момента появления соседской
записи (early-exit). Это единственное безопасное улучшение без ломки семафора.

### Точные правки

#### Место 1 — `_push_to_amo` (L248-277)

Заменить блок `if not lock_acquired:` (от L252 `if not lock_acquired:` до L277 — конец
warning-лога) на поллинг. Новый код:

```python
        if not lock_acquired:
            # Другой leg держит lock прямо сейчас. Вместо слепого sleep(3) —
            # короткий поллинг SQL 3×1с: как только сосед сохранит amo_lead_id,
            # выходим раньше и не держим слот семафора зря.
            post_wait_lead_id = None
            for _ in range(3):
                await asyncio.sleep(1)
                post_wait_lead_id = await _find_existing_lead_for_caller(
                    db, call.caller_number, call.id, window_minutes=30
                )
                if post_wait_lead_id:
                    break
            if post_wait_lead_id:
                call.amo_lead_id = post_wait_lead_id
                await db.commit()
                logger.info(
                    "AMO: дубль leg (post-wait SQL), привязан к лиду %s (caller=%s)",
                    post_wait_lead_id, call.caller_number,
                )
                try:
                    await amocrm_client.add_call_note(post_wait_lead_id, call)
                except Exception:
                    logger.exception(
                        "AMO: add_call_note на reuse (post-wait) не сработал (lead=%s)",
                        post_wait_lead_id,
                    )
            else:
                logger.warning(
                    "AMO: lock не получен и лид не найден после ожидания (caller=%s, uniqueid=%s) — пропускаем",
                    call.caller_number, uniqueid,
                )
```

> Изменение по сути точечное: строку `await asyncio.sleep(3)` заменяем на цикл поллинга,
> остальная логика (успех → reuse + add_call_note, иначе → warning) сохранена 1:1.

#### Место 2 — `_handle_cdr` (L386-398)

Заменить блок:
```python
    if not call_lock_acquired:
        logger.info(
            "call_lock: caller=%s уже обрабатывается другим leg-ом — ждём 3с", normalized_caller
        )
        await asyncio.sleep(3)
        # После паузы продолжаем — SQL pre-check найдёт лид от первого leg-а
```
на:
```python
    if not call_lock_acquired:
        logger.info(
            "call_lock: caller=%s уже обрабатывается другим leg-ом — поллим до 3с",
            normalized_caller,
        )
        # Вместо слепого sleep(3) — короткий поллинг: как только сосед-leg сохранит
        # свою запись Call с этим caller_number, продолжаем. Раньше выходим — раньше
        # освобождаем слот семафора. Держим слот максимум 3с (как было), но обычно меньше.
        normalized_caller_search = normalized_caller
        for _ in range(3):
            await asyncio.sleep(1)
            try:
                async with async_session() as _poll_db:
                    exists_row = await _poll_db.execute(
                        select(Call.id)
                        .where(Call.caller_number == src)
                        .where(
                            Call.started_at
                            >= datetime.now(timezone.utc) - timedelta(minutes=30)
                        )
                        .limit(1)
                    )
                    if exists_row.scalar_one_or_none() is not None:
                        break
            except Exception:
                # Поллинг — best-effort; ошибка чтения не должна прерывать обработку
                logger.debug(
                    "call_lock poll: ошибка проверки caller=%s (продолжаем ждать)",
                    normalized_caller_search,
                )
        # После ожидания продолжаем — SQL pre-check в _push_to_amo найдёт лид от первого leg-а
```

> Пояснение: в месте 2 ждём появления соседской записи `Call` (по `caller_number == src`).
> `src` — это исходный (ненормализованный) caller; в таблицу `calls` пишется именно `src`
> (см. L456 `caller_number=src`). Поэтому в WHERE используем `src`, а не `normalized_caller`.
> `select`, `Call`, `datetime`, `timedelta`, `timezone`, `async_session` уже импортированы
> в модуле (L8, L10, L12, L15). Отдельная сессия `_poll_db` открывается и закрывается на
> каждой итерации, чтобы не держать транзакцию.

### Риски и как проверить

- Риск: поллинг делает до 3 доп. SELECT-ов при промахе lock-а. Нагрузка мала (промах lock
  редок и только для multi-leg), запрос по индексируемому `caller_number` + окно 30 мин.
- Риск: слот семафора всё ещё занят во время поллинга. Это осознанный компромисс: полностью
  освободить слот безопасно нельзя без ломки семафора; early-exit сокращает время.
- Проверка: `ast.parse` файла; ручной прогон логики глазами (тестами это место не покрываем —
  требует реальных Redis+PG; smoke-контракт покрывает вызовы amocrm_client).
- Проверка отсутствия `sleep(3)` под семафором: `grep -n "sleep(3)" backend/app/workers/call_processor.py`
  должен вернуть ПУСТО.

---

## П1.8 — Redis-fallback round-robin (детерминированный)

### Контекст (реальный код)

`backend/app/services/amocrm.py`:
- L89: `_ROUND_ROBIN_CITIES = ["Алматы", "Астана", "Шымкент", "Атырау", "Актобе"]`
- L164-173 — текущий метод, при исключении Redis возвращает `_ROUND_ROBIN_CITIES[0]`
  («Алматы» ВСЕМ лидам при любом сбое Redis).
- Единственный вызов — L335-337 в `create_lead_from_call`:
  ```python
  forced_city: str | None = None
  if not (_SOURCE_TO_CITY.get(call.source) or _city_from_campaign(call.campaign)):
      forced_city = await self._next_round_robin_city()
  ```

### Требуемое поведение

- Fallback при сбое Redis: `_ROUND_ROBIN_CITIES[crc32(caller_bytes) % len(...)]`.
  `crc32` (`zlib.crc32`) стабилен между процессами и запусками (в отличие от `hash()`,
  который рандомизирован `PYTHONHASHSEED`). Это даёт детерминированный, равномерный
  разброс по 5 городам вместо «всё в Алматы».
- Сигнатура меняется: метод принимает `caller: str`.

### Точные правки

#### Импорт (L7-9, блок `import logging` / `import time`)

Добавить рядом с существующими импортами (после `import time`, L8):

```python
import zlib
```

#### Метод (заменить L164-173 целиком)

```python
    async def _next_round_robin_city(self, caller: str) -> str:
        """Возвращает следующий город по кругу (атомарно через Redis INCR).

        Используется когда город лида неизвестен (веб-каналы: instagram/site/fb/tiktok).
        При сбое Redis — детерминированный fallback: crc32(caller) % N.
        crc32 стабилен между процессами (в отличие от hash(), который рандомизирован
        PYTHONHASHSEED), поэтому один и тот же номер всегда попадёт в один город,
        а поток номеров равномерно разложится по всем 5 городам вместо «всё в Алматы».
        """
        try:
            n = await redis_client.incr(_RR_CITY_REDIS_KEY)
            return _ROUND_ROBIN_CITIES[(n - 1) % len(_ROUND_ROBIN_CITIES)]
        except Exception:
            # Стабильный детерминированный разброс по caller при недоступном Redis.
            idx = zlib.crc32((caller or "").encode("utf-8")) % len(_ROUND_ROBIN_CITIES)
            logger.exception(
                "RR-город: ошибка Redis, fallback по crc32(caller=%s) -> %s",
                caller, _ROUND_ROBIN_CITIES[idx],
            )
            return _ROUND_ROBIN_CITIES[idx]
```

#### Вызов (L337)

Заменить:
```python
        forced_city = await self._next_round_robin_city()
```
на:
```python
        forced_city = await self._next_round_robin_city(caller)
```

> `caller` доступен в `create_lead_from_call(self, call: Call, caller: str)` (параметр
> метода, L265) — прокидываем его.

### Риски и как проверить

- Риск: `zlib.crc32` возвращает unsigned int в Python 3 — `% 5` всегда даёт 0..4, границы
  корректны.
- Риск: `caller` может быть пустой строкой — `(caller or "")` даёт crc32 пустой строки (0),
  индекс 0 («Алматы»). Приемлемо (пустой caller — краевой случай).
- Проверка: unit-тест `test_round_robin_city.py` (П1.10.1): happy-path через мок `incr`,
  fallback через `incr` бросающий исключение + проверка `zlib.crc32` детерминизма.
- Grep других вызовов: `grep -rn "_next_round_robin_city" backend/` — должно быть ровно 2
  совпадения (определение + один вызов). Reconciliation вызывает `create_lead_from_call`, а
  не метод напрямую, — его не трогаем.

---

## П1.9 — Логи из /tmp + ротация

Всё в этом пункте — операции на СЕРВЕРЕ (195.49.215.96, пользователь `alisher`, работаем
только внутри `/home/alisher/`) плюс правки в репозитории. Root НЕ используем. systemd-юнит
воркера — user-unit пользователя alisher (не системный); правится через
`systemctl --user`. Если юнит окажется системным (в `/etc/systemd/system/`) —
редактирование требует root: тогда ТОЛЬКО подготовить diff и эскалировать человеку
(см. раздел «если юнит системный»).

### (а) Создать папку логов

На сервере:
```bash
mkdir -p /home/alisher/kurotrack/logs
chmod 755 /home/alisher/kurotrack/logs
```

В репозитории — чтобы папка существовала после `git pull` и не коммитились сами логи:

1. `.gitignore` — добавить в конец:
```
# Логи воркера/скриптов — не коммитим содержимое, но храним папку
logs/
!logs/.gitkeep
```
2. Создать пустой файл-маркер `logs/.gitkeep` (в корне репо):
```bash
mkdir -p /Users/baigelenov/projects/kurotrack/logs
touch /Users/baigelenov/projects/kurotrack/logs/.gitkeep
```
> Исполнитель П1.9 создаёт `logs/.gitkeep` через файловый Write (пустой файл) и правит
> `.gitignore`.

### (б) systemd-юнит воркера

Юнит на сервере (найти точное имя):
```bash
systemctl --user list-units --type=service | grep -i kuro
# либо
ls ~/.config/systemd/user/*.service 2>/dev/null | grep -i kuro
```

В найденном `[Service]`-блоке заменить:
```
StandardOutput=append:/tmp/kurotrack-worker.log
StandardError=append:/tmp/kurotrack-worker.log
```
на:
```
StandardOutput=append:/home/alisher/kurotrack/logs/worker.log
StandardError=append:/home/alisher/kurotrack/logs/worker.log
```
Применить:
```bash
systemctl --user daemon-reload
systemctl --user restart <имя-юнита>.service
```

> Файл юнита в репозитории ОТСУТСТВУЕТ (лежит только на сервере). В git ничего по (б) не
> коммитим. Правка — вручную на сервере исполнителем-деплоером.

### (в) Скрипты и cron на новые пути

1. `scripts/monitor.py`, L141:
```python
    log_path = "/tmp/kurotrack-worker.log"
```
→
```python
    log_path = "/home/alisher/kurotrack/logs/worker.log"
```

2. `scripts/backup_db.sh` — НЕ содержит путей `/tmp`, пишет `ls -lh` в stdout (его
   перехватывает cron). Оставляем как есть; при желании его вывод направляется в лог через
   cron-строку (см. ниже). Правок в файле не требуется.

3. Cron-строки на сервере (`crontab -e` от alisher, БЕЗ sudo). Заменить старые пути логов
   `/tmp/kurotrack-*.log` на `/home/alisher/kurotrack/logs/`. Итоговый crontab (эталон —
   исполнитель сверяет с текущим `crontab -l` и правит только строки KuroTrack):

```cron
# --- KuroTrack ---
# Health-monitor каждые 5 минут (сам решает, слать ли алерт)
*/5 * * * * cd /home/alisher/kurotrack && set -a && . backend/.env.worker 2>/dev/null; . backend/.env.monitor 2>/dev/null; set +a; /home/alisher/kurotrack/backend/venv/bin/python /home/alisher/kurotrack/scripts/monitor.py >> /home/alisher/kurotrack/logs/monitor.log 2>&1
# Ежедневный бэкап БД в 03:30 (ретеншн внутри скрипта — 14 дней)
30 3 * * * /home/alisher/kurotrack/scripts/backup_db.sh >> /home/alisher/kurotrack/logs/backup.log 2>&1
# Ротация логов (см. пункт г)
0 4 * * * /usr/sbin/logrotate --state /home/alisher/kurotrack/logs/.logrotate.state /home/alisher/kurotrack/logs/logrotate.conf >> /home/alisher/kurotrack/logs/logrotate.log 2>&1
```

> ВАЖНО: строки `auto_assign` и `cleanup_drugoy` из crontab должны отсутствовать (см. П1.11).
> Команда проверки — в П1.11.

### (г) Ротация без root

Выбран logrotate с user-state (проще самописного и не требует root). Причина выбора:
`monitor.py`-trim (описан ниже как fallback-часть критерия) страхует только `worker.log`;
logrotate покрывает все логи единообразно и стандартно. Реализуем ОБА минимально:
основная ротация — logrotate; в `monitor.py` — аварийный trim `worker.log`, если он
внезапно перерос 50 МБ между ротациями (критерий AC-4 требует именно этого).

Создать на сервере файл `/home/alisher/kurotrack/logs/logrotate.conf`:
```
/home/alisher/kurotrack/logs/worker.log
/home/alisher/kurotrack/logs/monitor.log
/home/alisher/kurotrack/logs/backup.log
{
    daily
    rotate 7
    maxsize 50M
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
}
```
> `copytruncate` обязателен: worker пишет через `append:` от systemd и держит файл открытым;
> без copytruncate logrotate не сможет переоткрыть файл (нет способа послать сигнал сервису
> без root). `--state /home/alisher/kurotrack/logs/.logrotate.state` держит состояние в
> user-папке — root не нужен. `logrotate` присутствует в системе как бинарь
> (`/usr/sbin/logrotate`), запуск от обычного пользователя со своим state-файлом разрешён.

Аварийный trim в `monitor.py` — добавить функцию и вызвать её в конце `check_worker_errors`
(или в `main`). Точный код — новая функция после `check_worker_errors` (после L155):

```python
def trim_worker_log_if_huge(log_path: str, max_bytes: int = 50 * 1024 * 1024,
                            keep_bytes: int = 20 * 1024 * 1024):
    """Аварийный trim: если лог перерос max_bytes — усекаем до последних keep_bytes.

    Страховка на случай, если logrotate не отработал. Читаем хвост keep_bytes и
    перезаписываем файл им же. Без root, atomic через временный файл в той же папке.
    """
    try:
        if not os.path.exists(log_path):
            return
        size = os.path.getsize(log_path)
        if size <= max_bytes:
            return
        with open(log_path, "rb") as f:
            f.seek(size - keep_bytes)
            tail = f.read()
        tmp_path = log_path + ".trim.tmp"
        with open(tmp_path, "wb") as f:
            f.write(tail)
        os.replace(tmp_path, log_path)
        print(f"trim_worker_log: усечён {log_path} с {size} до ~{keep_bytes} байт", flush=True)
    except Exception as e:
        print(f"trim_worker_log: не критично, не смог усечь {log_path}: {e}")
```

И в `main()` (после `await check_worker_errors()`, L191) добавить:
```python
    trim_worker_log_if_huge("/home/alisher/kurotrack/logs/worker.log")
```

### (д) journald

Проверить, пишет ли юнит в journal:
```bash
systemctl --user cat <имя-юнита>.service | grep -i StandardOutput
journalctl --user -u <имя-юнита>.service --no-pager -n 5
journalctl --user --disk-usage
```
Факт из ревью: `StandardOutput=append:` направляет вывод В ФАЙЛ, а НЕ в journal — значит
новые записи воркера в journal НЕ идут. Старые 736 МБ в journal — исторические (до
перехода на `append:`), их нужно усечь.

Усечение user-журнала без root (работает для журнала пользователя alisher):
```bash
# Посмотреть текущий объём
journalctl --user --disk-usage
# Усечь до 100 МБ (или по времени)
journalctl --user --vacuum-size=100M
# Альтернатива — по возрасту
journalctl --user --vacuum-time=7d
```
> `journalctl --user --vacuum-*` работает с журналом текущего пользователя без root, если
> включён persistent user-journal (`/var/log/journal/<uid>/`). Если 736 МБ лежат в
> СИСТЕМНОМ журнале (не user) — `--user` их не тронет, и очистка системного журнала требует
> root → эскалировать человеку с командой `sudo journalctl --vacuum-size=200M`
> (человек выполняет сам, координатор НЕ запускает sudo). Сначала проверить принадлежность:
> `journalctl --user --disk-usage` vs `journalctl --disk-usage` (последнее без --user может
> потребовать прав; если 736 МБ видно только без --user — журнал системный).

### Если юнит системный (не user)

Если `ls ~/.config/systemd/user/*.service` пуст, а юнит найден в `/etc/systemd/system/`:
правка файла и `systemctl daemon-reload/restart` требуют root → это выход за
`/home/alisher/`. Действие: подготовить готовый diff `[Service]`-блока (см. (б)) и
эскалировать человеку для применения под root. НЕ редактировать `/etc/` самостоятельно.

### Риски и как проверить

- Риск: `copytruncate` теряет строки, записанные между copy и truncate (доли секунды) —
  для логов приемлемо.
- Риск: путь systemd `append:` создаёт файл, если директория существует; поэтому (а) папку
  создаём ДО рестарта (б).
- Проверка (а): `ls -ld /home/alisher/kurotrack/logs`.
- Проверка (б): после рестарта `ls -l /home/alisher/kurotrack/logs/worker.log` растёт;
  `/tmp/kurotrack-worker.log` больше не пишется (`stat` mtime не обновляется).
- Проверка (г): `logrotate -d /home/alisher/kurotrack/logs/logrotate.conf` (dry-run, флаг
  `-d` = debug, без изменений) не ругается; `crontab -l | grep logrotate` присутствует.
- Проверка (в): `grep -rn "/tmp/kurotrack" scripts/` — после правок должно остаться только
  историческое упоминание в комментариях удаляемых файлов (которых уже нет). В `monitor.py`
  путь `/tmp` отсутствует.

---

## П1.10 — Тесты

### Общая настройка (pyproject)

В `backend/pyproject.toml` добавить секцию (после блока `[tool.ruff.lint]` или сразу после
optional-dependencies — порядок в TOML не важен, но не внутри существующих таблиц):

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```
> `asyncio_mode = "auto"` (pytest-asyncio, уже в dev-deps) позволяет писать `async def test_*`
> без декоратора `@pytest.mark.asyncio`. Существующие синхронные тесты не ломаются.
> `conftest.py` не требуется: все три теста импортируют модули напрямую через
> `sys.path.insert` (паттерн из `test_city_from_campaign.py`, L6).

Команда запуска (из папки `backend/`, эталон):
```bash
cd backend && python -m pytest tests -q
```

### П1.10.1 — `backend/tests/test_round_robin_city.py`

Мок-подход: `_next_round_robin_city` вызывает модульный `redis_client.incr`
(`app.services.amocrm.redis_client`). Патчим этот объект через
`unittest.mock.patch` + `AsyncMock`.

Скелет (полный, готов к запуску):
```python
"""Unit-тесты для AmoCRMClient._next_round_robin_city (П1.8)."""

import os
import sys
import zlib
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from app.services.amocrm import AmoCRMClient, _ROUND_ROBIN_CITIES  # noqa: E402


@pytest.fixture
def client():
    return AmoCRMClient()


class TestRoundRobinHappyPath:
    """Redis.incr отдаёт 1..N — города идут по кругу."""

    async def test_sequential_incr_cycles_cities(self, client):
        # incr возвращает 1,2,3,4,5,6,7 последовательно
        seq = iter([1, 2, 3, 4, 5, 6, 7])
        mock_incr = AsyncMock(side_effect=lambda *a, **k: next(seq))
        with patch("app.services.amocrm.redis_client.incr", mock_incr):
            results = [await client._next_round_robin_city("+77001234567") for _ in range(7)]
        # (n-1) % 5: 0,1,2,3,4,0,1 → города по кругу, 6-й = город[0], 7-й = город[1]
        expected = [
            _ROUND_ROBIN_CITIES[0], _ROUND_ROBIN_CITIES[1], _ROUND_ROBIN_CITIES[2],
            _ROUND_ROBIN_CITIES[3], _ROUND_ROBIN_CITIES[4],
            _ROUND_ROBIN_CITIES[0], _ROUND_ROBIN_CITIES[1],
        ]
        assert results == expected


class TestRoundRobinRedisFallback:
    """Redis.incr бросает исключение — детерминированный fallback по crc32(caller)."""

    async def test_exception_returns_crc32_deterministic_city(self, client):
        caller = "+77001234567"
        mock_incr = AsyncMock(side_effect=RuntimeError("redis down"))
        with patch("app.services.amocrm.redis_client.incr", mock_incr):
            city = await client._next_round_robin_city(caller)
        expected_idx = zlib.crc32(caller.encode("utf-8")) % len(_ROUND_ROBIN_CITIES)
        assert city == _ROUND_ROBIN_CITIES[expected_idx]

    async def test_fallback_stable_across_calls(self, client):
        """Один и тот же caller → один и тот же город при каждом сбое (стабильность)."""
        caller = "+77009998877"
        mock_incr = AsyncMock(side_effect=RuntimeError("redis down"))
        with patch("app.services.amocrm.redis_client.incr", mock_incr):
            first = await client._next_round_robin_city(caller)
            second = await client._next_round_robin_city(caller)
        assert first == second

    async def test_fallback_spreads_across_cities(self, client):
        """Разные caller при сбое Redis не валятся все в один город (не всё в Алматы)."""
        callers = [f"+7700123{i:04d}" for i in range(50)]
        mock_incr = AsyncMock(side_effect=RuntimeError("redis down"))
        with patch("app.services.amocrm.redis_client.incr", mock_incr):
            cities = set()
            for c in callers:
                cities.add(await client._next_round_robin_city(c))
        # crc32 по 50 разным номерам должен затронуть >1 города
        assert len(cities) > 1

    async def test_empty_caller_no_crash(self, client):
        mock_incr = AsyncMock(side_effect=RuntimeError("redis down"))
        with patch("app.services.amocrm.redis_client.incr", mock_incr):
            city = await client._next_round_robin_city("")
        assert city in _ROUND_ROBIN_CITIES
```

### П1.10.2 — `backend/tests/test_retry_handle_cdr.py`

Мок-подход: патчим `app.workers.call_processor._handle_cdr` на `AsyncMock` с `side_effect`,
и `app.workers.call_processor.asyncio.sleep` на `AsyncMock` (чтобы тест не ждал реальных
1/2/4с). Семафор `_cdr_semaphore` не мешает (тест последовательный).

Скелет (полный):
```python
"""Unit-тесты для _retry_handle_cdr (П1.6): какие ошибки ретраятся, какие — нет."""

import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from asyncpg.exceptions import TooManyConnectionsError, ConnectionDoesNotExistError  # noqa: E402
from sqlalchemy.exc import IntegrityError, OperationalError, TimeoutError as SATimeoutError  # noqa: E402

from app.workers import call_processor  # noqa: E402


EVENT = {"uniqueid": "test-uid-1"}


async def _run_with_mocked_handle(side_effect):
    """Патчит _handle_cdr и asyncio.sleep, вызывает _retry_handle_cdr, возвращает mock."""
    mock_handle = AsyncMock(side_effect=side_effect)
    with patch.object(call_processor, "_handle_cdr", mock_handle), \
         patch.object(call_processor.asyncio, "sleep", AsyncMock()):
        try:
            await call_processor._retry_handle_cdr(EVENT, max_attempts=3)
        except Exception as exc:  # noqa: BLE001
            return mock_handle, exc
    return mock_handle, None


class TestRetriableErrors:
    """Транзиентные сбои БД → ретраятся до max_attempts."""

    async def test_too_many_connections_retries_3_times(self):
        # Всегда падает → 3 попытки, потом raise
        mock_handle, exc = await _run_with_mocked_handle(
            TooManyConnectionsError("too many")
        )
        assert mock_handle.await_count == 3
        assert isinstance(exc, TooManyConnectionsError)

    async def test_operational_error_retries(self):
        mock_handle, exc = await _run_with_mocked_handle(
            OperationalError("stmt", {}, Exception("conn lost"))
        )
        assert mock_handle.await_count == 3
        assert isinstance(exc, OperationalError)

    async def test_sqlalchemy_timeout_retries(self):
        mock_handle, exc = await _run_with_mocked_handle(SATimeoutError())
        assert mock_handle.await_count == 3
        assert isinstance(exc, SATimeoutError)

    async def test_connection_does_not_exist_retries(self):
        mock_handle, exc = await _run_with_mocked_handle(
            ConnectionDoesNotExistError("gone")
        )
        assert mock_handle.await_count == 3

    async def test_wrapped_asyncpg_in_dbapi_orig_retries(self):
        # asyncpg-исключение, обёрнутое SQLAlchemy: OperationalError с orig=TooManyConnections
        wrapped = OperationalError("stmt", {}, TooManyConnectionsError("too many"))
        mock_handle, exc = await _run_with_mocked_handle(wrapped)
        assert mock_handle.await_count == 3


class TestNonRetriableErrors:
    """IntegrityError и прочее → НЕ ретраится (одна попытка)."""

    async def test_integrity_error_not_retried(self):
        # IntegrityError — дубль, НЕ ретраим: ровно 1 вызов, потом raise
        mock_handle, exc = await _run_with_mocked_handle(
            IntegrityError("stmt", {}, Exception("dup key"))
        )
        assert mock_handle.await_count == 1
        assert isinstance(exc, IntegrityError)

    async def test_value_error_not_retried(self):
        mock_handle, exc = await _run_with_mocked_handle(ValueError("logic bug"))
        assert mock_handle.await_count == 1
        assert isinstance(exc, ValueError)


class TestSuccessPath:
    """Успех с первой попытки → один вызов, без исключений."""

    async def test_success_first_try(self):
        mock_handle = AsyncMock(return_value=None)
        with patch.object(call_processor, "_handle_cdr", mock_handle), \
             patch.object(call_processor.asyncio, "sleep", AsyncMock()):
            await call_processor._retry_handle_cdr(EVENT, max_attempts=3)
        assert mock_handle.await_count == 1

    async def test_recovers_on_second_attempt(self):
        # Первая попытка падает транзиентно, вторая успешна → 2 вызова, без raise
        calls = {"n": 0}

        async def side(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TooManyConnectionsError("too many")
            return None

        mock_handle = AsyncMock(side_effect=side)
        with patch.object(call_processor, "_handle_cdr", mock_handle), \
             patch.object(call_processor.asyncio, "sleep", AsyncMock()):
            await call_processor._retry_handle_cdr(EVENT, max_attempts=3)
        assert mock_handle.await_count == 2
```

> Примечание по конструкторам исключений SQLAlchemy: `OperationalError` и `IntegrityError`
> принимают `(statement, params, orig)`. `orig` — оригинальное драйверное исключение;
> для проверки `.orig` в `_is_retriable_db_error` передаём в него нужный тип. Конструктор
> `sqlalchemy.exc.TimeoutError` вызывается без аргументов.

### П1.10.3 — `backend/tests/test_amocrm_interface.py`

Цель: поймать регрессии типа «удалили `add_call_note`, а воркер его зовёт». Парсим AST двух
файлов воркера, собираем все имена методов, вызванные на `amocrm_client`, и проверяем, что
`AmoCRMClient` их имеет. Реальные вызовы (проверено grep-ом): `create_lead_from_call`,
`add_call_note`.

Скелет (полный):
```python
"""Smoke-контракт: все методы amocrm_client, вызываемые из воркеров, существуют (П1.10.3).

Ловит регрессии вроде удаления add_call_note при живом вызове в call_processor.
"""

import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from app.services.amocrm import AmoCRMClient  # noqa: E402

_BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..")
_WORKER_FILES = [
    os.path.join(_BACKEND_DIR, "app", "workers", "call_processor.py"),
    os.path.join(_BACKEND_DIR, "app", "workers", "reconciliation.py"),
]


def _collect_amocrm_method_calls(source: str) -> set[str]:
    """Возвращает множество имён X из вызовов amocrm_client.X(...) в исходнике."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        # Ищем Call, у которого func = Attribute(value=Name('amocrm_client'), attr='X')
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func
            if isinstance(attr.value, ast.Name) and attr.value.id == "amocrm_client":
                names.add(attr.attr)
    return names


def _all_called_methods() -> set[str]:
    called: set[str] = set()
    for path in _WORKER_FILES:
        with open(path, "r", encoding="utf-8") as f:
            called |= _collect_amocrm_method_calls(f.read())
    return called


def test_worker_files_exist():
    for path in _WORKER_FILES:
        assert os.path.exists(path), f"нет файла воркера: {path}"


def test_at_least_expected_methods_detected():
    """Sanity: парсер реально что-то нашёл (иначе тест бессмыслен)."""
    called = _all_called_methods()
    # Эти два вызова заведомо есть в коде — если пропали, что-то сломалось в парсере/коде
    assert "create_lead_from_call" in called
    assert "add_call_note" in called


@pytest.mark.parametrize("method_name", sorted(_all_called_methods()))
def test_amocrm_client_has_method(method_name):
    """Каждый вызываемый из воркеров метод должен существовать у AmoCRMClient."""
    assert hasattr(AmoCRMClient, method_name), (
        f"AmoCRMClient не имеет метода '{method_name}', "
        f"но он вызывается на amocrm_client в воркерах — регрессия интерфейса"
    )
```

> Параметризация по `sorted(_all_called_methods())` вычисляется на этапе сбора тестов —
> это безопасно, т.к. функция читает файлы с диска без побочных эффектов.

### Риски и как проверить

- Риск: `asyncio_mode="auto"` неожиданно повлияет на существующие sync-тесты. Митигация:
  sync-тесты (`def test_*`) в auto-режиме не оборачиваются как корутины — pytest-asyncio
  трогает только `async def`. Прогнать полный набор: `python -m pytest tests -q`.
- Проверка: `cd backend && python -m pytest tests -q` — все зелёные (существующие +
  новые). Если pytest-asyncio не установлен — `pip install -e ".[dev]"` в venv.

---

## П1.11 — Мёртвый код

### Что удаляем

| Файл | Причина |
|------|---------|
| `scripts/auto_assign_leads.py` | Снят с cron; round-robin город в `amocrm.py` (`_next_round_robin_city`) заменил назначение — Salesbot AMO раздаёт лиды по городам сам |
| `scripts/run_auto_assign.sh` | Обёртка запуска удаляемого скрипта |
| `scripts/cleanup_drugoy_city.py` | Снят с cron; город «Другой» больше не ставится (город пишется явно либо round-robin), чистить нечего |
| `scripts/run_cleanup_drugoy.sh` | Обёртка запуска удаляемого скрипта |

### Проверка отсутствия ссылок (выполнено при анализе, повторить перед удалением)

Live-код НЕ ссылается на эти файлы. Единственные ссылки:
- `scripts/run_auto_assign.sh` → `auto_assign_leads.py` (обёртка удаляется вместе)
- `scripts/run_cleanup_drugoy.sh` → `cleanup_drugoy_city.py` (обёртка удаляется вместе)
- `.claude/worktrees/agent-afb1ac1f480e71d73/...` — ИЗОЛИРОВАННАЯ рабочая копия другого
  агента, НЕ трогаем (не часть основного дерева).

Мёртвые импорты уходят автоматически вместе с файлами:
- `auto_assign_leads.py` L14 `import json` (не используется), L20 импорт
  `FIELD_CITY, ENUM_CITY_DRUGOY, STATUS_LOST` — файл удаляется целиком.
- `cleanup_drugoy_city.py` L11 `import json`, L16 `ENUM_CITY_DRUGOY` — файл удаляется целиком.

ВАЖНО: сами константы `ENUM_CITY_DRUGOY` и `STATUS_LOST` в
`backend/app/core/amo_constants.py` НЕ удаляем — они используются в живом коде:
- `STATUS_LOST` → `backend/app/services/amo_sync.py` L15,25.
- `ENUM_CITY_DRUGOY` → определена и используется только в удаляемом скрипте, НО оставляем
  её в `amo_constants.py` как справочный enum города (безвредна, удаление константы вне
  задачи и рискует чужими импортами). Комментарий на L23 «которую чистит cleanup-скрипт»
  можно поправить, но это не обязательно — не входит в scope.

### Команды удаления (git)

```bash
cd /Users/baigelenov/projects/kurotrack
git rm scripts/auto_assign_leads.py scripts/run_auto_assign.sh \
       scripts/cleanup_drugoy_city.py scripts/run_cleanup_drugoy.sh
```

### Проверка crontab на сервере (не должно быть этих задач)

```bash
crontab -l | grep -E "auto_assign|run_auto_assign|cleanup_drugoy|run_cleanup" || echo "OK: нет задач auto_assign/cleanup в crontab"
```
Если строки найдены — удалить их через `crontab -e` (без sudo, только пользователь alisher).

### Риски и как проверить

- Риск: скрипт всё же нужен где-то в cron. Митигация: команда проверки crontab выше; если
  пусто — безопасно.
- Проверка: `git status` показывает 4 удалённых файла; `ls scripts/` — остались только
  `monitor.py`, `backup_db.sh`; `grep -rn "auto_assign_leads\|cleanup_drugoy_city" .
  --include="*.py" --include="*.sh"` (вне `.claude/worktrees` и `.git`) — пусто.

---

## П1.12 — Merge в master

### Установленные факты (проверено на реальном репо)

- Локальной ветки `master` НЕТ. Есть только `origin/master` = `76c6b73 Initial commit`.
- `origin/master` — прямой предок `fix/call-tracking-persistence`
  (`git merge-base --is-ancestor origin/master fix/... ` → истина).
  Значит **merge будет fast-forward** (88 коммитов fix поверх Initial commit).
- Локальная `fix/call-tracking-persistence` == `origin/fix/call-tracking-persistence`
  (диапазон `origin/fix..fix` пуст) → **все локальные коммиты уже в GitHub**. Последние 5
  хэшей совпадают: `fc30d03, 4209850, 6a6ff78, a2d57c2, 96c69a5`.
- Worktree `master` НЕ держится: в `git worktree list` нет строки с `[master]` — ни один
  worktree не занимает ветку master, конфликта checkout не будет.
- ВАЖНО: этот merge должен выполняться ПОСЛЕ того, как все правки П1.6–П1.11 закоммичены в
  `fix/call-tracking-persistence` и запушены. Порядок задач — см. JSON-блок (П1.12 = wave 4).

### Локальный процесс (после коммита и пуша всех P1-правок в fix)

```bash
cd /Users/baigelenov/projects/kurotrack

# 0. Сверка: fix полностью в origin (после push P1-правок должно быть пусто)
git fetch origin
git log --oneline origin/fix/call-tracking-persistence..fix/call-tracking-persistence
#   ^ ПУСТО = всё запушено. Если непусто — сначала git push origin fix/call-tracking-persistence

# 1. Создать/получить локальный master из origin и влить fix (fast-forward)
git checkout -B master origin/master
git merge --ff-only fix/call-tracking-persistence
#   --ff-only гарантирует именно fast-forward; если git откажет — merge не был бы линейным,
#   тогда СТОП и эскалация (по фактам выше ff гарантирован).

# 2. Пуш master
git push origin master

# 3. Вернуться на рабочую ветку
git checkout fix/call-tracking-persistence
```

### Процесс на сервере

```bash
cd /home/alisher/kurotrack
git fetch origin
# запомнить текущий коммит для отката
git rev-parse HEAD    # <-- записать, например BEFORE=<hash>
git checkout master
git pull --ff-only origin master

# рестарт воркера (user-unit; имя — из П1.9)
systemctl --user restart <имя-юнита>.service
sleep 3

# health-check (uvicorn слушает 127.0.0.1:8102; наружу — https://kt.aiplus.kz/health)
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8102/health   # ожидаем 200
curl -sS https://kt.aiplus.kz/api/v1/health                              # ожидаем status ok
```

### План отката

Если health != 200 ИЛИ воркер не поднялся:
```bash
cd /home/alisher/kurotrack
git checkout fix/call-tracking-persistence   # рабочая ветка со всеми теми же коммитами
# (fix и master после ff указывают на один коммит — откат по сути возвращает
#  на именованную ветку; если проблема в самом коде — откатиться на предыдущий known-good:)
git reset --hard <BEFORE>                     # <BEFORE> из шага выше, ТОЛЬКО если код сломан
systemctl --user restart <имя-юнита>.service
sleep 3
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8102/health
```
> Поскольку merge — fast-forward, `master` и `fix` указывают на ОДИН коммит `fc30d03(+P1)`.
> «Откат» на fix-ветку сам по себе код не меняет. Реальный откат кода = `git reset --hard`
> на предыдущий рабочий коммит (`<BEFORE>`), который зафиксировали до pull. Это безопасно:
> `reset --hard` на локальной серверной копии не трогает GitHub.

### Риски и как проверить

- Риск: `git checkout master` на сервере упрётся в незакоммиченные локальные изменения
  (например, ранее правленый вручную файл). Митигация: перед checkout `git status` должен
  быть чистым; если нет — `git stash` (не удалять!) и разобраться.
- Риск: `--ff-only` откажет, если в fix появились НЕзапушенные локальные коммиты после
  создания master. Митигация: шаг 0 (сверка origin) обязателен.
- Проверка успеха: `git log --oneline -1 origin/master` = `fc30d03(+P1-коммиты)`;
  `curl .../health` = 200; в `logs/worker.log` нет трейсбеков на старте.

---

## 8. Edge Cases & Error Handling

| Функция | Кейс | Поведение |
|---------|------|-----------|
| `_is_retriable_db_error` | `IntegrityError` (даже как `DBAPIError`) | `False` — НЕ ретраить (ранний выход до проверки DBAPIError) |
| `_is_retriable_db_error` | `OperationalError` с `orig=None` | Ретраить (сам `OperationalError` в списке ретраибельных) |
| `_is_retriable_db_error` | `OperationalError` с `orig=TooManyConnectionsError` | Ретраить (по `e.orig`) |
| `_is_retriable_db_error` | `ValueError`/логический баг | `False` — пробросить сразу |
| `_retry_handle_cdr` | Успех на 2-й попытке | Возврат без исключения, 2 вызова `_handle_cdr` |
| `_retry_handle_cdr` | 3 транзиентных сбоя подряд | `logger.error` + `raise` последнего исключения |
| `_push_to_amo` поллинг | Сосед-leg создал лид за 1с | Выход на 1-й итерации, привязка + `add_call_note` |
| `_push_to_amo` поллинг | Лид так и не появился за 3с | `logger.warning` «lock не получен и лид не найден» |
| `_handle_cdr` поллинг | Ошибка чтения в поллинге | `logger.debug`, продолжаем (best-effort) |
| `_next_round_robin_city` | Redis OK | по кругу через `incr` |
| `_next_round_robin_city` | Redis исключение | `crc32(caller) % 5`, детерминированно |
| `_next_round_robin_city` | `caller=""` или `None` | `crc32("") % 5` = 0 → город[0], без падения |
| `trim_worker_log_if_huge` | Файла нет | тихо выходит |
| `trim_worker_log_if_huge` | Размер ≤ 50 МБ | ничего не делает |
| `trim_worker_log_if_huge` | Ошибка I/O | print-варнинг, не падает (не критично для monitor) |
| П1.12 merge | `--ff-only` отказал | СТОП, не форсить merge-коммит, эскалация |
| П1.12 server | health != 200 | откат по плану, `git reset --hard <BEFORE>` |

---

## 9. Test Scenarios

| Тест | Вход | Ожидание | Тип |
|------|------|----------|-----|
| RR happy path | incr=1..7 | города по кругу (idx=(n-1)%5) | unit |
| RR fallback детерминизм | incr бросает; caller фикс. | город = crc32(caller)%5 | unit |
| RR fallback стабильность | тот же caller дважды | одинаковый город | unit |
| RR fallback разброс | 50 разных caller | затронуто >1 города | unit |
| RR пустой caller | caller="" | город из списка, без краша | unit |
| retry TooManyConnections | всегда падает | 3 попытки + raise | unit |
| retry OperationalError | всегда падает | 3 попытки + raise | unit |
| retry SATimeoutError | всегда падает | 3 попытки + raise | unit |
| retry ConnectionDoesNotExist | всегда падает | 3 попытки | unit |
| retry обёрнутый asyncpg | OperationalError(orig=TooManyConn) | 3 попытки | unit |
| retry IntegrityError | падает | 1 попытка + raise (НЕ ретрай) | unit |
| retry ValueError | падает | 1 попытка + raise | unit |
| retry success | ок с 1-й | 1 вызов, без raise | unit |
| retry recover | падает→ок | 2 вызова, без raise | unit |
| interface: методы есть | AST call_processor+reconciliation | все `amocrm_client.X` → hasattr | unit |
| interface: парсер жив | — | найдены create_lead_from_call, add_call_note | unit |
| pytest общий прогон | `pytest tests` | все зелёные (старые+новые) | integration |
| merge ff | локально | `--ff-only` успешен, master=fix | manual |
| server health | после pull+restart | `/health` = 200 | manual |

---

## 10. Tasks JSON Block

Wave-логика:
- **Wave 1** — независимые код-правки в РАЗНЫХ файлах (параллельно): P1.6+P1.7 (оба в
  `call_processor.py` → ОДИН owner, одна задача, чтобы не было конфликта файла), P1.8
  (`amocrm.py`), P1.11 (удаление `scripts/*`), P1.9-код (`monitor.py` + `.gitignore` +
  `logs/.gitkeep`). Все файлы разные → параллелятся.
- **Wave 2** — тесты (после кода: тестируют новую логику P1.6/P1.8; правят
  `pyproject.toml` + создают новые файлы тестов; `pyproject.toml` — один owner). Зависят
  от P1.6, P1.8.
- **Wave 3** — серверные операции П1.9 (systemd/cron/logrotate/journald) — выполняются на
  сервере после того как код в репо готов (нужен новый `monitor.py`). Зависят от P1.9-код.
- **Wave 4** — merge П1.12 (последний, требует всех коммитов в fix + прогон тестов). Один
  git-репозиторий → строго последовательно после всего.

```json
{
  "tasks": [
    {
      "id": "P1.6-7",
      "description": "call_processor.py: расширить retry на транзиентные сбои БД (TimeoutError/OperationalError/ConnectionDoesNotExist/PostgresConnectionError + распаковка DBAPIError.orig, IntegrityError НЕ ретраить); убрать оба sleep(3) из-под семафора, заменить на SQL-поллинг 3x1c с early-exit",
      "files": ["backend/app/workers/call_processor.py"],
      "owner": "sonnet-backend",
      "wave": 1,
      "depends_on": [],
      "estimated_turns": 40,
      "acceptance": [
        "_is_retriable_db_error возвращает False на IntegrityError и True на 5 транзиентных типов + обёрнутый orig",
        "grep 'sleep(3)' по файлу пусто",
        "ast.parse файла без ошибок"
      ],
      "status": "pending"
    },
    {
      "id": "P1.8",
      "description": "amocrm.py: детерминированный Redis-fallback round-robin — _next_round_robin_city(caller) возвращает crc32(caller)%5 при сбое Redis; import zlib; поправить единственный вызов на caller",
      "files": ["backend/app/services/amocrm.py"],
      "owner": "sonnet-backend-2",
      "wave": 1,
      "depends_on": [],
      "estimated_turns": 15,
      "acceptance": [
        "Сигнатура _next_round_robin_city(self, caller: str)",
        "fallback = _ROUND_ROBIN_CITIES[zlib.crc32(caller.encode())%5]",
        "вызов на L~337 передаёт caller",
        "ast.parse без ошибок"
      ],
      "status": "pending"
    },
    {
      "id": "P1.11",
      "description": "Удалить мёртвые скрипты: auto_assign_leads.py, run_auto_assign.sh, cleanup_drugoy_city.py, run_cleanup_drugoy.sh (git rm). Константы amo_constants НЕ трогать",
      "files": [
        "scripts/auto_assign_leads.py",
        "scripts/run_auto_assign.sh",
        "scripts/cleanup_drugoy_city.py",
        "scripts/run_cleanup_drugoy.sh"
      ],
      "owner": "sonnet-ops",
      "wave": 1,
      "depends_on": [],
      "estimated_turns": 8,
      "acceptance": [
        "4 файла удалены через git rm",
        "grep ссылок в live-коде (вне .claude/worktrees, .git) пусто",
        "amo_constants.py не изменён"
      ],
      "status": "pending"
    },
    {
      "id": "P1.9-code",
      "description": "Код-часть логов: monitor.py путь лога -> /home/alisher/kurotrack/logs/worker.log + функция trim_worker_log_if_huge (>50МБ -> усечь до 20МБ) + вызов в main; .gitignore добавить logs/ с !logs/.gitkeep; создать logs/.gitkeep",
      "files": ["scripts/monitor.py", ".gitignore", "logs/.gitkeep"],
      "owner": "sonnet-ops-2",
      "wave": 1,
      "depends_on": [],
      "estimated_turns": 15,
      "acceptance": [
        "monitor.py не содержит /tmp путей",
        "trim_worker_log_if_huge реализована и вызвана в main",
        ".gitignore игнорит logs/ но хранит .gitkeep",
        "logs/.gitkeep существует"
      ],
      "status": "pending"
    },
    {
      "id": "P1.10",
      "description": "Тесты: pyproject.toml [tool.pytest.ini_options] asyncio_mode=auto; создать test_round_robin_city.py, test_retry_handle_cdr.py, test_amocrm_interface.py по скелетам из спеки; прогнать pytest tests зелёным",
      "files": [
        "backend/pyproject.toml",
        "backend/tests/test_round_robin_city.py",
        "backend/tests/test_retry_handle_cdr.py",
        "backend/tests/test_amocrm_interface.py"
      ],
      "owner": "sonnet-tester",
      "wave": 2,
      "depends_on": ["P1.6-7", "P1.8"],
      "estimated_turns": 45,
      "acceptance": [
        "asyncio_mode=auto в pyproject",
        "3 файла тестов созданы",
        "cd backend && python -m pytest tests -q — все зелёные (старые + новые)"
      ],
      "status": "pending"
    },
    {
      "id": "P1.9-server",
      "description": "Серверные операции логов: mkdir logs; правка systemd-юнита на append:/home/alisher/kurotrack/logs/worker.log (user-unit; если системный — diff + эскалация); crontab на новые пути; logrotate.conf + user-cron с --state; journalctl --user --vacuum-size. Только внутри /home/alisher, без sudo",
      "files": [
        "SERVER:/home/alisher/kurotrack/logs/",
        "SERVER:systemd user unit",
        "SERVER:crontab",
        "SERVER:/home/alisher/kurotrack/logs/logrotate.conf"
      ],
      "owner": "sonnet-deployer",
      "wave": 3,
      "depends_on": ["P1.9-code"],
      "estimated_turns": 30,
      "acceptance": [
        "worker.log пишется в logs/, /tmp/kurotrack-worker.log не растёт",
        "logrotate -d конфига без ошибок; cron-строка присутствует",
        "crontab без auto_assign/cleanup_drugoy",
        "journalctl --user --disk-usage уменьшился (или эскалация если журнал системный)"
      ],
      "status": "pending"
    },
    {
      "id": "P1.12",
      "description": "Merge fix/call-tracking-persistence в master (fast-forward, проверено), push origin master; на сервере git pull master + restart воркера + health-check; план отката задокументирован и готов",
      "files": ["GIT:master", "SERVER:/home/alisher/kurotrack"],
      "owner": "sonnet-deployer",
      "wave": 4,
      "depends_on": ["P1.6-7", "P1.8", "P1.11", "P1.9-code", "P1.10", "P1.9-server"],
      "estimated_turns": 20,
      "acceptance": [
        "git merge --ff-only успешен (master == fix)",
        "origin/master запушен",
        "сервер: git pull --ff-only + restart, /health = 200",
        "при health != 200 — откат по плану выполнен"
      ],
      "status": "pending"
    }
  ]
}
```

### Замечания по волнам и владению файлами

- В **wave 1** четыре задачи (P1.6-7, P1.8, P1.11, P1.9-code) трогают НЕПЕРЕСЕКАЮЩИЕСЯ
  наборы файлов (`call_processor.py` / `amocrm.py` / `scripts/*deleted*` / `monitor.py`+
  `.gitignore`+`logs/.gitkeep`) — безопасно параллельно.
- P1.6 и P1.7 ОБА в `call_processor.py` → объединены в одну задачу `P1.6-7` (правило: один
  файл = один owner в волне).
- **wave 2** (тесты) зависит от кода P1.6-7 и P1.8 (тестирует их новое поведение). Владеет
  `pyproject.toml` и новыми файлами тестов — с wave 1 не пересекается.
- **wave 3** (сервер) зависит от `P1.9-code` (на сервер выкатывается новый `monitor.py`).
- **wave 4** (merge) — строго последний: один git-репо, коммиты линейны; требует, чтобы всё
  выше было закоммичено в fix и тесты прошли.
- Коммиты в `fix/call-tracking-persistence` делаются последовательно координатором после
  каждой волны (один репозиторий — параллельные коммиты невозможны). Рекомендуемые
  сообщения:
  - `fix(worker): retry all transient DB errors + drop sleep(3) under semaphore (P1.6/P1.7)`
  - `fix(amo): deterministic crc32 round-robin fallback on redis failure (P1.8)`
  - `chore(scripts): remove dead auto_assign/cleanup_drugoy scripts (P1.11)`
  - `ops(logs): move logs out of /tmp, add trim + logrotate (P1.9)`
  - `test: round-robin, retry_handle_cdr, amocrm interface smoke (P1.10)`

---

## Out of Scope

- Не меняем схему БД, не создаём миграций.
- Не удаляем константы `ENUM_CITY_DRUGOY`/`STATUS_LOST` из `amo_constants.py` (используются
  живым кодом / оставлены как справочные).
- Не рефакторим `_handle_cdr` за пределами замены `sleep(3)` (локальный `import
  IntegrityError` внутри `except` оставляем).
- Не трогаем `.claude/worktrees/*` — изолированные копии других агентов.
- Не выполняем действия под `sudo` (правка `/etc/`, системный journal) — только эскалация
  человеку с готовыми командами.
