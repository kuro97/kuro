# Подключение AMO CRM к KuroTrack

## 1. Получить long-lived token

1. Войти в AMO CRM под аккаунтом с правами администратора
2. Перейти: **Настройки** → **Интеграции** → кнопка **Создать интеграцию**
3. Указать название (например, `KuroTrack`), разрешения: Leads, Contacts, Notes
4. На вкладке **Ключи и токены** скопировать **Long-lived token**

> Long-lived token не истекает, но привязан к пользователю. Если пользователя удалить — токен станет недействительным.

## 2. Узнать subdomain

Subdomain — это часть URL вашего AMO до `.amocrm.ru`.

Пример: URL `https://qadam.amocrm.ru` → subdomain = `qadam`

## 3. Узнать pipeline_id и responsible_user_id

### pipeline_id (id воронки продаж)

```bash
curl -s "https://qadam.amocrm.ru/api/v4/leads/pipelines" \
  -H "Authorization: Bearer {ваш_токен}" | python3 -m json.tool
```

В ответе найти нужную воронку, скопировать значение поля `id`.

### responsible_user_id (ответственный за лид)

```bash
curl -s "https://qadam.amocrm.ru/api/v4/users" \
  -H "Authorization: Bearer {ваш_токен}" | python3 -m json.tool
```

В ответе найти нужного пользователя в `_embedded.users`, скопировать значение поля `id`.

## 4. Прописать в .env

На сервере открыть файл `.env.worker` (или `.env` в директории проекта):

```bash
nano /home/alisher/kurotrack/.env.worker
```

Добавить / заполнить блок:

```
KURO_AMO_SUBDOMAIN=qadam
KURO_AMO_TOKEN=ваш_long-lived_token
KURO_AMO_PIPELINE_ID=123456
KURO_AMO_RESPONSIBLE_USER_ID=654321
```

`KURO_AMO_PIPELINE_ID` и `KURO_AMO_RESPONSIBLE_USER_ID` — опциональны. Если не указаны, AMO создаст лид в воронке по умолчанию.

## 5. Перезапустить воркер

```bash
# Если запущен через systemd
sudo systemctl restart kurotrack-worker

# Если через screen / tmux — перезапустить вручную
```

## Поведение при отключённой интеграции

Если `KURO_AMO_SUBDOMAIN` или `KURO_AMO_TOKEN` не заполнены — интеграция тихо отключается. В логах будет warning:

```
AMO CRM не настроен (amo_subdomain/amo_token пустые) — пропускаем создание лида
```

Обработка звонков продолжает работать в штатном режиме.
