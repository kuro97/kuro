# ARCH-lead-reopen: Реанимация закрытых лидов вместо создания дубликатов

## Проблема

Когда OPS не может дозвониться клиенту 7 дней, Salesbot "Закрытие 3 ндз" закрывает лид
(pipeline "Новые продажи" id=3321094, status_id=143 "Закрыто и не реализовано",
custom field "Причина отказа" id=878831 = "Гасится").

Если клиент потом звонит/пишет — AMO CRM создаёт **новый** лид с тем же контактом,
но **без UTM/маркетинговых данных**. Это ломает атрибуцию и плодит дубликаты.

## Решение

Webhook handler на стороне kurotrack. Когда AMO присылает `leads[add]`:

1. Получить данные нового лида из API (контакт, телефон)
2. Найти закрытые лиды того же контакта с "Причина отказа" = "Гасится"
3. Если найден — **реанимировать** старый лид и **закрыть** новый как дубликат

## Ключевые ID AMO (аккаунт qadam)

| Сущность | ID |
|----------|-----|
| Pipeline "Новые продажи" | 3321094 |
| Status "Клиент реанимирован" | 48026560 |
| Status "Закрыто и не реализовано" | 143 |
| Field "Причина отказа" | 878831 |
| Enum value "Гасится" | нужен enum_id, пока используем text match |
| Field "Сделка завершена" | 879295 |
| Field "call attempt" | 912743 |

## Архитектура

### Новый сервис: `backend/app/services/lead_reopen.py`

```python
class LeadReopenService:
    """Реанимация закрытых лидов при повторном обращении клиента."""

    async def check_and_reopen(self, new_lead_id: int) -> bool:
        """Проверяет новый лид. Если это дубликат закрытого — реанимирует старый.

        Возвращает True если произошла реанимация (новый лид закрыт как дубликат).
        """

    async def _get_lead_contacts(self, lead_id: int) -> list[int]:
        """Возвращает contact_id привязанные к лиду."""

    async def _find_closed_lead_by_contact(self, contact_id: int, exclude_lead_id: int) -> dict | None:
        """Ищет закрытый лид контакта с причиной 'Гасится'."""

    async def _reopen_lead(self, lead_id: int) -> bool:
        """Переводит лид в статус 'Клиент реанимирован', очищает причину отказа."""

    async def _close_as_duplicate(self, lead_id: int, original_lead_id: int) -> bool:
        """Закрывает новый лид как дубликат с пометкой на оригинал."""
```

### Модификация: `backend/app/api/v1/amo_webhook.py`

Добавить обработку `leads[add]` — после парсинга lead_ids вызвать
`lead_reopen.check_and_reopen(lead_id)` для каждого нового лида.

### Без изменений

- `config.py` — не нужны новые переменные, используем существующие amo_subdomain/amo_token
- `amocrm.py` — не трогаем, новая логика в отдельном сервисе
- `call_processor.py` — не трогаем
- Миграции — не нужны (изменения только в AMO через API)

## API Flow

### 1. Webhook получает leads[add]

```
POST /api/v1/amo/webhook
leads[add][0][id]=99999
```

### 2. Получаем контакты нового лида

```
GET /api/v4/leads/99999?with=contacts
→ _embedded.contacts[0].id = 12345
```

### 3. Ищем закрытые лиды контакта

```
GET /api/v4/contacts/12345?with=leads
→ перебираем leads, ищем status_id=143
```

### 4. Проверяем причину отказа

```
GET /api/v4/leads/{closed_lead_id}
→ custom_fields_values → field_id=878831 → value == "Гасится"
```

### 5. Реанимируем старый лид

```
PATCH /api/v4/leads/{closed_lead_id}
{
  "pipeline_id": 3321094,
  "status_id": 48026560,
  "custom_fields_values": [
    {"field_id": 878831, "values": [{"value": ""}]},
    {"field_id": 912743, "values": [{"value": "0"}]}
  ]
}
```

### 6. Закрываем новый лид как дубликат

```
PATCH /api/v4/leads/99999
{
  "status_id": 143,
  "loss_reason_id": null,
  "custom_fields_values": [
    {"field_id": 878831, "values": [{"value": "Дубликат — реанимирован старый лид"}]}
  ]
}
```

### 7. Добавляем заметку к реанимированному лиду

```
POST /api/v4/leads/{closed_lead_id}/notes
[{
  "note_type": "common",
  "params": {
    "text": "Лид реанимирован автоматически. Клиент повторно обратился. Дубликат лид #99999 закрыт."
  }
}]
```

## Защита от бесконечного цикла

Когда мы закрываем новый лид как дубликат, AMO пришлёт webhook `leads[status]`.
Когда мы реанимируем старый лид, AMO тоже пришлёт webhook `leads[status]`.

Защита:
- Redis lock `lead_reopen:{contact_id}` с TTL 60с — предотвращает параллельную обработку
- Проверяем что новый лид НЕ имеет "Причина отказа" = "Дубликат" (значит мы его сами закрыли)
- Обрабатываем ТОЛЬКО `leads[add]`, не `leads[status]` для реанимации

## Ограничения

- Работает только для лидов в pipeline "Новые продажи" (3321094)
- Реанимирует только лиды с "Причина отказа" = "Гасится" (автозакрытие по недозвону)
- Если у контакта несколько закрытых лидов — берём последний (самый свежий)

## Тесты

Юнит-тесты для LeadReopenService:
- `test_reopen_when_closed_lead_exists` — happy path
- `test_no_reopen_when_no_closed_lead` — нет закрытого лида
- `test_no_reopen_when_reason_not_gasitsya` — закрытый лид есть но причина другая
- `test_no_reopen_when_different_pipeline` — лид в другой воронке
- `test_duplicate_closed_correctly` — новый лид закрыт с правильной пометкой

## JSON Tasks

```json
{
  "tasks": [
    {
      "id": "T1",
      "title": "Создать LeadReopenService",
      "files": ["backend/app/services/lead_reopen.py"],
      "wave": 1,
      "owner": "impl-1"
    },
    {
      "id": "T2",
      "title": "Интегрировать в webhook handler",
      "files": ["backend/app/api/v1/amo_webhook.py"],
      "wave": 2,
      "owner": "impl-1",
      "depends": ["T1"]
    }
  ]
}
```
