#!/bin/bash
# ============================================================================
# KuroTrack — живой смоук-тест перед релизом.
#
# КОГДА ЗАПУСКАТЬ: перед каждым "готово"/деплоем на прод, вручную или из CI.
#   bash scripts/smoke_test.sh
#
# ЧТО ПРОВЕРЯЕТ (read-only, ничего в бою не мутирует, кроме одной идемпотентной
# DNI-брони на фиксированный session_id, которая сама протухнет через 15 мин):
#   A. Юнит/функциональные тесты backend (pytest)
#   B. Живой worker: /health + systemd
#   C. Живая БД: SELECT-запросы, свежие звонки, пул соединений
#   D. Живой API дашборда через nginx (kt.aiplus.kz)
#   E. Живой DNI: выдача номера из пула
#   F. Живой AMO CRM: валидность токена + целостность интерфейса AmoCRMClient
#   G. AMI: SIP-регистрации транков
#
# СТАТУСЫ:
#   [PASS] — проверка прошла успешно
#   [FAIL] — реальная проблема прод-системы, требует внимания ДО релиза.
#            Скрипт завершится exit 1, если было хотя бы одно [FAIL].
#   [WARN] — подозрительно, но не блокирует релиз (например: 0 звонков за
#            последние 24ч в нерабочее время, SIP-окно перерегистрации).
#            Не влияет на exit-код.
#
# Скрипт идемпотентен: можно гонять сколько угодно раз подряд без побочных
# эффектов на прод (DNI-бронь переиспользуется по фиксированному session_id,
# лиды в AMO НЕ создаются, звонки НЕ шлются, БД только читается).
# ============================================================================
set -uo pipefail

BACKEND_DIR="/home/alisher/kurotrack/backend"
PY="$BACKEND_DIR/venv/bin/python"
PSQL="PGPASSWORD=kuro psql -h 127.0.0.1 -p 5433 -U kuro -d kurotrack -t -A"
WORKER_URL="http://127.0.0.1:8102"
PUBLIC_URL="https://kt.aiplus.kz"
SMOKE_SESSION_ID="smoke-test-session"

PASS=0
FAIL=0
WARN=0

pass() { echo "[PASS] $1 — $2"; PASS=$((PASS+1)); }
fail() { echo "[FAIL] $1 — $2"; FAIL=$((FAIL+1)); }
warn() { echo "[WARN] $1 — $2"; WARN=$((WARN+1)); }

echo "=== KuroTrack SMOKE TEST $(date '+%Y-%m-%d %H:%M:%S') ==="
echo

# ----------------------------------------------------------------------------
# A. Юнит/функциональные тесты backend (offline, быстрые)
# ----------------------------------------------------------------------------
echo "--- A. Backend unit tests ---"
if [ -x "$PY" ]; then
    PYTEST_OUT=$(cd "$BACKEND_DIR" && ./venv/bin/python -m pytest -q 2>&1)
    PYTEST_RC=$?
    PYTEST_SUMMARY=$(echo "$PYTEST_OUT" | tail -1)
    if [ $PYTEST_RC -eq 0 ]; then
        pass "pytest" "$PYTEST_SUMMARY"
    else
        fail "pytest" "$PYTEST_SUMMARY"
        echo "$PYTEST_OUT" | tail -30
    fi
else
    fail "pytest" "venv не найден: $PY"
fi
echo

# ----------------------------------------------------------------------------
# B. Живой worker (health + systemd)
# ----------------------------------------------------------------------------
echo "--- B. Live worker health ---"
HEALTH_JSON=$(curl -sf --max-time 5 "$WORKER_URL/api/v1/health" 2>&1)
if [ $? -eq 0 ] && [ -n "$HEALTH_JSON" ]; then
    STATUS=$(echo "$HEALTH_JSON" | jq -r '.status // "missing"')
    AMI_OK=$(echo "$HEALTH_JSON" | jq -r '.ami_connected // false')
    DB_OK=$(echo "$HEALTH_JSON" | jq -r '.db_ok // false')
    REDIS_OK=$(echo "$HEALTH_JSON" | jq -r '.redis_ok // false')

    [ "$STATUS" = "ok" ] && pass "health.status" "$STATUS" || fail "health.status" "ожидали ok, получили: $STATUS"
    [ "$AMI_OK" = "true" ] && pass "health.ami_connected" "true" || fail "health.ami_connected" "AMI не подключен ($AMI_OK) — звонки не будут отслеживаться"
    [ "$DB_OK" = "true" ] && pass "health.db_ok" "true" || fail "health.db_ok" "БД недоступна из воркера ($DB_OK)"
    [ "$REDIS_OK" = "true" ] && pass "health.redis_ok" "true" || fail "health.redis_ok" "Redis недоступен из воркера ($REDIS_OK)"
