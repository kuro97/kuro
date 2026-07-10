"""Unit-тесты приоритета call_processor._resolve_did (ARCH-attribution-fix).

Порядок приоритета зафиксирован спекой и НЕ меняется этим фиксом:
inbound_did:{uniqueid} -> inbound_did:{linkedid} -> user_field -> dst.
Redis мокается — реальных соединений нет.
"""

import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from app.workers.call_processor import _resolve_did  # noqa: E402


def _fake_get(store: dict):
    """Возвращает async side_effect функцию: redis_client.get(key) -> store.get(key)."""

    async def _get(key):
        return store.get(key)

    return _get


class TestResolveByUniqueid:
    """inbound_did:{uniqueid} есть -> возвращается он, независимо от linkedid."""

    async def test_resolve_by_uniqueid(self):
        store = {"inbound_did:U": "7004982690", "inbound_did:L": None}
        with patch(
            "app.workers.call_processor.redis_client.get",
            AsyncMock(side_effect=_fake_get(store)),
        ):
            result = await _resolve_did("U", "L", None, "702")
        assert result == "7004982690"

    async def test_uniqueid_precedes_linkedid(self):
        """Оба ключа валидны -> побеждает inbound_did:{uniqueid} (короткое замыкание or)."""
        store = {"inbound_did:U": "7004982690", "inbound_did:L": "7004980117"}
        with patch(
            "app.workers.call_processor.redis_client.get",
            AsyncMock(side_effect=_fake_get(store)),
        ):
            result = await _resolve_did("U", "L", None, "702")
        assert result == "7004982690"


class TestResolveByLinkedid:
    """inbound_did:{uniqueid} пуст, inbound_did:{linkedid} есть -> fallback на linkedid."""

    async def test_resolve_by_linkedid(self):
        store = {"inbound_did:U": None, "inbound_did:L": "7004982690"}
        with patch(
            "app.workers.call_processor.redis_client.get",
            AsyncMock(side_effect=_fake_get(store)),
        ):
            result = await _resolve_did("U", "L", None, "702")
        assert result == "7004982690"


class TestFallbackToDstAndUserField:
    """Ни одного ключа в Redis -> user_field, а если и его нет -> dst."""

    async def test_fallback_to_dst(self):
        store = {"inbound_did:U": None, "inbound_did:L": None}
        with patch(
            "app.workers.call_processor.redis_client.get",
            AsyncMock(side_effect=_fake_get(store)),
        ):
            result = await _resolve_did("U", "L", None, "702")
        assert result == "702"

    async def test_fallback_to_user_field(self):
        store = {"inbound_did:U": None, "inbound_did:L": None}
        with patch(
            "app.workers.call_processor.redis_client.get",
            AsyncMock(side_effect=_fake_get(store)),
        ):
            result = await _resolve_did("U", "L", "7004982690", "702")
        assert result == "7004982690"


class TestRedisExceptionFallsToDst:
    """redis.get бросает исключение -> try/except гасит её, идём в dst."""

    async def test_redis_exception_falls_to_dst(self):
        mock_get = AsyncMock(side_effect=RuntimeError("redis down"))
        with patch("app.workers.call_processor.redis_client.get", mock_get):
            result = await _resolve_did("U", "L", None, "702")
        assert result == "702"
