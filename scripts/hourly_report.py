#!/usr/bin/env python3
"""KuroTrack hourly health report.
Запускается из cron раз в час (в :00). В отличие от monitor.py (который
молчит, если всё ок, и алёртит только при поломке), этот скрипт всегда
шлёт ОДНО сообщение-снимок состояния — heartbeat, подтверждающий что
система и мониторинг живы.

Env (через scripts/monitor.env):
  KURO_DATABASE_URL
  KURO_AMI_HOST, KURO_AMI_PORT, KURO_AMI_USERNAME, KURO_AMI_SECRET
  KURO_TG_BOT_TOKEN, KURO_TG_CHAT_ID
"""
import asyncio, os, sys, datetime, subprocess, urllib.request, urllib.parse, json

sys.path.insert(0, "/home/alisher/kurotrack/backend")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

TIMEZONE_OFFSET = 5  # Алматы = UTC+5
EXPECTED_REGS = 18
HEALTH_URL = "http://127.0.0.1:8102/api/v1/health"
DB_MAX_CONN = 80
DB_WARN_CONN = 70

# Строки отчёта и общий флаг проблем собираем сюда
lines = []
has_problem = False


def add_line(text_line: str, ok: bool):
    """Добавить строку отчёта. Если ok=False — помечаем весь отчёт как проблемный."""
    global has_problem
    if not ok:
        has_problem = True
    lines.append(text_line)


def fmt_uptime(seconds: float) -> str:
    """Форматирует секунды в 'Xд Yч' / 'Yч Zм' / 'Zм'."""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}д {hours}ч"
    if hours > 0:
        return f"{hours}ч {minutes}м"
    return f"{minutes}м"