else
    fail "health endpoint" "воркер не отвечает на $WORKER_URL/api/v1/health"
fi

SYSTEMD_STATE=$(systemctl --user is-active kurotrack-worker.service 2>&1)
[ "$SYSTEMD_STATE" = "active" ] && pass "systemd kurotrack-worker" "active" || fail "systemd kurotrack-worker" "статус: $SYSTEMD_STATE"
echo

# ----------------------------------------------------------------------------
# C. Живая БД (read-only SELECT)
# ----------------------------------------------------------------------------
echo "--- C. Live database ---"
DB_PING=$(eval "$PSQL -c \"SELECT 1;\"" 2>&1)
if [ "$DB_PING" = "1" ]; then
    pass "db connect" "SELECT 1 = 1"
else
    fail "db connect" "не удалось подключиться: $DB_PING"
fi

RECENT_CALLS=$(eval "$PSQL -c \"SELECT count(*) FROM calls WHERE started_at > now() - interval '24 hours';\"" 2>&1)
if [[ "$RECENT_CALLS" =~ ^[0-9]+$ ]]; then
    if [ "$RECENT_CALLS" -gt 0 ]; then
        pass "recent calls (24h)" "$RECENT_CALLS звонков"
    else
        warn "recent calls (24h)" "0 звонков за последние 24 часа (может быть нерабочее время)"
    fi
else
    fail "recent calls (24h)" "запрос не выполнен: $RECENT_CALLS"
fi

# Атрибуция входящих: доля звонков с source среди звонков за 24ч — статистика, не строгий FAIL
ATTRIBUTED=$(eval "$PSQL -c \"SELECT count(*) FROM calls WHERE started_at > now() - interval '24 hours' AND source IS NOT NULL;\"" 2>&1)
if [[ "$ATTRIBUTED" =~ ^[0-9]+$ ]] && [[ "$RECENT_CALLS" =~ ^[0-9]+$ ]] && [ "$RECENT_CALLS" -gt 0 ]; then
    warn "call attribution (24h)" "$ATTRIBUTED из $RECENT_CALLS звонков имеют source"
elif [[ "$RECENT_CALLS" =~ ^[0-9]+$ ]] && [ "$RECENT_CALLS" -eq 0 ]; then
    warn "call attribution (24h)" "пропущено — нет звонков за период"
fi

DB_CONNECTIONS=$(eval "$PSQL -c \"SELECT count(*) FROM pg_stat_activity WHERE datname='kurotrack';\"" 2>&1)
if [[ "$DB_CONNECTIONS" =~ ^[0-9]+$ ]]; then
    if [ "$DB_CONNECTIONS" -lt 80 ]; then
        pass "db connections" "$DB_CONNECTIONS / 80 (запас пула есть)"
    else
        fail "db connections" "$DB_CONNECTIONS >= 80 — пул почти исчерпан"
    fi
else
    fail "db connections" "запрос не выполнен: $DB_CONNECTIONS"
fi
echo

# ----------------------------------------------------------------------------
# D. Живой API дашборда через nginx
# ----------------------------------------------------------------------------
echo "--- D. Live public API (nginx) ---"
PUBLIC_HEALTH_CODE=$(curl -s --max-time 8 -o /dev/null -w "%{http_code}" "$PUBLIC_URL/api/v1/health" 2>&1)
[ "$PUBLIC_HEALTH_CODE" = "200" ] && pass "public /api/v1/health" "200" || fail "public /api/v1/health" "код $PUBLIC_HEALTH_CODE"

PROJECT_ID=$(eval "$PSQL -c \"SELECT id FROM projects LIMIT 1;\"" 2>&1)
if [ -n "$PROJECT_ID" ] && [[ "$PROJECT_ID" != *ERROR* ]]; then
    NO_AUTH_CODE=$(curl -s --max-time 8 -o /dev/null -w "%{http_code}" "$PUBLIC_URL/api/v1/calls/?project_id=$PROJECT_ID&limit=1" 2>&1)
    [ "$NO_AUTH_CODE" = "401" ] && pass "calls endpoint auth" "401 без токена (защита работает)" || fail "calls endpoint auth" "ожидали 401, получили $NO_AUTH_CODE — возможна дыра в авторизации"
