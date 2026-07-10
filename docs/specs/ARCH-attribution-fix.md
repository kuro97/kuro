# ARCH-attribution-fix — Фикс атрибуции рекламных звонков (poisoned inbound_did + upgrade чужих строк)

> Baseline / прод-паритет: `origin/master` **2e1e81e**.
> Затрагиваемые файлы (текущие, идентичны baseline): `backend/app/services/ami_client.py`, `backend/app/workers/call_processor.py`, `backend/app/main.py`.
> Спека написана после верификации на живом проде (read-only). Все числа — из реальных запросов к `ami_events` / `calls` / Redis на 2026-07-10.
> **Ревизия v2:** T0 (остановка второго процесса, нужен админ) заменён на **код-обход** — upgrade чужих неатрибуцированных строк в `_handle_cdr`. Остановка контейнера вынесена в «Эскалация админу» (желательный ops-финал, НЕ блокер).

---

## 1. Summary

С 8 июля ~14:50 атрибуция рекламных входящих звонков мертва: дневное число атрибуцированных лидов упало с 40–75/день до 0–1/день, все звонки идут с `source=NULL` и `tracking_did` = внутренний extension (293, 282, 702, `s`) вместо рекламного DID (700498xxxx / 700480xxxx).

Корневых причин **две**, обе верифицированы, и фикс закрывает обе кодом:

1. **Отравление ключей `inbound_did` (code).** `ami_client._handle_newchannel` захватывает DID по жадному `is_our_did OR is_inbound_trunk`. С 8 июля оператор `77072374305` перестроил транзит: на from-trunk каналах (`SIP/altel_2gis_aktobe_7004982671`) в `Exten` теперь часто лежит номер оператора `77072374305` или чужой `77007544476` вместо DID. Ветка `is_inbound_trunk` пишет этот мусор в `inbound_did:{uniqueid|linkedid}`. Живьём: из 25 ключей `inbound_did:*` — 13 = `7072374305`, 2 = `7007544476`, только 10 = настоящие DID.

2. **Второй потребитель AMI выигрывает дедуп (ops → закрываем кодом).** На проде работает **docker-контейнер `api` нашего же проекта** (порт 8100:8000, `restart: unless-stopped`, собран из СТАРОГО кода; AMI-коннект из докер-сети `172.21.0.5`, в хостовом `ss` не виден — свой netns). Он второй потребитель AMI: обрабатывает CDR **раньше** правильного воркера старой логикой (`tracking_did=dst`, `linkedid=NULL`, `project_id=NULL`, **лида не создаёт** — 0 лидов за 2 дня, старый код до пуша не доходит), создаёт `Call` первым. Правильный воркер (venv, :8102, код 2e1e81e) при том же CDR натыкается на дедуп по `calls.uniqueid` (`call_processor.py` L566–573) и логирует «CDR duplicate skipped». Замеры координатора после рестарта 11:08: **21 597 «duplicate skipped» против 92 «CDR saved»**; **970 из 985** свежих `calls` без `linkedid`. Прав на docker у нас нет (alisher не в docker-группе, sudo с паролем) — остановить контейнер сами не можем.

**Что делает фикс:**
- (A) `_handle_newchannel` захватывает `inbound_did` **только** когда `Exten` — реально наш активный DID (`is_our_did`), с `nx=True` на linkedid-ключе (не перезатирать валидный). Ключи больше не отравляются.
- (B) `_handle_cdr` в ветке дедупа: если существующая `Call` создана «чужим» писателем (неатрибуцирована: `project_id IS NULL AND linkedid IS NULL`) И у нас есть атрибуция (`project_id`, т.е. `did_norm` из Redis `inbound_did` совпал с нашим DID) — **не скипаем**, а **UPDATE**'им существующую строку (tracking_did, linkedid, project_id, source-атрибуция) и идём обычным путём `_push_to_amo` (создаём лид/ноут). Контейнер лиды не создаёт → дублей лидов нет; наш AMO-дедуп (recent 5мин + SQL 30мин + amo_lead_lock) — вторая страховка.
- (C) фоновый `run_did_refresh_loop` (раз в 300с перечитывает `_our_dids`) закрывает edge-case «свежий активный DID ещё не в кеше».

