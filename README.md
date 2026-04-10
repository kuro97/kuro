# KuroTrack

Call tracking platform built on Asterisk/FreePBX.

## Stack

- **Backend**: Python 3.12 + FastAPI
- **DB**: PostgreSQL 16 + Redis 7
- **Telephony**: Asterisk AMI + AGI (FreePBX 16)
- **DNI**: Vanilla JS snippet (~5KB)
- **Infra**: Docker Compose

## Quick Start

```bash
# Clone and configure
cp backend/.env.example backend/.env
# Edit backend/.env with your Asterisk AMI credentials

# Run
docker compose up -d

# Run migrations
docker compose exec api alembic upgrade head

# API docs
open http://localhost:8000/docs
```

## Project Structure

```
backend/
  app/
    api/v1/          # FastAPI endpoints
    core/            # Config, DB, Redis connections
    models/          # SQLAlchemy models
    schemas/         # Pydantic schemas
    services/        # Business logic (number pool, AMI client)
    workers/         # Call event processors
  asterisk/
    agi/             # AGI scripts for Asterisk dialplan
    dialplan/        # Custom dialplan for FreePBX
  migrations/        # Alembic DB migrations
frontend/
  snippet/           # DNI JavaScript snippet
docker/              # Dockerfiles
```