else
    fail "calls endpoint auth" "не удалось получить project_id из БД: $PROJECT_ID"
fi

DASHBOARD_CODE=$(curl -sf --max-time 8 -o /dev/null -w "%{http_code}" "$PUBLIC_URL/dashboard/" 2>&1)
[ "$DASHBOARD_CODE" = "200" ] && pass "dashboard" "200" || fail "dashboard" "код $DASHBOARD_CODE"

KUROTRACK_JS=$(curl -sf --max-time 8 "$PUBLIC_URL/kurotrack.js" 2>&1)
if [ $? -eq 0 ] && echo "$KUROTRACK_JS" | grep -q "KuroTrack"; then
    pass "kurotrack.js" "200 и содержит 'KuroTrack'"
else
    fail "kurotrack.js" "недоступен или не содержит ожидаемый текст"
fi
echo

# ----------------------------------------------------------------------------
# E. Живой DNI (выдача номера, фиксированная сессия — не жрёт пул)
# ----------------------------------------------------------------------------
echo "--- E. Live DNI (get-number) ---"
API_KEY=$(eval "$PSQL -c \"SELECT api_key FROM projects LIMIT 1;\"" 2>&1)
if [ -n "$API_KEY" ] && [[ "$API_KEY" != *ERROR* ]]; then
    DNI_RESPONSE=$(curl -sf --max-time 5 -X POST "$WORKER_URL/api/v1/tracking/get-number" \
        -H "Content-Type: application/json" \
        -H "X-API-Key: $API_KEY" \
        -d "{\"client_id\": \"$SMOKE_SESSION_ID\", \"source\": \"smoke-test\"}" 2>&1)
    if [ $? -eq 0 ]; then
        DNI_PHONE=$(echo "$DNI_RESPONSE" | jq -r '.phone // empty')
        if [ -n "$DNI_PHONE" ]; then
            pass "DNI get-number" "выдан номер $DNI_PHONE (session=$SMOKE_SESSION_ID, бронь протухнет сама через 15 мин простоя)"
        else
            fail "DNI get-number" "ответ без поля phone: $DNI_RESPONSE"
        fi
    else
        fail "DNI get-number" "запрос не выполнен: $DNI_RESPONSE"
    fi
else
    fail "DNI get-number" "не удалось получить api_key из БД: $API_KEY"
fi
echo

