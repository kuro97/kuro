#!/usr/bin/env python3
"""AGI-скрипт для Asterisk. Вызывается из dialplan при входящем звонке.
Определяет маршрут по подменному номеру (DID) через API KuroTrack.

Использование в dialplan (extensions_custom.conf):
  exten => _X.,1,AGI(call_tracking.py,${EXTEN},${CALLERID(num)})
"""

import json
import sys
import urllib.request

# Конфигурация — адрес API KuroTrack
API_URL = "http://127.0.0.1:8000/api/v1"


def read_agi_env():
    """Читает переменные окружения AGI из stdin."""
    env = {}
    while True:
        line = sys.stdin.readline().strip()
        if not line:
            break
        if ":" in line:
            key, _, value = line.partition(":")
            env[key.strip()] = value.strip()
    return env


def agi_set(name, value):
    """Устанавливает переменную канала Asterisk."""
    sys.stdout.write(f'SET VARIABLE {name} "{value}"\n')
    sys.stdout.flush()
    sys.stdin.readline()  # читаем ответ


def agi_verbose(message, level=1):
    sys.stdout.write(f"VERBOSE \"{message}\" {level}\n")
    sys.stdout.flush()
    sys.stdin.readline()


def main():
    env = read_agi_env()

    # Аргументы: DID (подменный номер), CallerID
    did = sys.argv[1] if len(sys.argv) > 1 else env.get("agi_extension", "")
    caller = sys.argv[2] if len(sys.argv) > 2 else env.get("agi_callerid", "")

    agi_verbose(f"KuroTrack AGI: DID={did}, Caller={caller}")

    # Запрашиваем маршрут у API
    try:
        data = json.dumps({"did": did, "caller": caller}).encode()
        req = urllib.request.Request(
            f"{API_URL}/tracking/resolve-did",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            result = json.loads(resp.read())

        # Устанавливаем переменные канала
        agi_set("TARGET_NUMBER", result.get("target_number", ""))
        agi_set("CAMPAIGN_ID", result.get("campaign_id", ""))
        agi_set("SESSION_ID", result.get("session_id", ""))
        agi_set("RECORD_PATH", result.get("record_path", "/var/spool/asterisk/monitor"))

        agi_verbose(f"KuroTrack: routing to {result.get('target_number', 'unknown')}")

    except Exception as e:
        agi_verbose(f"KuroTrack AGI error: {e}")
        # Fallback — маршрут на оператора по умолчанию
        agi_set("TARGET_NUMBER", "100")  # default extension
        agi_set("CAMPAIGN_ID", "")
        agi_set("SESSION_ID", "")


if __name__ == "__main__":
    main()