**Результат:** атрибуция восстанавливается для доли звонков, где оператор доносит настоящий DID в `Exten` (≈550 ног/сутки на канале `altel`), **без остановки контейнера**. Звонки без DID (`Exten=s` через `SIP/trunk_77072374305`, ≈28.5k/сутки) и с подменённым оператором DID (≈615/сутки) кодом восстановить нельзя — это телефония (Out of Scope).

---

## 2. Acceptance Criteria

1. `ami_client._handle_newchannel` пишет `inbound_did:{uniqueid|linkedid}` **только** при `is_our_did` (норм. `Exten` ∈ `_our_dids`). Условие `is_inbound_trunk` из гейта захвата **удалено**.
2. `Exten=77072374305` (норм. `7072374305`) на любом from-trunk / SIP/trunk_ канале → запись `inbound_did:*` **НЕ производится**.
3. `Exten=77007544476` (норм. `7007544476`) → запись `inbound_did:*` **НЕ производится**.
4. `Exten ∈ {"s","h","i",""}` → `did_norm` пуст, запись **НЕ производится** (поведение сохранено).
5. `Exten=7004982690` (наш активный DID) → `inbound_did:{uniqueid}=7004982690`; для первой ноги (`uniqueid==linkedid`) второй записи по linkedid нет.
6. `inbound_did:{linkedid}` пишется с `nx=True` — записанный валидный наш DID не перезатирается последующими ногами.
7. `linkedid_for:{uniqueid}` (L187–194) и `_dispatch_with_journal` (журнал) **не изменены**.
8. `call_processor._resolve_did` **не изменяется** (порядок `inbound_did:{uniqueid}` → `inbound_did:{linkedid}` → `user_field` → `dst`).
9. **UPDATE чужой строки:** в `_handle_cdr`, при найденном дубле по `uniqueid`, если `_should_upgrade_foreign_row(existing, project_id)` истинно → строка обновляется нашей атрибуцией и вызывается `_push_to_amo`. Иначе — «duplicate skipped» как раньше.
10. **Идемпотентность:** guard `project_id is not None AND existing.project_id IS NULL AND existing.linkedid IS NULL`. Повторный наш leg (строка уже с `linkedid`/`project_id`) → skip, повторного UPDATE/лида нет. Наши собственные строки (мы всегда пишем `linkedid`) под guard не попадают.
11. Добавлен `run_did_refresh_loop()` в `ami_client`, перечитывает `_our_dids` раз в 300с; запускается/отменяется в `main.lifespan`.
12. Существующие тесты (`test_ami_journal.py`, `test_retry_handle_cdr.py`, `test_open_lead_task.py`, …) проходят без изменений. Новые тесты (раздел 9) зелёные.
13. **Проверка эффекта (после деплоя, без остановки контейнера):** доля свежих `calls` с `project_id IS NOT NULL` растёт; в логе воркера появляются «CDR upgraded foreign row»; дублей лидов в AMO по одному caller за 30 мин нет.

---

## 3. Files to Create

| Path | Purpose | Key Functions |
|------|---------|---------------|
| `backend/tests/test_inbound_did_capture.py` | Unit-тесты гейта захвата `_handle_newchannel` | `test_captures_our_did`, `test_ignores_operator_transit_number`, `test_ignores_foreign_number`, `test_ignores_exten_s`, `test_linkedid_write_uses_nx`, `test_linkedid_for_still_written` |
| `backend/tests/test_resolve_did.py` | Unit-тесты приоритета `_resolve_did` | `test_resolve_by_uniqueid`, `test_uniqueid_precedes_linkedid`, `test_resolve_by_linkedid`, `test_fallback_to_dst`, `test_fallback_to_user_field`, `test_redis_exception_falls_to_dst` |
| `backend/tests/test_cdr_upgrade_foreign_row.py` | Unit-тесты guard'а upgrade чужой строки | `test_upgrade_unattributed_foreign_with_our_did`, `test_skip_already_attributed`, `test_skip_row_with_linkedid`, `test_skip_when_no_resolution` |

---

## 4. Files to Modify

