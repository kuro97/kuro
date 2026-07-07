# ARCH-lead-dedup: подавление дубля лида при наличии ОТКРЫТОГО лида у номера

> Синтез CTO-архитектора. Базлайн (прод-паритет) = `.claude/worktrees/lead-dedup`.
> Это финальная версия. Заменяет черновик `ARCH-open-lead-dedup.md` (тот оставлен как история).

## 1. Summary

Клиент, у которого в AMO уже есть **открытый** лид (в любой воронке), при повторном
входящем звонке с рекламы порождает ВТОРОЙ лид-дубль. Реальный кейс: 77052699005 утром
оставил заявку через Taplink → лид #31819407 (открыт, «встреча назначена», менеджер Дамитхан);
днём тот же человек позвонил с 2ГИС → KuroTrack создал дубль #31820171, его подхватил другой
менеджер, наделал холодных недозвонов, живой человек вручную закрыл «Дубль».

Причина: текущий дедуп ловит только мультилег одного физического звонка (SQL-окно 30 мин +
AMO-окно 5 мин). Кейс «тот же человек, другой звонок через часы» проваливается: поиска «есть ли
у номера ЛЮБОЙ открытый лид в AMO» нет; `create_lead_from_call` никогда не ищет лид клиента по
телефону вне 5-минутного окна; метода постановки задачи (`POST /api/v4/tasks`) нет.

**Фикс — минимально-хирургический.** В `create_lead_from_call`, в точке где текущий код уже
решил «свежего мультилег-лида нет, создаю новый», добавляем ОДИН запрос
`GET /api/v4/leads?query=<нормализованный номер>` и ищем среди найденных лидов ЛЮБОЙ **открытый**
(`status_id ∉ {142, 143}`, любая воронка). Если открытый лид найден — новый лид НЕ создаём:
возвращаем его id (вызывающий `_push_to_amo` сам добавит call-note и проставит `call.amo_lead_id`),
и дополнительно ставим задачу «Клиент снова обратился — перезвоните» ответственному менеджеру
этого лида через `POST /api/v4/tasks`. Если открытого лида нет (контакта нет ИЛИ только закрытые
лиды) — поведение не меняется: создаём новый лид, а закрытые lost-лиды (143) по-прежнему добивает
существующий `lead_reopen` через webhook `leads[add]`.

**Дельта:** 1 изменённый файл прод-кода (`backend/app/services/amocrm.py`) + 1 новый тест-файл.
`config.py`, `call_processor.py`, `reconciliation.py`, `lead_reopen.py`, `amo_webhook.py`, модели,
миграции — БЕЗ изменений.

**Безопасный дефолт (нельзя терять настоящий лид):** подавляем новый лид ТОЛЬКО когда есть явно
активный (открытый) лид. Любая ошибка/таймаут AMO при поиске → fail-open → создаём новый лид.

## 2. Acceptance Criteria

1. При входящем звонке от номера, у которого в AMO есть лид со `status_id ∉ {142, 143}`
   (любая воронка), новый лид НЕ создаётся; `create_lead_from_call` возвращает id этого открытого лида.
2. В том же случае к открытому лиду добавляется call-note (существующим `add_call_note`, вызывается
   из `_push_to_amo`) И создаётся ровно ОДНА задача через `POST /api/v4/tasks` с `entity_type="leads"`,
   `entity_id=<id открытого лида>`, `responsible_user_id` = менеджер этого лида, `task_type_id=1`,
   `text` содержит источник/город и слово «перезвоните».
3. `call.amo_lead_id` проставляется в id открытого лида (делает `_push_to_amo`, эта ветка не меняется).
4. Если у номера нет ни одного открытого лида (контакта нет ИЛИ только `status_id ∈ {142, 143}`) —
   создаётся новый лид ровно как сейчас (round-robin город, `/leads/complex`), задача НЕ ставится.
5. Мультилег одного физического звонка по-прежнему даёт ОДИН лид и НОЛЬ лишних задач
   (защита: `amo_lead_lock:{caller}` + SQL-дедуп 30 мин + AMO-recent 5 мин работают ДО нашей вставки).
6. `lead_reopen` (реанимация закрытых lost=143) продолжает работать без изменений: наш код срабатывает
   только на ОТКРЫТЫХ лидах, и в этом случае новый лид не создаётся, значит webhook `leads[add]` не
   генерируется. Когда лидов нет / только закрытые — создаётся новый лид → webhook → `lead_reopen` цел.