def check_worker():
    """1. Worker: активен ли systemd unit + аптайм с момента ActiveEnterTimestamp."""
    try:
        state = subprocess.run(
            ["systemctl", "--user", "is-active", "kurotrack-worker.service"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        active = state == "active"

        uptime_str = ""
        try:
            out = subprocess.run(
                ["systemctl", "--user", "show", "kurotrack-worker.service", "-p", "ActiveEnterTimestamp"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            # формат: ActiveEnterTimestamp=Thu 2026-07-02 10:37:45 +05
            # systemd отдаёт таймзону как "+05" (без минут) — strptime с %z
            # требует "+0500", поэтому дополняем нулями до 5 символов
            ts_raw = out.split("=", 1)[1].strip() if "=" in out else ""
            if ts_raw:
                parts = ts_raw.rsplit(" ", 1)
                if len(parts) == 2 and (parts[1].startswith("+") or parts[1].startswith("-")):
                    tz = parts[1]
                    tz = tz + "0" * (5 - len(tz))
                    ts_raw = f"{parts[0]} {tz}"
                dt = datetime.datetime.strptime(ts_raw, "%a %Y-%m-%d %H:%M:%S %z")
                now = datetime.datetime.now(dt.tzinfo)
                uptime_str = f" (аптайм {fmt_uptime((now - dt).total_seconds())})"
        except Exception:
            pass  # аптайм не критичен, показываем без него

        if active:
            add_line(f"✅ Worker: работает{uptime_str}", True)
        else:
            add_line(f"⚠️ Worker: НЕ активен (статус: {state})", False)
    except Exception as e:
        add_line(f"⚠️ Worker: не смог проверить ({e})", False)


def check_health():
    """2. API /api/v1/health — ami_connected, db_ok, redis_ok."""
    try:
        req = urllib.request.Request(HEALTH_URL)
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        ami_ok = bool(data.get("ami_connected"))
        db_ok = bool(data.get("db_ok"))
        redis_ok = bool(data.get("redis_ok"))
        return ami_ok, db_ok, redis_ok
    except Exception as e:
        add_line(f"⚠️ Health API недоступен: {e}", False)
        return None, None, None


async def fetch_sip_registry():
    """Один замер SIPshowregistry через AMI (подход как в monitor.py)."""
    from panoramisk import Manager
    mgr = Manager(
        host=os.environ["KURO_AMI_HOST"],
        port=int(os.environ.get("KURO_AMI_PORT", "5038")),
        username=os.environ["KURO_AMI_USERNAME"],
        secret=os.environ.get("KURO_AMI_SECRET") or os.environ.get("KURO_AMI_PASSWORD"),
    )
    await mgr.connect()
    try:
        r = await mgr.send_action({"Action": "SIPshowregistry"})
        items = r if isinstance(r, list) else [r]
        regs = []
        for it in items:
            d = dict(it.items()) if hasattr(it, "items") else it
            if d.get("Event") == "RegistryEntry" or "Username" in d:
                if d.get("State") == "Registered":
                    regs.append(d.get("Username"))
        return regs
    finally:
        mgr.close()


async def check_sip(ami_ok):
    """3. SIP-линии: снимок текущего числа зарегистрированных (без дебаунса — это heartbeat)."""
    try:
        regs = await fetch_sip_registry()
        n = len(regs)
        ami_status = "AMI на связи" if ami_ok else "AMI: статус неизвестен"
        if n >= EXPECTED_REGS:
            add_line(f"✅ Телефония: {n}/{EXPECTED_REGS} линий, {ami_status}", True)
        else:
            add_line(f"⚠️ Телефония: {n}/{EXPECTED_REGS} линий, {ami_status}", False)
    except Exception as e:
        add_line(f"⚠️ SIP: не смог проверить ({e})", False)


async def check_calls(db: AsyncSession):
    """4-5. Звонки за час/сутки + уникальные лиды за сутки."""
    try:
        q_hour_all = text("SELECT count(*) FROM calls WHERE started_at > now() - interval '1 hour'")
        q_hour_tracking = text(
            "SELECT count(*) FROM calls WHERE started_at > now() - interval '1 hour' "
            "AND tracking_did LIKE '700498%'"
        )
        q_day_tracking = text(
            "SELECT count(*) FROM calls WHERE started_at > now() - interval '24 hours' "
            "AND tracking_did LIKE '700498%'"
        )
        hour_all = (await db.execute(q_hour_all)).scalar() or 0
        hour_tracking = (await db.execute(q_hour_tracking)).scalar() or 0
        day_tracking = (await db.execute(q_day_tracking)).scalar() or 0
        add_line(
            f"📞 Звонки: {hour_all} за час ({hour_tracking} на трекинг) · "
            f"{day_tracking}/сутки трекинг",
            True,
        )
    except Exception as e:
        add_line(f"⚠️ Звонки: не смог посчитать ({e})", False)

    try:
        q_leads = text(
            "SELECT count(DISTINCT amo_lead_id) FROM calls "
            "WHERE amo_lead_id IS NOT NULL AND started_at > now() - interval '24 hours'"
        )
        leads = (await db.execute(q_leads)).scalar() or 0
        add_line(f"🎯 Лидов за сутки: {leads}", True)
    except Exception as e:
        add_line(f"⚠️ Лиды: не смог посчитать ({e})", False)


async def check_db_connections(db: AsyncSession):
    """6. Число соединений к БД kurotrack."""
    try:
        q = text("SELECT count(*) FROM pg_stat_activity WHERE datname='kurotrack'")
        n = (await db.execute(q)).scalar() or 0
        if n > DB_WARN_CONN:
            add_line(f"⚠️ БД: {n}/{DB_MAX_CONN} соединений (много)", False)
        else:
            add_line(f"✅ БД: {n}/{DB_MAX_CONN} соединений", True)
    except Exception as e:
        add_line(f"⚠️ БД-соединения: не смог посчитать ({e})", False)


def send_telegram(msg: str) -> bool:
    """Шлёт сообщение в Telegram (как в monitor.py). Возвращает True если Telegram ответил ok."""
    token = os.environ.get("KURO_TG_BOT_TOKEN")
    chat_id = os.environ.get("KURO_TG_CHAT_ID")
    if not token or not chat_id:
        print("(нет KURO_TG_BOT_TOKEN/KURO_TG_CHAT_ID — отчёт только в stdout)")
        return False
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": msg,
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read().decode("utf-8"))
            return bool(resp.get("ok"))
    except Exception as e:
        print(f"Telegram failed: {e}")
        return False


async def main():
    db_url = os.environ["KURO_DATABASE_URL"]
    engine = create_async_engine(db_url, pool_pre_ping=True)
    Sess = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # 1. Worker
    check_worker()

    # 2. Health API (db_ok/redis_ok показываем одной строкой, ami_ok передаём в SIP-чек)
    ami_ok, db_ok, redis_ok = check_health()
    if ami_ok is not None:
        if db_ok and redis_ok:
            add_line("✅ База и Redis: ок", True)
        else:
            problems = []
            if not db_ok:
                problems.append("БД")
            if not redis_ok:
                problems.append("Redis")
            add_line(f"⚠️ Проблема: {', '.join(problems)} не отвечает", False)

    # 3. SIP-линии
    await check_sip(ami_ok)

    # 4-6. Метрики из БД
    try:
        async with Sess() as db:
            await check_calls(db)
            await check_db_connections(db)
    except Exception as e:
        add_line(f"⚠️ Не смог подключиться к БД для метрик: {e}", False)

    await engine.dispose()

    now_local = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)
    header = "⚠️ KuroTrack — есть проблемы" if has_problem else "📊 KuroTrack"
    footer = "Есть проблемы, смотри выше ⚠️" if has_problem else "Всё работает ✅"

    msg = f"{header} — {now_local.strftime('%d.%m %H:%M')}\n\n" + "\n".join(lines) + f"\n\n{footer}"

    ok = send_telegram(msg)
    print(msg)
    print(f"\nTelegram sendMessage ok={ok}")


asyncio.run(main())