| Path | What Changes | Lines (approx, baseline 2e1e81e) |
|------|-------------|----------------------------------|
| `backend/app/services/ami_client.py` | (a) гейт захвата `if (is_our_did or is_inbound_trunk) and did_norm:` → `if is_our_did:`; удалить `is_inbound_trunk` (L158–165); (b) `inbound_did:{linkedid}` с `nx=True`; (c) `_DID_REFRESH_INTERVAL_SEC` + `run_did_refresh_loop()` | L155–181 (гейт), после L41 (loop) |
| `backend/app/workers/call_processor.py` | (a) добавить `_should_upgrade_foreign_row(existing, resolved_project_id)`; (b) заменить дедуп-ветку L566–573 на upgrade-or-skip | после L176 (helper), L566–573 (ветка) |
| `backend/app/main.py` | импорт `run_did_refresh_loop`; `create_task` + cancel на shutdown | L11, L57, L65 |

### 4.1 `ami_client.py` — гейт захвата (L155–181)

Baseline:

```python
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
                await redis_client.set(f"inbound_did:{uniqueid}", did_norm, ex=7200)
                if linkedid and linkedid != uniqueid:
                    await redis_client.set(f"inbound_did:{linkedid}", did_norm, ex=7200)
                logger.info(
                    "inbound DID captured: uniqueid=%s linkedid=%s did=%s channel=%s",
                    uniqueid, linkedid, did_norm, channel,
                )
            except Exception:
                logger.exception(
                    "Ошибка сохранения inbound DID в Redis: uniqueid=%s did=%s",
                    uniqueid, did_norm,
                )
```

Target:

```python
        did_norm = normalize_phone(exten) if exten and not exten.startswith(("s", "h", "i")) else ""
        is_our_did = bool(did_norm and did_norm in _our_dids)

        # Захватываем inbound_did ТОЛЬКО когда Exten — реально наш активный DID.
        # НИКОГДА не по одному лишь признаку транка: с 8 июля оператор 77072374305
        # перестроил транзит и кладёт свой (или чужой) номер в Exten на from-trunk
        # каналах — жадный is_inbound_trunk отравлял ключ inbound_did мусором.
        # Цена: редкий валидный, но ещё не закешированный DID пропустим (кеш
        # обновляется каждые 5 мин и на реконнекте) — записать мусор хуже.
        if is_our_did:
            try:
                # inbound_did:{uniqueid} — uniqueid уникален на канал, простой SET.
                await redis_client.set(f"inbound_did:{uniqueid}", did_norm, ex=7200)
                # inbound_did:{linkedid} — общий ключ всех ног звонка. nx=True:
                # первый валидный наш DID НЕ перезатирается последующими ногами.
                if linkedid and linkedid != uniqueid:
                    await redis_client.set(
                        f"inbound_did:{linkedid}", did_norm, ex=7200, nx=True
                    )
                logger.info(
                    "inbound DID captured: uniqueid=%s linkedid=%s did=%s channel=%s",
                    uniqueid, linkedid, did_norm, channel,
                )
            except Exception:
                logger.exception(
                    "Ошибка сохранения inbound DID в Redis: uniqueid=%s did=%s",
                    uniqueid, did_norm,
                )
```

> `context`/`channel` продолжают читаться выше (L145–146) — нужны для `event_data` и `linkedid_for`. Удаляется ТОЛЬКО блок `is_inbound_trunk` (L158–165) и его использование в гейте.

### 4.2 `ami_client.py` — `run_did_refresh_loop()` (после `_reload_our_dids`, ~L41)

```python
# Интервал перечитывания кеша наших DID (сек). Свежедобавленный активный
# номер становится «захватываемым» без ожидания реконнекта AMI. Пишем в кеш
# только валидные DID (is_our_did), поэтому редкий незакешированный DID лучше
# пропустить на ≤ этот интервал, чем один раз записать мусор оператора.
_DID_REFRESH_INTERVAL_SEC = 300


async def run_did_refresh_loop() -> None:
    """Фоновый цикл: раз в _DID_REFRESH_INTERVAL_SEC перечитывает кеш _our_dids."""
    while True:
        try:
            await asyncio.sleep(_DID_REFRESH_INTERVAL_SEC)
            await _reload_our_dids()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("run_did_refresh_loop: ошибка цикла обновления DID-кеша")
```

