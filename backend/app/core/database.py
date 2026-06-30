from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

# pool_size=30 + max_overflow=40 → итого 70 соединений.
# Оставляет ~30 в резерве при max_connections=100 в PostgreSQL.
# При 50+50=100 под нагрузкой AMI БД упиралась в лимит → TooManyConnectionsError.
engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_size=30,
    max_overflow=40,         # итого 70 — оставляет ~30 соединений БД в резерве
    pool_timeout=30,         # было 10 — даём время дождаться свободного слота на пике
    pool_pre_ping=True,
    pool_recycle=1800,
    connect_args={
        "timeout": 15,           # таймаут на установку TCP-соединения (asyncpg)
        "command_timeout": 300,  # таймаут на выполнение SQL-команды (asyncpg)
    },
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    # rollback в finally завершает любую открытую транзакцию (idle in transaction leak).
    # Если endpoint сам сделал commit — rollback станет no-op. Для read-only это снимает
    # зависшие коннекты которые иначе забивают pool и приводят к 504.
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.rollback()
