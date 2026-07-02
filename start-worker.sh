#!/bin/bash
# KuroTrack AMI worker — автозапуск и health-check каждую минуту через cron.
# БЕЗ set -e: pkill/tmux ошибки не должны нас останавливать.

SESSION="kurotrack-worker"
PROJECT="/home/alisher/kurotrack/backend"
LOG="/tmp/kurotrack-worker.log"
HEALTH_URL="http://127.0.0.1:8102/api/v1/health"

# Если health отвечает — всё ок.
if curl -s -m 3 -f -o /dev/null "$HEALTH_URL"; then
    exit 0
fi

echo "[$(date)] /health не отвечает — перезапуск worker." >> /tmp/kurotrack-restart.log

# Чистим всё что могло остаться (ошибки игнорируем — || true)
tmux kill-session -t "$SESSION" 2>/dev/null || true
pkill -u alisher -9 -f "uvicorn app.main" 2>/dev/null || true
sleep 2

# Стартуем
cd "$PROJECT" || exit 1
tmux new-session -d -s "$SESSION" \
    "ulimit -n 65536 && set -a && source .env.worker && set +a && \
     ./venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8102 \
     --limit-max-requests 10000 \
     2>&1 | tee $LOG"

# Проверка что tmux реально создалась (не упала на старте)
sleep 3
if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "[$(date)] FAIL: tmux new-session не создалась" >> /tmp/kurotrack-restart.log
    exit 1
fi
echo "[$(date)] worker запущен в tmux" >> /tmp/kurotrack-restart.log