### 4.3 `call_processor.py` — helper `_should_upgrade_foreign_row` (после `_resolve_did`, ~L176)

```python
def _should_upgrade_foreign_row(existing: "Call", resolved_project_id: str | None) -> bool:
    """Надо ли «добить» существующую строку нашей атрибуцией.

    True только если:
      - мы сами что-то разрезолвили (resolved_project_id — наш DID нашёлся), И
      - существующая строка НЕ атрибуцирована (создана «чужим» писателем —
        старым api-контейнером: project_id=NULL и linkedid=NULL).

    Наши собственные строки всегда имеют linkedid (пишем из payload), поэтому
    под этот guard не попадают → повторного UPDATE/лида не будет (идемпотентность).
    """
    return (
        resolved_project_id is not None
        and existing.project_id is None
        and existing.linkedid is None
    )
```

> `Call` уже импортирован в модуле (L15). Аннотация — строковая для безопасности порядка объявлений.

### 4.4 `call_processor.py` — дедуп-ветка (заменить L566–573)

Baseline:

```python
            # Проверяем дубликат по uniqueid перед INSERT
            # AMI может прислать CDR дважды (после переподключения worker-а)
            existing_call_row = await db.execute(
                select(Call).where(Call.uniqueid == call.uniqueid)
            )
            if existing_call_row.scalar_one_or_none() is not None:
                logger.info("CDR duplicate skipped: uniqueid=%s", uniqueid)
                return
```

Target:

```python
            # Существующая запись по uniqueid? AMI может прислать CDR дважды
            # (реконнект/replay), И на проде звонок мог создать «чужой» писатель
            # (старый api-контейнер: tracking_did=dst, linkedid=NULL, project_id=NULL,
            # лида не создаёт). Если существующая строка не атрибутирована, а у нас
            # есть атрибуция (project_id) — ДОБИВАЕМ её и создаём лид вместо скипа.
            existing_call_row = await db.execute(
                select(Call).where(Call.uniqueid == call.uniqueid)
            )
            existing_call = existing_call_row.scalar_one_or_none()
            if existing_call is not None:
                if _should_upgrade_foreign_row(existing_call, project_id):
                    existing_call.tracking_did = call.tracking_did
                    existing_call.linkedid = call.linkedid
                    existing_call.project_id = call.project_id
                    existing_call.source = call.source
                    existing_call.medium = call.medium
                    existing_call.campaign = call.campaign
                    existing_call.keyword = call.keyword
                    existing_call.is_unique = call.is_unique
                    existing_call.is_target = call.is_target
                    if call.recording_url:
                        existing_call.recording_url = call.recording_url
                    await db.commit()
                    logger.info(
                        "CDR upgraded foreign row: uniqueid=%s did=%s project=%s",
                        uniqueid, did_norm, project_id,
                    )
                    # Обычный путь атрибуции: создаём/переиспользуем лид.
                    await _push_to_amo(db, existing_call, src, uniqueid)
                    return
                logger.info("CDR duplicate skipped: uniqueid=%s", uniqueid)
                return
```

> Остальной путь (L575+: `db.add(call)` → commit → IntegrityError-guard → `_push_to_amo(db, call, ...)`) остаётся без изменений — он отрабатывает, когда дубля нет (existing_call is None). После `return` в upgrade-ветке хвостовая очистка (`active_calls.pop`, delete lock, L648–656) не выполняется — так же, как в текущей ветке skip (lock снимется по TTL 120с). Существующее поведение, регрессии нет.

### 4.5 `main.py`

Импорт (L11):

```python
from app.services.ami_client import ami_client, run_did_refresh_loop
```

В `lifespan` (после L57 `journal_cleanup_task = ...`):

```python
    # Периодическое обновление кеша наших DID (новый активный номер станет
    # захватываемым без ожидания реконнекта AMI).
    did_refresh_task = asyncio.create_task(run_did_refresh_loop())
```

В shutdown (рядом с L62–65):

