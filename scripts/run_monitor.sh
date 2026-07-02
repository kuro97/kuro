#!/bin/bash
# KuroTrack monitor — запуск из cron каждые 5 минут.
set -a
source /home/alisher/kurotrack/scripts/monitor.env
set +a
/home/alisher/kurotrack/backend/venv/bin/python /home/alisher/kurotrack/scripts/monitor.py >> /tmp/kurotrack-monitor.log 2>&1