7. Открытый/закрытый различается корректно во ВСЕХ воронках: закрытые = `{142, 143}` (системные
   терминальные статусы AMO, зарезервированы и одинаковы во всех воронках), всё остальное = открыто.
8. Номер нормализуется через `normalize_phone` перед запросом в AMO (`+7…/8…/7…` → одни и те же 10 цифр).
9. Ошибка/таймаут/невалидный JSON/401/403 AMO при поиске открытого лида НЕ роняет обработку и НЕ
   подавляет создание лида (fail-open: считаем «открытого лида нет» → создаём новый — нельзя терять заявку).
10. Все существующие тесты (`backend/tests/`) проходят; добавлен новый тест-файл со всеми кейсами раздела 9.

## 3. Files to Create

| Path | Purpose | Key Functions |
|------|---------|---------------|
| `backend/tests/test_open_lead_task.py` | Юнит-тесты новой логики | тест-функции из раздела 9 (async, `asyncio_mode=auto`, паттерн как `test_round_robin_city.py`) |

## 4. Files to Modify

| Path | What Changes | Lines (approx) |
|------|-------------|----------------|
| `backend/app/services/amocrm.py` | +импорт `normalize_phone`; +2 имени в импорте `amo_constants` (`STATUS_WON`, `STATUS_LOST`); +4 модульных константы; +метод `_find_open_lead_by_caller`; +метод `_create_followup_task`; +вставка ветки «открытый лид» в `create_lead_from_call` | импорт после L13; `amo_constants` импорт L15-24; константы после L86; методы после L274 (перед `create_lead_from_call`, L276); вставка перед L343 |

Ничего больше не трогаем.

### 4.1. Импорты (amocrm.py)

Добавить после существующего `from app.core.redis import redis_client` (L14):

```python
from app.core.phone import normalize_phone
```

В существующий блок `from app.core.amo_constants import (...)` (L15-24) добавить два имени:

```python
from app.core.amo_constants import (
    FIELD_CITY,
    FIELD_DEPARTMENT,
    FIELD_UTM_SOURCE,
    FIELD_UTM_MEDIUM,
    FIELD_UTM_CAMPAIGN,
    FIELD_UTM_CONTENT,
    FIELD_UTM_TERM,
    ENUM_DEPT_OFFLINE,
    STATUS_WON,   # 142 «Успешно реализовано»
    STATUS_LOST,  # 143 «Закрыто и не реализовано»
)
```

`time` уже импортирован на L8 — переиспользуем для `complete_till`.

### 4.2. Модульные константы (amocrm.py, после L86, рядом с `_RECENT_LEAD_WINDOW_SECONDS`)

```python
# Закрытые (терминальные) системные статусы AMO: 142 «Успешно», 143 «Закрыто и не реализовано».
# Эти id зарезервированы AMO и ОДИНАКОВЫ во всех воронках, поэтому проверка
# status_id ∉ _CLOSED_STATUS_IDS корректно отличает открытый лид в любой воронке.
_CLOSED_STATUS_IDS = {STATUS_WON, STATUS_LOST}  # {142, 143}

# Тип задачи AMO: 1 = «Связаться» (встроенный дефолтный тип «перезвонить», есть в каждом аккаунте).
_TASK_TYPE_CONTACT = 1

# Дедлайн задачи «перезвоните» — через 1 час после звонка (unix-ts = now + это значение).
_TASK_DEADLINE_SECONDS = 3600

# Сколько лидов тянуть при поиске открытого лида по номеру (без временного окна).
_OPEN_LEAD_SEARCH_LIMIT = 50
```

## 5. Database Changes

Нет. Изменения только в AMO через API. Миграции не требуются.

## 6. API Contract (AMO API v4 — внешние вызовы)

### 6.1. Поиск открытого лида по номеру

`GET https://{subdomain}.amocrm.ru/api/v4/leads`

Query params:
- `query` = нормализованный номер (10 цифр, без `+`), напр. `7052699005`
- `limit` = `50`

Headers: `Authorization: Bearer <token>`, `Content-Type: application/json`.