```python
    did_refresh_task.cancel()
```

---

## 5. Database Changes

**Нет.** Схема не меняется. Отравленные Redis-ключи `inbound_did:*` не требуют миграции — истекают по TTL 7200с; новые звонки получают новые `uniqueid`/`linkedid`. Upgrade чужих строк — обычный `UPDATE` через ORM (параметризовано SQLAlchemy), без DDL. Никакого `DELETE`/`DROP`.

---

## 6. API Contract

**Нет изменений.** Фикс полностью внутри воркера AMI (backend, без HTTP).

---

## 7. Frontend Contract

**Нет изменений.** Восстановление атрибуции проявится в существующих отчётах (`source`/`project_id` перестанут быть NULL для рекламных звонков).

---

## 8. Edge Cases & Error Handling

### `_handle_newchannel` (гейт захвата)
- `Exten = наш DID 7004982690` → пишем `inbound_did:{uniqueid}`. Happy path.
- `Exten = 77072374305` (оператор) → норм. `7072374305` ∉ `_our_dids` → **ничего не пишем**. Суть фикса.
- `Exten = 77007544476` (чужой) → **ничего не пишем**.
- `Exten = s` (канал `SIP/trunk_77072374305`) → `did_norm=""` → **ничего не пишем**. DID отсутствует физически (Out of Scope).
- `Exten = 77004982690` (наш DID с кодом страны) → норм. `7004982690` ∈ кеша → пишем.
- `linkedid == uniqueid` (первая нога) → только `inbound_did:{uniqueid}`; читается CDR-ногой как `inbound_did:{linkedid}` (значения равны).
- `linkedid` уже с валидным нашим DID, вторая наша-DID нога → `nx=True` сохраняет первый.
- **валидный DID не в кеше** → пропускаем на ≤300с, до `run_did_refresh_loop`. Записать номер оператора в общий ключ хуже, чем разово пропустить.
- Redis недоступен при записи → `try/except` логирует, обработку не роняет.

### `_handle_cdr` (upgrade чужой строки)
- **дубль неатрибуцирован (project_id NULL, linkedid NULL) + у нас project_id есть** → UPDATE + `_push_to_amo` (лид создаётся). Основной сценарий обхода контейнера.
- **дубль уже атрибуцирован** (`existing.project_id` не NULL) → guard False → skip (не портим готовую строку).
- **дубль — наша собственная строка** (`existing.linkedid` не NULL) → guard False → skip (идемпотентность: повторного лида нет).
- **у нас нет резолва** (`project_id is None`, напр. Redis-ключа нет / `Exten=s`) → guard False → skip как раньше (мусор поверх чужой строки не пишем).
- **гонка: контейнер записал позже нас** → мы вставили атрибуцированную строку первыми (existing_call is None → обычный INSERT); контейнер получит `IntegrityError` по unique(uniqueid) и отвалится. DB-констрейнт защищает; guard гарантирует, что атрибуцированную строку не «понизят».
- **дубли лидов** → исключены: контейнер лидов не создаёт (0 за 2 дня), плюс `_push_to_amo` дедуп (SQL 30мин + `amo_lead_lock` NX + post-lock SQL).
- **`db.commit()` в upgrade падает транзиентно** → пробросится в `_retry_handle_cdr` (ретрай транзиентных сбоёв БД); IntegrityError сюда не дойдёт (мы делаем UPDATE, не INSERT).

### `_resolve_did` (без изменений, фиксируем тестами)
- `inbound_did:{uniqueid}` есть → возвращает его (не зависит от linkedid).
- только `inbound_did:{linkedid}` → возвращает его.
- ни того ни другого → `user_field or dst`.
- Redis-исключение → `try/except`, падает в `dst`.

### `run_did_refresh_loop`
- Ошибка `_reload_our_dids` (БД недоступна) → проглатывается внутри `_reload_our_dids`, кеш прежний; `CancelledError` завершает цикл на shutdown.

---

## 9. Test Scenarios

Pytest-async, БД/Redis мокаются (`monkeypatch`/`AsyncMock`), реальных соединений нет. Стиль — как `backend/tests/test_ami_journal.py`.

