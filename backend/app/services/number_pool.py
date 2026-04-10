"""Менеджер пула номеров. Использует Redis для быстрого выделения/освобождения номеров."""

import json
import time

import redis.asyncio as redis

# Lua-скрипт для атомарного выделения номера из пула (LRU — наиболее давно освободившийся)
ALLOCATE_NUMBER_LUA = """
local pool_key = KEYS[1]
local mapping_key = KEYS[2]
local session_id = ARGV[1]
local session_data = ARGV[2]
local freeze_time = tonumber(ARGV[3])

-- Проверяем, есть ли у сессии уже назначенный номер
local existing = redis.call('HGET', mapping_key .. ':session', session_id)
if existing then
    -- Обновляем last_activity
    redis.call('HSET', mapping_key .. ':number', existing, session_data)
    return existing
end

-- Берём наиболее давно освободившийся номер (LRU)
local result = redis.call('ZPOPMIN', pool_key, 1)
if #result == 0 then
    return nil
end

local number = result[1]

-- Маппинг: номер → данные сессии, сессия → номер
redis.call('HSET', mapping_key .. ':number', number, session_data)
redis.call('HSET', mapping_key .. ':session', session_id, number)

return number
"""

RELEASE_NUMBER_LUA = """
local pool_key = KEYS[1]
local mapping_key = KEYS[2]
local number = ARGV[1]
local timestamp = tonumber(ARGV[2])

-- Получаем данные сессии для этого номера
local session_data = redis.call('HGET', mapping_key .. ':number', number)
if not session_data then
    return 0
end

local data = cjson.decode(session_data)
local session_id = data.session_id

-- Удаляем маппинги
redis.call('HDEL', mapping_key .. ':number', number)
if session_id then
    redis.call('HDEL', mapping_key .. ':session', session_id)
end

-- Возвращаем номер в пул с текущим timestamp (для LRU)
redis.call('ZADD', pool_key, timestamp, number)

return 1
"""


class NumberPoolManager:
    def __init__(self, redis_client: redis.Redis, project_id: str):
        self.redis = redis_client
        self.project_id = project_id
        self.pool_key = f"pool:{project_id}:free"
        self.mapping_key = f"pool:{project_id}:map"

    async def allocate_number(
        self,
        session_id: str,
        source: str | None = None,
        utm_campaign: str | None = None,
        utm_medium: str | None = None,
        utm_keyword: str | None = None,
    ) -> str | None:
        """Выделяет номер из пула для сессии. Возвращает None если пул исчерпан."""
        session_data = json.dumps({
            "session_id": session_id,
            "source": source,
            "utm_campaign": utm_campaign,
            "utm_medium": utm_medium,
            "utm_keyword": utm_keyword,
            "last_activity": time.time(),
        })

        result = await self.redis.eval(
            ALLOCATE_NUMBER_LUA,
            2,
            self.pool_key,
            self.mapping_key,
            session_id,
            session_data,
            900,  # freeze_time
        )
        return result

    async def release_number(self, number: str) -> bool:
        """Освобождает номер обратно в пул."""
        result = await self.redis.eval(
            RELEASE_NUMBER_LUA,
            2,
            self.pool_key,
            self.mapping_key,
            number,
            time.time(),
        )
        return bool(result)

    async def heartbeat(self, session_id: str) -> bool:
        """Обновляет last_activity для сессии. Возвращает False если сессия не найдена."""
        number = await self.redis.hget(f"{self.mapping_key}:session", session_id)
        if not number:
            return False

        raw = await self.redis.hget(f"{self.mapping_key}:number", number)
        if not raw:
            return False

        data = json.loads(raw)
        data["last_activity"] = time.time()
        await self.redis.hset(f"{self.mapping_key}:number", number, json.dumps(data))
        return True

    async def get_session_by_number(self, number: str) -> dict | None:
        """Получает данные сессии по подменному номеру (при входящем звонке)."""
        raw = await self.redis.hget(f"{self.mapping_key}:number", number)
        if not raw:
            return None
        return json.loads(raw)

    async def add_number_to_pool(self, number: str) -> None:
        """Добавляет номер в пул свободных."""
        await self.redis.zadd(self.pool_key, {number: 0})

    async def get_pool_stats(self) -> dict:
        """Статистика пула: свободные / занятые."""
        free_count = await self.redis.zcard(self.pool_key)
        busy_count = await self.redis.hlen(f"{self.mapping_key}:number")
        return {
            "free": free_count,
            "busy": busy_count,
            "total": free_count + busy_count,
            "utilization": busy_count / (free_count + busy_count) if (free_count + busy_count) > 0 else 0,
        }