Ответ 200 (JSON):
```json
{
  "_embedded": {
    "leads": [
      {
        "id": 31819407,
        "status_id": 47837654,
        "pipeline_id": 3321094,
        "responsible_user_id": 11220133,
        "created_at": 1720000000,
        "updated_at": 1720003600
      }
    ]
  }
}
```
- Параметр `query` на `/leads` матчит номер телефона привязанного контакта (полнотекстовый поиск
  AMO по лидам и связанным контактам). Это то же поведение, что прод уже использует в
  `_find_recent_lead_by_caller` (L204-208) — проверенный паттерн, 1 запрос.
- `status_id` и `responsible_user_id` — top-level поля лида, `with=contacts` НЕ нужен (не запрашиваем).
- `204 No Content` / пустой body / отсутствие `_embedded.leads` = у номера нет лидов.
- `401/403` (токен протух) — считаем «открытого лида нет» и возвращаем `None` (fail-open). Создание
  нового лида при этом отдельно защищено: `_find_recent_lead_by_caller` на 401/403 бросает
  `AmoAuthError` (L225-230) → `create_lead_from_call` прерывается ДО нашей вставки (L303-312) → дубль
  не появится. То есть наш fail-open безопасен: до open-search на протухшем токене дело не дойдёт.

### 6.2. Постановка задачи на лид

`POST https://{subdomain}.amocrm.ru/api/v4/tasks`

Headers: те же (`_headers()`).

Body — **МАССИВ** из одного объекта:
```json
[
  {
    "task_type_id": 1,
    "text": "Клиент снова обратился — входящий звонок с рекламы (2gis_astana, Астана). Перезвоните. Тел: 7052699005",
    "complete_till": 1720003600,
    "entity_id": 31819407,
    "entity_type": "leads",
    "responsible_user_id": 11220133
  }
]
```
- `entity_type` = `"leads"` (множественное — так требует AMO v4).
- `complete_till` — unix-timestamp (int) = `int(time.time()) + _TASK_DEADLINE_SECONDS`.
- `responsible_user_id` — включаем ТОЛЬКО если известен (менеджер лида, иначе `settings.amo_responsible_user_id`).
  Если оба None — поле опускаем (AMO поставит задачу на владельца токена).
- Успех: `200` (иногда `201`), body: `{"_embedded": {"tasks": [{"id": ...}]}}`. id нам не нужен.

## 7. Frontend Contract

Не применимо — фронтенда в задаче нет.

## 8. Новые методы AmoCRMClient (полные сигнатуры + контракты)

### 8.1. `_find_open_lead_by_caller`

```python
async def _find_open_lead_by_caller(
    self,
    client: httpx.AsyncClient,
    caller: str,
) -> dict | None:
    """Ищет ЛЮБОЙ открытый лид (status_id ∉ {142,143}, любая воронка) по номеру caller.

    Возвращает dict самого свежего открытого лида (нужны ключи 'id' и 'responsible_user_id'),
    либо None если открытых лидов нет / ошибка.
    НЕ бросает исключений (fail-open): любая ошибка/таймаут/невалидный JSON/401/403 → None,
    чтобы не подавить создание нового лида и не потерять заявку.
    """
```
Реализация (по шагам, early-return):
1. `phone = normalize_phone(caller)`; если `not phone` → `return None`.
2. `try` GET `f"{self._base_url()}/api/v4/leads"` c `params={"query": phone, "limit": _OPEN_LEAD_SEARCH_LIMIT}`,
   `headers=self._headers()`. `except httpx.TimeoutException` → `logger.warning(...)` → `return None`.
   `except Exception` → `logger.warning(...)` → `return None`.
3. Если `resp.status_code in (401, 403)` → `logger.warning("AMO: 401/403 при поиске открытого лида caller=%s — fail-open", caller)` → `return None`.
4. Если `resp.status_code != 200` → `logger.warning(...)` → `return None`. (204/иное — нет открытого лида.)
5. Если `not resp.content` → `return None`.
6. `try: data = resp.json()` `except Exception` → `logger.warning(...)` → `return None`.
7. `leads = data.get("_embedded", {}).get("leads", [])`; если пусто → `return None`.
8. `open_leads = [l for l in leads if l.get("status_id") not in _CLOSED_STATUS_IDS]`; если пусто → `return None`.
9. `return max(open_leads, key=lambda l: l.get("updated_at") or l.get("created_at") or 0)`.

Важно: НЕ применять никакого временного окна (открытый лид может быть создан утром/вчера).

### 8.2. `_create_followup_task`