### `test_inbound_did_capture.py` (мок `app.services.ami_client.redis_client`, `_dispatch_with_journal`; `_our_dids` через monkeypatch)

| Test | Input (message dict) | Expected |
|------|----------------------|----------|
| `test_captures_our_did` | `Exten=7004982690, Uniqueid=U1, Linkedid=U1, Channel=SIP/altel_...-x, Context=from-trunk`; `_our_dids={"7004982690"}` | `set("inbound_did:U1","7004982690",ex=7200)`; ветка linkedid не вызвана |
| `test_ignores_operator_transit_number` | `Exten=77072374305, Context=from-trunk, Channel=SIP/altel_...-x`; `_our_dids={"7004982690"}` | среди `set`-вызовов нет ключа `inbound_did:*` |
| `test_ignores_foreign_number` | `Exten=77007544476, Context=from-trunk` | нет `inbound_did:*` записи |
| `test_ignores_exten_s` | `Exten=s, Channel=SIP/trunk_77072374305-x, Context=from-trunk` | нет `inbound_did:*` записи |
| `test_linkedid_write_uses_nx` | `Exten=7004982690, Uniqueid=U2, Linkedid=L2` (L2!=U2) | два `set`: `inbound_did:U2` и `inbound_did:L2` с `nx=True` |
| `test_linkedid_for_still_written` | `Uniqueid=U3, Linkedid=L3, Exten=268` (не наш DID) | `set("linkedid_for:U3","L3",ex=3600)` вызван (регресс-гард) |

> `monkeypatch.setattr("app.services.ami_client._our_dids", {"7004982690"})`; `redis_client.set` = `AsyncMock`, `nx` через `call.kwargs.get("nx")`; `_dispatch_with_journal` = `AsyncMock`. Вызов: `await AMIClient()._handle_newchannel(None, <dict>)`.

### `test_resolve_did.py` (мок `app.workers.call_processor.redis_client`)

| Test | Input | Expected |
|------|-------|----------|
| `test_resolve_by_uniqueid` | `inbound_did:U→"7004982690"`, `inbound_did:L→None`; `_resolve_did("U","L",None,"702")` | `"7004982690"` |
| `test_uniqueid_precedes_linkedid` | `inbound_did:U→"7004982690"`, `inbound_did:L→"7004980117"` | `"7004982690"` |
| `test_resolve_by_linkedid` | `inbound_did:U→None`, `inbound_did:L→"7004982690"` | `"7004982690"` |
| `test_fallback_to_dst` | оба None, `user_field=None`, `dst="702"` | `"702"` |
| `test_fallback_to_user_field` | оба None, `user_field="7004982690"`, `dst="702"` | `"7004982690"` |
| `test_redis_exception_falls_to_dst` | `redis.get` бросает `Exception`, `dst="702"` | `"702"` |

> `redis_client.get` = `AsyncMock(side_effect=fake_get)`, где `fake_get(key)` возвращает значение по ключу.

### `test_cdr_upgrade_foreign_row.py` (чистый guard — быстрые тесты)

Тестируем `_should_upgrade_foreign_row(existing, resolved_project_id)`. `existing` — лёгкий стаб с атрибутами `project_id`, `linkedid` (напр. `types.SimpleNamespace`).

| Test | existing | resolved_project_id | Expected |
|------|----------|---------------------|----------|
| `test_upgrade_unattributed_foreign_with_our_did` | `project_id=None, linkedid=None` | `"proj-uuid"` | `True` (дубль-без-атрибуции + наш DID → UPDATE+лид) |
| `test_skip_already_attributed` | `project_id="p", linkedid=None` | `"proj-uuid"` | `False` (уже атрибуцирован → skip) |
| `test_skip_row_with_linkedid` | `project_id=None, linkedid="L"` | `"proj-uuid"` | `False` (наша строка → skip, идемпотентность) |
| `test_skip_when_no_resolution` | `project_id=None, linkedid=None` | `None` | `False` (нет Redis-ключа/резолва → skip как раньше) |

