# KuroTrack

Call tracking platform built on Asterisk/FreePBX.

## Stack

- **Backend**: Python 3.12 + FastAPI
- **DB**: PostgreSQL 16 + Redis 7
- **Telephony**: Asterisk AMI + AGI (FreePBX 16)
- **Frontend**: React 19 + Vite
- **DNI**: Vanilla JS snippet (~5KB)
- **Infra**: Docker Compose + Nginx

## Quick Start

```bash
# 1. Configure
cp backend/.env.example backend/.env

# 2. Run
docker compose up -d

# 3. Run migrations
docker compose exec api alembic upgrade head

# 4. Seed demo data (admin user + demo project + test numbers)
docker compose exec api python -m app.scripts.seed

# 5. Generate demo calls (30 days of realistic test data)
docker compose exec api python -m app.scripts.demo_calls

# 6. Open
#    Dashboard:  http://localhost
#    Demo site:  http://localhost/demo
#    API docs:   http://localhost:8000/docs
#    Login:      admin@kurotrack.local / admin123
```

## Demo Mode

You can test the full system without real SIP numbers:

1. Run seed + demo_calls scripts (see above)
2. Open dashboard at http://localhost — you'll see stats, charts, call history
3. Open http://localhost/demo — test DNI script and callback widget
4. Add `?utm_source=google&utm_medium=cpc&utm_campaign=test` to demo URL to test attribution

## How Call Tracking Works

```
Visitor clicks ad → lands on site with UTM params
  → JS script reads UTMs, requests tracking number from API
  → Phone number on site replaced with tracking number
  → Visitor calls tracking number
  → Asterisk receives call on tracking DID
  → System matches DID → session → UTM source
  → Call saved with full attribution (source, campaign, keyword)
  → Dashboard shows which ads generate calls
```

## Project Structure

```
backend/
  app/
    api/v1/          # FastAPI endpoints (auth, tracking, calls, projects, numbers, callback)
    core/            # Config, DB, Redis, JWT auth
    models/          # SQLAlchemy models (5 tables)
    schemas/         # Pydantic schemas
    services/        # Number pool, AMI, analytics (GA4/YM), webhooks, recordings, antispam
    workers/         # Call processor, number cleanup
    scripts/         # Seed data, demo call generator
  asterisk/
    agi/             # AGI scripts for Asterisk dialplan
    dialplan/        # Custom dialplan for FreePBX
  migrations/        # Alembic DB migrations
frontend/
  dashboard/         # React SPA (login, stats, calls, numbers, projects)
  snippet/           # DNI script + callback widget
  demo/              # Demo website for testing
docker/              # Dockerfiles + nginx config
```

## Connecting Real SIP Numbers

When ready to go live:

1. Buy SIP numbers from a provider (Voip.kz, A1 Telecom, DIDWW)
2. Configure SIP trunk in FreePBX (Connectivity → Trunks)
3. Route incoming DIDs to kurotrack context (Connectivity → Inbound Routes)
4. Copy `backend/asterisk/dialplan/extensions_custom.conf` to FreePBX
5. Copy `backend/asterisk/agi/call_tracking.py` to `/var/lib/asterisk/agi-bin/`
6. Add numbers through dashboard (Numbers → Add/Bulk Add)
7. Place JS snippet on your website
