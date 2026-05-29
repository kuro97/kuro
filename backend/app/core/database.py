from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

# pool_size 50 + overflow 50 = 100 коннектов
# под нагрузкой AMI (multi-leg звонки) дефолт 5+10 быстро переполняется
engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_size=50,
    max_overflow=50,
    pool_timeout=10,
    pool_pre_ping=True,
    pool_recycle=1800,
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