> Опционально (если позволяет бюджет задачи) — один интеграционный тест `_handle_cdr` с моками (`async_session`, `redis_client`, `_find_session_by_did`, `classify_call`, `recording_service`, `amocrm_client`): при найденном неатрибуцированном дубле вызывается `db.commit` + `_push_to_amo`, при атрибуцированном — нет. Не обязателен: guard покрыт unit-тестами выше.

**Регресс:** прогнать весь `backend/tests/` — существующие тесты остаются зелёными. Команда (из `backend/`): `./.venv/bin/pytest -q` (или `venv/bin/pytest`).

---

## Эскалация админу (желательный ops-финал, НЕ блокер)

> Код-обход (раздел 4.4) восстанавливает атрибуцию **без** этих действий. Ниже — «чистый» финал, который убирает лишнюю работу и гонку; выполняет админ сервера (у нас нет прав docker/sudo).

1. **Остановить docker-контейнер `api`** (старый код, второй потребитель AMI). После этого правильный воркер станет единственным создателем `Call`, upgrade-ветка перестанет срабатывать (existing_call всегда None → обычный INSERT сразу с атрибуцией).
2. **AGI `resolve-did`.** AGI (`asterisk/agi/call_tracking.py`) стучится в `http://127.0.0.1:8000/api/v1/tracking/resolve-did` (порт контейнера, наружу 8100). При остановке контейнера перенаправить AGI на действующий API (порт воркера/боевого API), иначе маршрутизация DNI сломается. Проверить после переключения, что `resolve-did` отвечает.
3. Подтвердить: один ESTAB к asterisk:5038; в логе воркера растёт «CDR saved», падает «CDR duplicate skipped» / «CDR upgraded foreign row».

**Почему не блокер:** пока контейнер жив, наш код добивает его неатрибуцированные строки и сам создаёт лиды. Остановка — оптимизация (меньше двойной работы, нет гонки), а не условие работы фикса.

---

## Out of Scope (телефония — кодом не восстановить)

Оценка по `ami_events` за 24ч (входящие from-trunk / SIP/trunk_ ноги):

| Категория | Ноги/24ч | Канал | Кодом? |
|-----------|----------|-------|--------|
| **B — Exten = наш активный DID** | **~550** | `SIP/altel_2gis_aktobe_7004982671` | **ДА — восстанавливается фиксом** |
| C — Exten = номер оператора/чужой (`7072374305`/`7007544476`) | ~615 | `SIP/altel_2gis_aktobe_7004982671` | НЕТ: настоящий DID утерян оператором в Exten |
| A — Exten = `s`/пусто (DID физически отсутствует) | ~28531 | преим. `SIP/trunk_77072374305` | НЕТ: транзит без DID |

**Доля восстановимого кодом:** среди рекламных ног с внешним номером в `Exten` (B+C=1165/сутки) оператор доносит настоящий DID лишь в **~47%** (550/1165). Плюс основной объём (~28.5k/сутки) — `SIP/trunk_77072374305` с `Exten=s` (DID отсутствует физически, преим. не-рекламный транзит).

**Вывод:** код-фикс вернёт атрибуцию для B-доли (порядок исторических 40–75/сутки для DID-несущих звонков). Полное восстановление требует **телефонийной миграции на прямые DID-транки** (чтобы DID всегда приходил в `Exten`/SIP INVITE, а не подменялся транзитом `77072374305`). Вне кода KuroTrack.

**Что НЕ ломаем:** мультилег-дедуп AMO (`_push_to_amo`), журнал/replay (`_dispatch_with_journal`, `ami_journal`), reconciliation-воркер, `_resolve_did`/основной путь `_handle_cdr` (INSERT-ветка), запись `linkedid_for`. Изменения — только гейт захвата `inbound_did`, upgrade-ветка дедупа и фоновый refresh-цикл.

---

## 10. Tasks JSON Block