```python
async def _create_followup_task(
    self,
    client: httpx.AsyncClient,
    lead_id: int,
    responsible_user_id: int | None,
    call: Call,
    caller: str,
) -> bool:
    """Ставит задачу 'перезвоните' ответственному менеджеру открытого лида.

    POST /api/v4/tasks. Возвращает True при успехе, False при ошибке.
    НЕ бросает: ошибка постановки задачи не должна ломать привязку call к лиду.
    """
```
Реализация:
1. `city = _SOURCE_TO_CITY.get(call.source) or _city_from_campaign(call.campaign)`.
2. `source_str = call.source or "неизвестный источник"`.
3. `suffix = f", {city}" if city else ""`.
4. `text = f"Клиент снова обратился — входящий звонок с рекламы ({source_str}{suffix}). Перезвоните. Тел: {caller}"`.
5. Собрать task:
   ```python
   task: dict = {
       "task_type_id": _TASK_TYPE_CONTACT,
       "text": text,
       "complete_till": int(time.time()) + _TASK_DEADLINE_SECONDS,
       "entity_id": lead_id,
       "entity_type": "leads",
   }
   ```
6. `resp_user = responsible_user_id if responsible_user_id is not None else settings.amo_responsible_user_id`;
   если `resp_user is not None` → `task["responsible_user_id"] = resp_user`.
7. `try`: `resp = await client.post(f"{self._base_url()}/api/v4/tasks", json=[task], headers=self._headers())`;
   `resp.raise_for_status()`; `logger.info("AMO: задача 'перезвоните' поставлена на лид id=%s (менеджер=%s) caller=%s", lead_id, resp_user, caller)`; `return True`.
   `except Exception`: `logger.exception("AMO: не удалось поставить задачу на лид id=%s caller=%s", lead_id, caller)`; `return False`.

### 8.3. Вставка ветки «открытый лид» в `create_lead_from_call`

**Точка врезки:** внутри `async with httpx.AsyncClient(...)`, СРАЗУ ПОСЛЕ блока
`if existing_id and not is_ours:` (заканчивается на L341 `return existing_id`) и ПЕРЕД
комментарием L343 «Ничего не нашли — создаём новый лид через /leads/complex».

То есть эта ветка выполняется ТОЛЬКО когда `_find_recent_lead_by_caller` вернул `(None, False)` —
свежего мультилег-лида нет.

```python
                # --- НОВОЕ: открытый лид у этого номера (тот же человек, другой звонок) ---
                # Свежего мультилег-лида нет. Проверяем, нет ли у номера УЖЕ открытого лида
                # (в любой воронке, status_id ∉ {142,143}). Если есть — НЕ плодим дубль:
                # возвращаем его id (вызывающий _push_to_amo сам добавит call-note и проставит
                # call.amo_lead_id) и ставим задачу ответственному менеджеру «перезвоните».
                open_lead = await self._find_open_lead_by_caller(client, caller)
                if open_lead:
                    open_id = open_lead["id"]
                    responsible = open_lead.get("responsible_user_id")
                    await self._create_followup_task(client, open_id, responsible, call, caller)
                    logger.info(
                        "AMO CRM: у caller=%s есть открытый лид id=%s — дубль подавлён, "
                        "поставлена задача менеджеру (uniqueid=%s)",
                        caller, open_id, call.uniqueid,
                    )
                    return open_id
```

**Почему тут, а не в начале метода:** `_find_recent_lead_by_caller` должен отработать ПЕРВЫМ —
он ловит мультилег нашего же только что созданного лида (`is_ours=True`, лид открыт) и Asterisk-лид
того же звонка (`is_ours=False` → PATCH). Если бы open-check стоял выше, он принял бы наш свежий
мультилег-лид за «открытый лид клиента» и поставил лишнюю задачу на собственный звонок (нарушение
AC-5). Порядок: recent(5мин, is_ours=True → return) → recent(5мин, is_ours=False → PATCH return) →
**open-search (наша ветка)** → создание нового лида.

## 9. Edge Cases & Error Handling (по функциям)

`_find_open_lead_by_caller`:
- `normalize_phone(caller)` пустой (короткий/мусорный номер) → `None` → создаём новый лид как сейчас.
- 401/403 (токен протух) → `None` (fail-open). Дубль не появится: до этой точки код уже прошёл
  `_find_recent_lead_by_caller`, который на 401/403 бросил бы `AmoAuthError` и прервал метод раньше.
