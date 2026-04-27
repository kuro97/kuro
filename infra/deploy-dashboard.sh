#!/bin/bash
# Деплой dashboard на kt.aiplus.kz/dashboard/ — без админа.
# Ребилд + scp в /home/alisher/dashboard-build/dist/ на сервере.
# Изменения видны мгновенно — nginx читает файлы при каждом запросе (alias).

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVER="alisher@195.49.215.96"
SERVER_PASS="${KUROTRACK_SSH_PASS:-}"
DEST="/home/alisher/dashboard-build/dist"

cd "$ROOT/frontend/dashboard"
echo "[1/3] Building dashboard with base=/dashboard/ ..."
rm -rf dist
npx vite build --base=/dashboard/ 2>&1 | tail -3

echo "[2/3] Packing & uploading ..."
TAR=/tmp/dashboard-dist-$(date +%s).tar.gz
tar czf "$TAR" -C "$ROOT/frontend/dashboard" dist/

if [ -n "$SERVER_PASS" ] && command -v sshpass >/dev/null; then
    sshpass -p "$SERVER_PASS" scp "$TAR" "$SERVER:/tmp/dashboard-dist.tar.gz"
    sshpass -p "$SERVER_PASS" ssh "$SERVER" "cd /home/alisher/dashboard-build && rm -rf dist && tar xzf /tmp/dashboard-dist.tar.gz && rm /tmp/dashboard-dist.tar.gz"
else
    scp "$TAR" "$SERVER:/tmp/dashboard-dist.tar.gz"
    ssh "$SERVER" "cd /home/alisher/dashboard-build && rm -rf dist && tar xzf /tmp/dashboard-dist.tar.gz && rm /tmp/dashboard-dist.tar.gz"
fi
rm "$TAR"

echo "[3/3] Verifying ..."
curl -sI http://kt.aiplus.kz/dashboard/ | head -1

echo ""
echo "✅ Dashboard deployed → http://kt.aiplus.kz/dashboard/"
