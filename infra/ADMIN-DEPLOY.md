# Инструкция админу — публикация API KuroTrack

Подготовил: alisher (пользователь `/home/alisher/kurotrack/`).

Всё что мог сделать сам — сделал. Ниже 3 команды которые требуют root.

## Пред-требования (уже проверено)

- [x] nginx 1.14.1 установлен
- [x] certbot 1.22.0 установлен
- [x] Порт 8102 на 127.0.0.1 занят uvicorn'ом пользователя alisher (tmux)
- [x] Конфиг nginx проверен на синтаксис локально
- [x] JS-файл лежит `/home/alisher/kurotrack/frontend/snippet/kurotrack.js` (читается nginx'ом as-is, без exec)

## Что нужно от админа

### 1. DNS A-запись

На хостинге/регистраторе `aiplus.kz` добавить:

```
Type: A
Name: kt           (или любое другое имя — calls/track/tel/api-internal — неважно)
Value: 195.49.215.96
TTL: 300
```

Проверить после применения:
```bash
dig kt.aiplus.kz +short    # должен вернуть 195.49.215.96
```

### 2. Установить nginx-конфиг

Файл уже лежит в `/home/alisher/kurotrack/infra/nginx/kurotrack.conf`. Он referenced отсюда — **не меняй путь**, чтобы при обновлении проекта мой CI мог его обновлять.

```bash
# Один symlink достаточно:
sudo ln -s /home/alisher/kurotrack/infra/nginx/kurotrack.conf /etc/nginx/conf.d/kurotrack.conf

# ИЛИ copy, если не любишь симлинки:
sudo cp /home/alisher/kurotrack/infra/nginx/kurotrack.conf /etc/nginx/conf.d/kurotrack.conf

# Если имя поддомена будет НЕ kt.aiplus.kz — поправь server_name в файле перед копированием (одно место).

sudo nginx -t
```

### 3. Получить SSL и включить HTTPS

```bash
sudo certbot --nginx -d kt.aiplus.kz \
    --non-interactive --agree-tos -m alisher.oktl777@gmail.com --redirect

sudo systemctl reload nginx
```

certbot сам:
- получит сертификат через Let's Encrypt (webroot/nginx plugin)
- добавит `listen 443 ssl` блок
- добавит редирект 80→443
- установит cron для автоматического обновления сертификата

### 4. Проверка (со стороны админа)

```bash
curl -s https://kt.aiplus.kz/healthz
# должен вернуть JSON: {"status":"ok","ami_connected":true,"db_ok":true,...}

curl -s https://kt.aiplus.kz/kurotrack.js | head -3
# должен вернуть JS-файл (первые строки с комментарием DNI)

curl -s https://kt.aiplus.kz/some-other-path
# должен вернуть 404 — всё остальное заблокировано
```

## Безопасность

- **Правит только `/etc/nginx/conf.d/kurotrack.conf`** — никакие другие nginx-конфиги не трогаются.
- **Проксирование идёт на `127.0.0.1:8102`** — наш uvicorn. Снаружи напрямую к нему не достучаться (слушает только loopback).
- **CORS `*`** — объясняется тем что Tilda использует динамический origin. При желании сузим до `https://mektep.aiplus.kz`, скажи — поменяю.
- Методы ограничены GET/POST/OPTIONS. Никакого DELETE/PATCH снаружи.
- Статичный JS отдаётся как read-only файл, без exec.
- Моя папка `/home/alisher/kurotrack/` не имеет world-writable прав (проверяется `ls -la`).

## Отказ от работ

Если по любой причине что-то не получается (DNS не резолвится, certbot падает, nginx ругается) — скажи в чате, я разберусь на своей стороне.

## После старта

Скажи «готово» — я сразу вставлю snippet в Tilda и протестируем end-to-end.