```json
{
  "tasks": [
    {
      "id": "T1",
      "description": "ami_client.py: гейт захвата inbound_did = только is_our_did (удалить is_inbound_trunk); inbound_did:{linkedid} с nx=True; добавить _DID_REFRESH_INTERVAL_SEC и run_did_refresh_loop(). main.py: импортировать/запустить/отменить run_did_refresh_loop в lifespan.",
      "files": [
        "backend/app/services/ami_client.py",
        "backend/app/main.py"
      ],
      "owner": "backend-implementer",
      "wave": 1,
      "depends_on": [],
      "estimated_turns": 30,
      "acceptance": [
        "Гейт захвата = только is_our_did, is_inbound_trunk удалён",
        "inbound_did:{linkedid} пишется с nx=True",
        "linkedid_for и _dispatch_with_journal не тронуты",
        "run_did_refresh_loop добавлен и запускается/отменяется в lifespan",
        "Комментарии на русском"
      ],
      "status": "pending"
    },
    {
      "id": "T2",
      "description": "call_processor.py: добавить _should_upgrade_foreign_row(existing, resolved_project_id); в _handle_cdr заменить дедуп-ветку (L566-573) на upgrade-or-skip — при неатрибуцированном чужом дубле (project_id NULL, linkedid NULL) и наличии нашего project_id UPDATE строки + _push_to_amo, иначе skip. _resolve_did НЕ трогать.",
      "files": [
        "backend/app/workers/call_processor.py"
      ],
      "owner": "backend-implementer",
      "wave": 1,
      "depends_on": [],
      "estimated_turns": 30,
      "acceptance": [
        "_should_upgrade_foreign_row: True только при resolved_project_id!=None AND existing.project_id IS NULL AND existing.linkedid IS NULL",
        "Upgrade делает UPDATE атрибуции + _push_to_amo, затем return",
        "Уже атрибуцированный / свой (linkedid!=NULL) дубль — skip",
        "_resolve_did и INSERT-ветка без изменений",
        "SQL параметризован (ORM), комментарии на русском"
      ],
      "status": "pending"
    },
    {
      "id": "T3",
      "description": "Тесты: test_inbound_did_capture.py (захват своего DID / игнор 77072374305 / игнор 77007544476 / игнор Exten=s / nx на linkedid / linkedid_for сохранён), test_resolve_did.py (uniqueid/приоритет/linkedid/dst/user_field/исключение), test_cdr_upgrade_foreign_row.py (upgrade неатрибуцированного+наш DID / skip атрибуцированного / skip со своим linkedid / skip без резолва). Прогнать весь backend/tests — регресс зелёный.",
      "files": [
        "backend/tests/test_inbound_did_capture.py",
        "backend/tests/test_resolve_did.py",
        "backend/tests/test_cdr_upgrade_foreign_row.py"
      ],
      "owner": "tester",
      "wave": 2,
      "depends_on": ["T1", "T2"],
      "estimated_turns": 35,
      "acceptance": [
        "Все новые тесты зелёные (happy + edge: захват/игнор/nx/fallback/upgrade/skip)",
        "Существующие тесты не изменены и проходят",
        "Только моки, без sleep/реальных соединений"
      ],
      "status": "pending"
    },
    {
      "id": "T-admin",
      "description": "ЭСКАЛАЦИЯ АДМИНУ (желательный ops-финал, НЕ блокер): остановить docker-контейнер api (старый код, 2-й потребитель AMI) и перенаправить AGI resolve-did с :8000/:8100 на действующий API. У команды нет прав docker/sudo.",
      "files": [],
      "owner": "human-ops",
      "wave": 3,
      "depends_on": [],
      "estimated_turns": 0,
      "acceptance": [
        "Один потребитель AMI (один ESTAB к asterisk:5038)",
        "AGI resolve-did отвечает после переключения",
        "Upgrade-ветка перестаёт срабатывать (existing_call всегда None)"
      ],
      "status": "pending"
    }
  ]
}
```

### Waves
- **Wave 1 (параллельно, разные файлы):** T1 (`ami_client.py`+`main.py`) и T2 (`call_processor.py`). Пересечений файлов нет.
- **Wave 2:** T3 — тесты (новые файлы). Зависит от T1+T2.
- **Wave 3 (не блокер, человек):** T-admin — остановка контейнера + AGI. Можно выполнять в любой момент, эффект фикса от неё не зависит.

> File ownership: Wave 1 — `ami_client.py`+`main.py` только у T1, `call_processor.py` только у T2. Wave 2 — только новые тест-файлы. Конфликтов нет.