- Таймаут/сетевая ошибка → `None` → создаём новый лид (нельзя терять заявку).
- 204 / пустой body / нет `_embedded.leads` → `None`.
- Невалидный JSON → `None`.
- У номера только закрытые лиды (142/143) → `open_leads` пустой → `None` → создаём новый лид →
  webhook `leads[add]` → `lead_reopen` реанимирует закрытый lost. Не сломано (AC-6).
- Несколько открытых лидов → берём самый свежий по `updated_at` (fallback `created_at`, потом 0).

`_create_followup_task`:
- `responsible_user_id` у открытого лида None → fallback `settings.amo_responsible_user_id`; если и там
  None → ключ `responsible_user_id` опускаем (AMO поставит на владельца токена).
- `POST /tasks` вернул 4xx/5xx или бросил → `False`, но `create_lead_from_call` всё равно вернёт
  `open_id` → call привяжется к открытому лиду, call-note добавится. Задача не создалась — не фатально.
- `call.source` None → в тексте «неизвестный источник», без города.

`create_lead_from_call` (ветка open):
- Мультилег-гонка (lock истёк >60с, два leg дошли до create_lead_from_call): `amo_lead_lock:{caller}`
  в `_push_to_amo` (L229, L257) держится всю AMO-секцию, поэтому одновременно внутри
  `create_lead_from_call` только один leg → одна задача (AC-5).
- Reconciliation-воркер тоже зовёт `create_lead_from_call` (reconciliation.py L60), но только для
  звонков с `amo_lead_id=None` (L56). Если open-ветка проставила `amo_lead_id`, повторного пуша не будет.

## 10. Нормализация — план и обоснование

- **Записываем `call.caller_number` как есть (сырой `src`)** — НЕ меняем (call_processor.py ~L532).
  SQL-дедуп `_find_existing_lead_for_caller` (L122-141) сравнивает `Call.caller_number == caller_number`
  (raw-to-raw, self-consistent). Менять хранение = риск сломать мультилег-дедуп → out of scope.
- **Для AMO-query нормализуем** через `normalize_phone(caller)` (последние 10 цифр). Это делает
  open-search устойчивым к разным форматам (`+7…`, `8…`, `7…`) — именно то, что не умеет SQL-слой.
  Существующий `_find_recent_lead_by_caller` использует `caller.lstrip("+")` (L200) — мы осознанно
  берём более строгую нормализацию `normalize_phone`, т.к. это требование бизнес-правила и не
  ломает recent-поиск (тот остаётся как есть).
- Замечание (не меняем): `amo_lead_lock:{caller}` использует сырой `call.caller_number` (L229), а
  `call_lock:{normalized_caller}` — нормализованный (call_processor.py L432,L438). Расхождение
  пред-существующее, вне рамок этой задачи.

## 11. Идемпотентность (один звонок = максимум одна задача)

Слои защиты, работающие ДО нашей ветки (все — в `_push_to_amo`, call_processor.py):
1. SQL pre-lock (30 мин, L236-253): второй leg находит `amo_lead_id` первого → reuse + note, НЕ доходит
   до `create_lead_from_call` → задача не ставится повторно.
2. `amo_lead_lock:{caller}` SET NX EX 60 (L257): только один параллельный leg идёт в AMO API.
3. SQL post-lock (L294-312): финальная проверка перед созданием.
4. Внутри `create_lead_from_call`: `_find_recent_lead_by_caller` (5 мин, AMO) ловит наш/Asterisk-лид того
   же звонка ДО open-search.

Вывод: до open-ветки доходит ровно один leg реального звонка → ровно одна задача. Мультилег → 0 задач.
Разные реальные звонки одного человека через часы (>30 мин): каждый = напоминание менеджеру — это
желаемое поведение «клиент снова обратился», не баг (см. Out of Scope).

## 12. Что НЕ ломаем

- **Мультилег-дедуп** (SQL 30 мин + Redis lock + AMO recent 5 мин) — наша ветка стоит ПОСЛЕ них.
- **Attribution** (`inbound_did`, round-robin город) — round-robin применяется только при создании
  нового лида (L346-349), в open-ветке мы новый лид не создаём.
- **lead_reopen** — срабатывает на webhook `leads[add]` для закрытых lost (143). Наша ветка гасит только
  ОТКРЫТЫЕ лиды и в этом случае лид не создаётся → webhook не летит. Когда лидов нет/только закрытые —
  создаём новый лид → webhook → lead_reopen работает как раньше.
