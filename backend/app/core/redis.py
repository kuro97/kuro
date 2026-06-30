import redis.asyncio as redis
from redis.backoff import ExponentialBackoff
from redis.retry import Retry

from app.core.config import settings

# Retry с экспоненциальным backoff на транзиентные сбои Redis.
# health_check_interval=30 позволяет обнаружить разрыв соединения без лишних запросов.
_retry = Retry(ExponentialBackoff(), retries=3)

redis_client = redis.from_url(
    settings.redis_url,
    decode_responses=True,
    retry=_retry,
    health_check_interval=30,
)


async def get_redis() -> redis.Redis:
    return redis_client
