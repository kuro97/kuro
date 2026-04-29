#!/bin/bash
# Добавить новый трекинг-номер в kurotrack.
#
# Что делает:
#   1. INSERT в tracking_numbers (phone, phone_normalized, number_type, source_label)
#   2. Перезапускает worker (он сам перечитает кеш DIDs + сделает pool sync)
#
# После этого надо ОТДЕЛЬНО передать админу PBX SIP-credentials
# для регистрации trunk'а в FreePBX (без этого звонки не дойдут до Asterisk).
#
# Использование:
#   ./infra/add-number.sh <phone_e164> <type> <source_label>
#
# Примеры:
#   ./infra/add-number.sh +77004982700 static instagram_2
#   ./infra/add-number.sh +77004982701 dynamic site
#   ./infra/add-number.sh +77004982702 static youtube
#
# type:         static | dynamic
# source_label: instagram | facebook | tiktok | youtube |
#               2gis_almaty | 2gis_atyrau | 2gis_aktobe | 2gis_shymkent | 2gis_astana |
#               site | google_ads | yandex_ads | crowd | flyer | ...

set -e
PHONE="${1:?'Phone в формате +77004982XXX обязателен'}"
TYPE="${2:?'Тип обязателен: static или dynamic'}"
LABEL="${3:?'source_label обязателен'}"

if [[ ! "$PHONE" =~ ^\+7[0-9]{10}$ ]]; then
    echo "❌ Phone должен быть в формате +7XXXXXXXXXX (12 цифр после +)"
    exit 1
fi

if [[ "$TYPE" != "static" && "$TYPE" != "dynamic" ]]; then
    echo "❌ Type должен быть 'static' или 'dynamic'"
    exit 1
fi

# Нормализованный = последние 10 цифр
NORMALIZED="${PHONE: -10}"

SERVER="alisher@195.49.215.96"
SERVER_PASS="${KUROTRACK_SSH_PASS:?'KUROTRACK_SSH_PASS env required'}"
PROJECT_ID="c5917a86-2fe1-4c21-ac3b-b8827ac97116"

echo "[1/3] INSERT в tracking_numbers..."
sshpass -p "$SERVER_PASS" ssh "$SERVER" "PGPASSWORD=kuro psql -h 127.0.0.1 -p 5433 -U kuro -d kurotrack <<EOF
INSERT INTO tracking_numbers (id, project_id, phone, phone_normalized, number_type, source_label, is_active, freeze_time, created_at)
VALUES (gen_random_uuid(), '$PROJECT_ID', '$PHONE', '$NORMALIZED', '$TYPE', '$LABEL', true, 3600, now())
ON CONFLICT (phone_normalized) DO UPDATE SET
    number_type = EXCLUDED.number_type,
    source_label = EXCLUDED.source_label,
    is_active = true;
SELECT phone, phone_normalized, number_type, source_label FROM tracking_numbers WHERE phone_normalized = '$NORMALIZED';
EOF"

echo ""
echo "[2/3] Restart worker (перечитает DIDs + pool sync)..."
sshpass -p "$SERVER_PASS" ssh "$SERVER" "tmux kill-session -t kurotrack-worker 2>/dev/null; sleep 1; /home/alisher/kurotrack/start-worker.sh"
sleep 4

echo "[3/3] Verify..."
sshpass -p "$SERVER_PASS" ssh "$SERVER" "tail -30 /tmp/kurotrack-worker.log | grep -iE 'loaded.*dids|pool sync' | tail -3"

echo ""
echo "✅ Номер $PHONE добавлен в БД (тип: $TYPE, источник: $LABEL)"
echo ""
echo "⚠️  ОСТАЛОСЬ: передать админу PBX SIP-credentials для регистрации trunk'а в FreePBX."
echo "    Без trunk-регистрации звонки на $PHONE не дойдут до Asterisk."