- **reconciliation** — зовёт `create_lead_from_call`, автоматически получает новое поведение; повторного
  пуша нет т.к. проверяет `amo_lead_id`.
- **add_call_note / amo_lead_id** — проставляются существующим `_push_to_amo` (L313-319) на возвращённый
  `open_id`; ветку не трогаем.

## 13. Rate limits AMO

Лимит AMO ~7 req/s. `create_lead_from_call` вызывается РОВНО ОДИН раз на реальный входящий звонок,
дошедший до создания лида (мультилег отсечён раньше). Наша дельта:
- **+1 GET** (`_find_open_lead_by_caller`) на каждый такой звонок;
- **+1 POST** (`_create_followup_task`) только когда открытый лид найден (редкий «звонит снова»).

На типичном потоке (единицы звонков/сек) прибавка незначительна и не приближает к лимиту.

## 14. Test Scenarios (`backend/tests/test_open_lead_task.py`)

Паттерн — как `test_round_robin_city.py`: инстанс `AmoCRMClient()`, `unittest.mock`
(`AsyncMock`, `MagicMock`, `patch`), `asyncio_mode=auto` (плейн `async def`, без декораторов).
Мокаем переданный в метод `client` (его `.get`/`.post` через `AsyncMock`, возвращающий `MagicMock`
со `status_code`, `content`, `.json()`). `settings.amo_*` патчим через `patch`/monkeypatch.

| Test | Вход | Ожидание | Тип |
|------|------|----------|-----|
| `test_find_open_lead_returns_open` | leads с одним `status_id=47837654` | вернулся dict с этим id | unit |
| `test_find_open_lead_skips_closed_142` | единственный лид `status_id=142` | `None` | unit |
| `test_find_open_lead_skips_closed_143` | единственный лид `status_id=143` | `None` | unit |
| `test_find_open_lead_picks_newest_open` | два открытых, разные `updated_at` | вернулся с бОльшим `updated_at` | unit |
| `test_find_open_lead_204_returns_none` | resp `status_code=204`, `content=b""` | `None` | unit |
| `test_find_open_lead_non200_returns_none` | resp `status_code=500` | `None` | unit |
| `test_find_open_lead_401_returns_none` | resp `status_code=401` | `None` (fail-open, НЕ бросает) | unit |
| `test_find_open_lead_timeout_returns_none` | `client.get` бросает `httpx.TimeoutException` | `None` | unit |
| `test_find_open_lead_bad_json_returns_none` | `resp.json()` бросает | `None` | unit |
| `test_find_open_lead_empty_number_returns_none` | caller `"100"` (normalize → `100`, но пустого нет) / caller `""` (normalize → `""`) → второй кейс | `None`, `client.get` НЕ вызван | unit |
| `test_find_open_lead_normalizes_number` | caller `"+77052699005"` | в `client.get(..., params=...)` `params["query"] == "7052699005"` | unit |
| `test_create_task_payload_shape` | lead_id=1, resp=99, `call.source="2gis_astana"` | POST `/api/v4/tasks`, `body[0]` c `entity_type="leads"`, `entity_id=1`, `task_type_id=1`, `responsible_user_id=99`, `text` содержит «Перезвоните» и «Астана», `complete_till > time.time()` | unit |
| `test_create_task_fallback_responsible` | responsible=None, `settings.amo_responsible_user_id=7` | `responsible_user_id=7` в payload | unit |
| `test_create_task_omits_responsible_when_all_none` | responsible=None, settings=None | ключа `responsible_user_id` НЕТ в payload | unit |
| `test_create_task_post_fails_returns_false` | `client.post` бросает | `False`, наружу не бросает | unit |
| `test_create_lead_open_branch_suppresses_new` | `_find_recent_lead_by_caller`→`(None,False)`, `_find_open_lead_by_caller`→`{"id":31819407,"responsible_user_id":11220133}` | вернул `31819407`; `client.post(/leads/complex)` НЕ вызван; `_create_followup_task` вызван 1 раз | unit |
| `test_create_lead_no_open_creates_new` | recent→`(None,False)`, open→`None` | вызван POST `/leads/complex`; задача НЕ ставилась | unit |
| `test_create_lead_multileg_ours_no_task` | recent→`(id, is_ours=True)` | вернул тот id; `_find_open_lead_by_caller` НЕ вызван; задача НЕ ставилась | unit |
| `test_amocrm_interface_still_passes` | (существующий `test_amocrm_interface.py`) | зелёный: новые методы приватные, интерфейс воркеров цел | integration |

