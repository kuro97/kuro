#!/bin/bash
# KuroTrack hourly heartbeat report — запуск из cron раз в час.
set -a
source /home/alisher/kurotrack/scripts/monitor.env
set +a
/home/alisher/kurotrack/backend/venv/bin/python /home/alisher/kurotrack/scripts/hourly_report.py >> /home/alisher/kurotrack/logs/hourly.log 2>&1
