"""Unit-тесты для AmoCRMClient._next_round_robin_city (П1.8)."""

import os
import sys
import zlib
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from app.services.amocrm import AmoCRMClient, _ROUND_ROBIN_CITIES  # noqa: E402


@pytest.fixture
def client():
    return AmoCRMClient()


class TestRoundRobinHappyPath:
    """Redis.incr отдаёт 1..N — города идут по кругу."""

    async def test_sequential_incr_cycles_cities(self, client):
        # incr возвращает 1,2,3,4,5,6,7 последовательно
        seq = iter([1, 2, 3, 4, 5, 6, 7])
        mock_incr = AsyncMock(side_effect=lambda *a, **k: next(seq))
        with patch("app.services.amocrm.redis_client.incr", mock_incr):
            results = [await client._next_round_robin_city("+77001234567") for _ in range(7)]
        # (n-1) % 5: 0,1,2,3,4,0,1 → города по кругу, 6-й = город[0], 7-й = город[1]
        expected = [
            _ROUND_ROBIN_CITIES[0], _ROUND_ROBIN_CITIES[1], _ROUND_ROBIN_CITIES[2],
            _ROUND_ROBIN_CITIES[3], _ROUND_ROBIN_CITIES[4],
            _ROUND_ROBIN_CITIES[0], _ROUND_ROBIN_CITIES[1],
        ]
        assert results == expected


class TestRoundRobinRedisFallback:
    """Redis.incr бросает исключение — детерминированный fallback по crc32(caller)."""

    async def test_exception_returns_crc32_deterministic_city(self, client):
        caller = "+77001234567"
        mock_incr = AsyncMock(side_effect=RuntimeError("redis down"))
        with patch("app.services.amocrm.redis_client.incr", mock_incr):
            city = await client._next_round_robin_city(caller)
        expected_idx = zlib.crc32(caller.encode("utf-8")) % len(_ROUND_ROBIN_CITIES)
        assert city == _ROUND_ROBIN_CITIES[expected_idx]

    async def test_fallback_stable_across_calls(self, client):
        """Один и тот же caller → один и тот же город при каждом сбое (стабильность)."""
        caller = "+77009998877"
        mock_incr = AsyncMock(side_effect=RuntimeError("redis down"))
        with patch("app.services.amocrm.redis_client.incr", mock_incr):
            first = await client._next_round_robin_city(caller)
            second = await client._next_round_robin_city(caller)
        assert first == second

    async def test_fallback_spreads_across_cities(self, client):
        """Разные caller при сбое Redis не валятся все в один город (не всё в Алматы)."""
        callers = [f"+7700123{i:04d}" for i in range(50)]
        mock_incr = AsyncMock(side_effect=RuntimeError("redis down"))
        with patch("app.services.amocrm.redis_client.incr", mock_incr):
            cities = set()
            for c in callers:
                cities.add(await client._next_round_robin_city(c))
        # crc32 по 50 разным номерам должен затронуть >1 города
        assert len(cities) > 1

    async def test_empty_caller_no_crash(self, client):
        mock_incr = AsyncMock(side_effect=RuntimeError("redis down"))
        with patch("app.services.amocrm.redis_client.incr", mock_incr):
            city = await client._next_round_robin_city("")
        assert city in _ROUND_ROBIN_CITIES

    async def test_none_caller_no_crash(self, client):
        """caller=None при упавшем redis — не падает, отдаёт валидный город."""
        mock_incr = AsyncMock(side_effect=RuntimeError("redis down"))
        with patch("app.services.amocrm.redis_client.incr", mock_incr):
            city = await client._next_round_robin_city(None)
        assert city in _ROUND_ROBIN_CITIES