# ----------------------------------------------------------------------------
# F. Живой AMO CRM (read-only, лид НЕ создаётся)
# ----------------------------------------------------------------------------
echo "--- F. Live AMO CRM ---"
AMO_SUBDOMAIN=$(grep -E '^KURO_AMO_SUBDOMAIN=' "$BACKEND_DIR/.env.worker" | cut -d= -f2-)
AMO_TOKEN=$(grep -E '^KURO_AMO_TOKEN=' "$BACKEND_DIR/.env.worker" | cut -d= -f2-)
if [ -n "$AMO_SUBDOMAIN" ] && [ -n "$AMO_TOKEN" ]; then
    AMO_CODE=$(curl -s --max-time 8 -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer $AMO_TOKEN" \
        "https://${AMO_SUBDOMAIN}.amocrm.ru/api/v4/account" 2>&1)
    if [ "$AMO_CODE" = "200" ]; then
        pass "AMO token valid" "GET /account -> 200"
    elif [ "$AMO_CODE" = "401" ]; then
        fail "AMO token valid" "GET /account -> 401 — токен протух, создание лидов сломано!"
    else
        fail "AMO token valid" "GET /account -> $AMO_CODE (ожидали 200)"
    fi
else
    fail "AMO token valid" "не найден KURO_AMO_SUBDOMAIN или KURO_AMO_TOKEN в .env.worker"
fi

# Проверка целостности интерфейса AmoCRMClient: все методы, вызываемые в
# call_processor.py как amocrm_client.METHOD(...), должны существовать в классе.
# Ловит регрессию вида "случайно удалили add_call_note".
AMO_INTERFACE_CHECK=$(cd "$BACKEND_DIR" && ./venv/bin/python - <<'PYEOF'
import ast
import sys

CALL_PROCESSOR = "app/workers/call_processor.py"
AMOCRM_MODULE = "app/services/amocrm.py"

with open(CALL_PROCESSOR) as f:
    processor_tree = ast.parse(f.read(), filename=CALL_PROCESSOR)

with open(AMOCRM_MODULE) as f:
    amocrm_tree = ast.parse(f.read(), filename=AMOCRM_MODULE)

# Собираем все методы класса AmoCRMClient
class_methods = set()
for node in ast.walk(amocrm_tree):
    if isinstance(node, ast.ClassDef) and node.name == "AmoCRMClient":
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                class_methods.add(item.name)

# Собираем все вызовы amocrm_client.METHOD(...) в call_processor.py
called_methods = set()
for node in ast.walk(processor_tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if isinstance(node.func.value, ast.Name) and node.func.value.id == "amocrm_client":
            called_methods.add(node.func.attr)

missing = called_methods - class_methods
if missing:
    print(f"MISSING:{','.join(sorted(missing))}")
    sys.exit(1)
else:
    print(f"OK:{','.join(sorted(called_methods))}")
    sys.exit(0)
PYEOF
)
AMO_INTERFACE_RC=$?
if [ $AMO_INTERFACE_RC -eq 0 ]; then
    pass "AmoCRMClient interface" "все вызываемые методы существуют (${AMO_INTERFACE_CHECK#OK:})"
else
    fail "AmoCRMClient interface" "$AMO_INTERFACE_CHECK — call_processor.py вызывает несуществующие методы!"
fi
echo

# ----------------------------------------------------------------------------
# G. AMI (SIP-регистрации транков)
# ----------------------------------------------------------------------------
echo "--- G. AMI SIP registrations ---"
AMI_HOST=$(grep -E '^KURO_AMI_HOST=' "$BACKEND_DIR/.env.worker" | cut -d= -f2-)
AMI_PORT=$(grep -E '^KURO_AMI_PORT=' "$BACKEND_DIR/.env.worker" | cut -d= -f2-)
AMI_USER=$(grep -E '^KURO_AMI_USERNAME=' "$BACKEND_DIR/.env.worker" | cut -d= -f2-)
AMI_SECRET=$(grep -E '^KURO_AMI_SECRET=' "$BACKEND_DIR/.env.worker" | cut -d= -f2-)
AMI_HOST=${AMI_HOST:-45.136.56.159}
AMI_PORT=${AMI_PORT:-5038}

if [ -n "$AMI_USER" ] && [ -n "$AMI_SECRET" ]; then
    REGISTERED_COUNT=$(cd "$BACKEND_DIR" && AMI_HOST="$AMI_HOST" AMI_PORT="$AMI_PORT" AMI_USER="$AMI_USER" AMI_SECRET="$AMI_SECRET" \
        ./venv/bin/python - <<'PYEOF' 2>&1
import asyncio
import os
import sys
from panoramisk import Manager


async def main():
    manager = Manager(
        host=os.environ["AMI_HOST"],
        port=int(os.environ["AMI_PORT"]),
        username=os.environ["AMI_USER"],
        secret=os.environ["AMI_SECRET"],
    )
    try:
        await asyncio.wait_for(manager.connect(), timeout=5)
    except Exception as e:
        print(f"ERROR:connect failed: {e}")
        return

    registered = 0
    try:
        future = manager.send_action({"Action": "SIPshowregistry"})
        events = await asyncio.wait_for(future, timeout=8)
        # panoramisk возвращает список событий или один Message
        rows = events if isinstance(events, list) else [events]
        for row in rows:
            state = str(row.get("State", "") or row.get("Status", "")).lower()
            if "registered" in state and "unregistered" not in state and "registering" not in state:
                registered += 1
    except Exception as e:
        print(f"ERROR:action failed: {e}")
        manager.close()
        return

    print(f"OK:{registered}")
    manager.close()


asyncio.run(main())
PYEOF
    )
    if [[ "$REGISTERED_COUNT" == OK:* ]]; then
        N=${REGISTERED_COUNT#OK:}
        if [ "$N" -ge 18 ]; then
            pass "AMI SIP registrations" "$N зарегистрировано (>= 18)"
        else
            warn "AMI SIP registrations" "$N зарегистрировано (< 18, возможно окно перерегистрации)"
        fi
    else
        warn "AMI SIP registrations" "не удалось проверить через SIPshowregistry: $REGISTERED_COUNT (health.ami_connected уже проверен выше в B)"
    fi
else
    warn "AMI SIP registrations" "AMI креды не найдены в .env.worker, пропущено (health.ami_connected уже проверен в B)"
fi
echo

# ----------------------------------------------------------------------------
# Итог
# ----------------------------------------------------------------------------
echo "=== SMOKE: $PASS passed, $FAIL failed, $WARN warn ==="
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
