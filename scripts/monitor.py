#!/usr/bin/env python3
"""KuroTrack health-monitor.
Запускается из cron каждые 5 минут. Чек 5 пунктов, шлёт алёрт в Telegram
если что-то не так. Молчит когда всё хорошо.

Env (через .env.worker или .env.monitor):
  KURO_DATABASE_URL
  KURO_AMI_HOST, KURO_AMI_PORT, KURO_AMI_USERNAME, KURO_AMI_SECRET
  KURO_TG_BOT_TOKEN   — токен Telegram-бота
  KURO_TG_CHAT_ID     — куда слать (твой chat_id или ID группы)
"""
import asyncio, os, sys, datetime, urllib.request, urllib.parse, json
sys.path.insert(0, "/home/alisher/kurotrack/backend")

from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

ALERTS = []
TIMEZONE_OFFSET = 5  # Алматы = UTC+5
EXPECTED_REGS = 19   # 18 наших (12 старых + 6 новых) + 1 транзит V3bpWiYJ

# Какие DID считаем "наши"
OUR_DIDS = {
    # site/2gis/insta/fb — 12 старых
    "7004982661","7004982667","7004982670","7004982671","7004982672",
    "7004982675","7004982680","7004982682","7004982683","7004982685",
    "7004982687","7004982690",
    # Taplink (5 городов) + Instagram bio — 6 новых
    "7004980029","7004980038","7004980096","7004980109","7004980113",
    "7004980117",
}

def alert(msg: str):
    ALERTS.append(msg)
    print(f"⚠️  {msg}", flush=True)


async def check_kurotrack_js():
    """1. kurotrack.js должен отдаваться 200 (иначе DNI не работает на сайтах)."""
    import urllib.request, ssl
    ctx = ssl._create_unverified_context()
    try:
        req = urllib.request.Request("https://kt.aiplus.kz/kurotrack.js")
        with urllib.request.urlopen(req, timeout=5, context=ctx) as r:
            body = r.read(200).decode("utf-8", errors="ignore")
            if r.status != 200 or "KuroTrack" not in body:
                alert(f"kurotrack.js: HTTP {r.status}, контент не похож на JS")
    except Exception as e:
        alert(f"kurotrack.js недоступен: {e}")


async def check_api_health():
    """2. API /api/v1/health на воркере."""
    import urllib.request, ssl
    ctx = ssl._create_unverified_context()
    try:
        req = urllib.request.Request("https://kt.aiplus.kz/api/v1/health")
        with urllib.request.urlopen(req, timeout=5, context=ctx) as r:
            if r.status != 200:
                alert(f"API health: HTTP {r.status}")
    except Exception as e:
        alert(f"API недоступен: {e}")


async def check_sip_registry():
    """3. SIP registry должно быть >= EXPECTED_REGS."""
    from panoramisk import Manager
    try:
        mgr = Manager(
            host=os.environ["KURO_AMI_HOST"],
            port=int(os.environ.get("KURO_AMI_PORT","5038")),
            username=os.environ["KURO_AMI_USERNAME"],
            secret=os.environ.get("KURO_AMI_SECRET") or os.environ.get("KURO_AMI_PASSWORD"),
        )
        await mgr.connect()
        r = await mgr.send_action({"Action": "SIPshowregistry"})
        items = r if isinstance(r, list) else [r]
        regs = []
        for it in items:
            d = dict(it.items()) if hasattr(it,"items") else it
            if d.get("Event")=="RegistryEntry" or "Username" in d:
                if d.get("State") == "Registered":
                    regs.append(d.get("Username"))
        if len(regs) < EXPECTED_REGS:
            missing = set([str(d) for d in OUR_DIDS]) - set(regs)
            alert(f"SIP registry: {len(regs)}/{EXPECTED_REGS} зарегистрировано. "
                  f"Пропали: {', '.join(sorted(missing))[:150]}")
    except Exception as e:
        alert(f"AMI/SIP registry проверить не смог: {e}")


async def check_recent_inbound(db: AsyncSession):
    """4. За последний час должны быть звонки на 7004982XXX (в рабочее время Алматы 9-21)."""
    now_local = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)
    hour = now_local.hour
    if hour < 9 or hour >= 21:
        return  # ночь, не алёртим

    q = text("""
        SELECT COUNT(*) FROM calls
        WHERE tracking_did = ANY(:dids)
          AND started_at >= NOW() - INTERVAL '1 hour'
    """)
    res = await db.execute(q, {"dids": list(OUR_DIDS)})
    cnt = res.scalar() or 0
    if cnt == 0:
        alert(f"0 inbound на 7004982XXX за последний час (сейчас {hour}:00 Алматы — рабочее время)")


async def check_worker_errors():
    """5. В логе worker не должно быть IntegrityError за последние 5 минут."""
    log_path = "/tmp/kurotrack-worker.log"
    if not os.path.exists(log_path):
        return
    five_min_ago = datetime.datetime.now() - datetime.timedelta(minutes=5)
    try:
        st = os.stat(log_path)
        # tail последние 200 строк и ищем
        with open(log_path, "rb") as f:
            f.seek(max(0, st.st_size - 50000))
            data = f.read().decode("utf-8", errors="ignore")
        recent = data.split("\n")[-200:]
        errs = [l for l in recent if "IntegrityError" in l or "OSError: [Errno 24]" in l]
        if errs:
            alert(f"Worker лог: {len(errs)} критических ошибок в последних 200 строках. Пример: {errs[0][:150]}")
    except Exception as e:
        pass  # не критично


def send_telegram(text: str):
    """Шлёт сообщение в Telegram."""
    token = os.environ.get("KURO_TG_BOT_TOKEN")
    chat_id = os.environ.get("KURO_TG_CHAT_ID")
    if not token or not chat_id:
        print("(нет KURO_TG_BOT_TOKEN/KURO_TG_CHAT_ID — алёрт только в stdout)")
        return
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            pass
    except Exception as e:
        print(f"Telegram failed: {e}")


async def main():
    db_url = os.environ["KURO_DATABASE_URL"]
    engine = create_async_engine(db_url, pool_pre_ping=True)
    Sess = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    await check_kurotrack_js()
    await check_api_health()
    await check_sip_registry()
    async with Sess() as db:
        await check_recent_inbound(db)
    await check_worker_errors()

    await engine.dispose()

    if ALERTS:
        msg = "🚨 KuroTrack: " + datetime.datetime.now().strftime("%H:%M") + "\n\n" + "\n".join(f"• {a}" for a in ALERTS)
        send_telegram(msg)
    # Если ALERTS пуст — молчим (cron не шумит)

asyncio.run(main())
