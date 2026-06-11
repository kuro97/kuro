#!/bin/bash
set -a
source /home/alisher/kurotrack/backend/.env.worker
set +a
/home/alisher/kurotrack/backend/venv/bin/python /home/alisher/kurotrack/scripts/auto_assign_leads.py