## 15. Tasks JSON Block

```json
{
  "tasks": [
    {
      "id": "T1",
      "description": "amocrm.py: импорт normalize_phone + STATUS_WON/STATUS_LOST; константы _CLOSED_STATUS_IDS, _TASK_TYPE_CONTACT, _TASK_DEADLINE_SECONDS, _OPEN_LEAD_SEARCH_LIMIT; методы _find_open_lead_by_caller и _create_followup_task; вставка ветки открытого лида в create_lead_from_call перед L343 (после блока is_ours=False)",
      "files": ["backend/app/services/amocrm.py"],
      "owner": "backend-implementer",
      "wave": 1,
      "depends_on": [],
      "estimated_turns": 35,
      "acceptance": [
        "Номер нормализован normalize_phone перед AMO-query; query = 10 цифр без плюса",
        "open-check вставлен ПОСЛЕ _find_recent_lead_by_caller, только в ветке (None,False)",
        "fail-open: любая ошибка/таймаут/401/403/невалидный JSON при поиске → None, лид создаётся",
        "задача ставится ровно один раз, entity_type=leads, entity_id=open_id, task_type_id=1, responsible из лида или settings или опущен",
        "детекция открытого = status_id ∉ {142,143}, без временного окна, любая воронка",
        "мультилег, round-robin, reconciliation и lead_reopen не затронуты",
        "комментарии на русском; существующие тесты проходят"
      ],
      "status": "pending"
    },
    {
      "id": "T2",
      "description": "Юнит-тесты новой логики: _find_open_lead_by_caller (открыт/закрыт-142/закрыт-143/новейший/204/non200/401/таймаут/битый JSON/пустой номер/нормализация), _create_followup_task (shape/fallback/omit/fail), ветвление create_lead_from_call (подавление дубля / создание нового / мультилег без задачи)",
      "files": ["backend/tests/test_open_lead_task.py"],
      "owner": "backend-implementer",
      "wave": 2,
      "depends_on": ["T1"],
      "estimated_turns": 30,
      "acceptance": [
        "Все тест-кейсы из раздела 14 реализованы",
        "Моки только на границе (httpx client.get/post, settings, time при необходимости)",
        "asyncio_mode=auto, плейн async def как в test_round_robin_city.py",
        "pytest в backend/ зелёный, существующие тесты не тронуты (в т.ч. test_amocrm_interface.py)"
      ],
      "status": "pending"
    }
  ]
}
```

## 16. Out of Scope

- Не меняем `config.py` (`task_type_id` и дедлайн — константы модуля; при желании вынести в settings позже).
- Не меняем нормализацию хранения `call.caller_number` (остаётся сырой `src` — консистентно с SQL-дедупом).
- Не трогаем `lead_reopen`, `amo_webhook`, `call_processor`, `reconciliation`, модели, миграции.
- Не дедуплицируем задачи между разными звонками одного человека через часы: каждый реальный повторный
  звонок = напоминание менеджеру (желаемое поведение «клиент снова обратился»).
- Не переходим на `GET /api/v4/contacts?query=&with=leads` (см. раздел 17 — отклонено как N+1).

## 17. Отклонённые альтернативы (обоснование выбора)

- **`GET /api/v4/contacts?query=<phone>&with=leads` вместо `/leads?query=`** (буквальная трактовка
  бизнес-правила «искать контакт по телефону, с его лидами»). Отклонено: embedded-лиды контакта дают
  только `id`, без `status_id` → пришлось бы фетчить каждый лид отдельно (N+1 запросов, как в
  `lead_reopen._find_closed_lead_by_contact`). `/leads?query=` возвращает `status_id` и
  `responsible_user_id` сразу в одном запросе, матчит по телефону связанного контакта (проверено прод-кодом
  `_find_recent_lead_by_caller`). Выбран как простейший корректный вариант с минимумом обращений к AMO.
- **Хранить `call.caller_number` нормализованным** — отклонено: риск сломать мультилег SQL-дедуп; не нужно
  для достижения цели (нормализуем только AMO-query).
- **Отдельный флаг/поле в БД для «задача поставлена»** — не нужно: идемпотентность обеспечена локами и
  тем, что до `create_lead_from_call` доходит один leg.
```
