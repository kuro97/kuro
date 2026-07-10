"""Unit-тесты гейта захвата inbound_did в AMIClient._handle_newchannel (ARCH-attribution-fix).

Проверяем: DID захватывается ТОЛЬКО когда Exten — реально наш активный DID
(is_our_did), жадный is_inbound_trunk-триггер удалён и больше не отравляет
ключ мусором оператора (77072374305 / 77007544476). Redis мокается,
_dispatch_with_journal мокается — реальных соединений нет.
"""

import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from app.services.ami_client import AMIClient  # noqa: E402


def _client(monkeypatch, our_dids):
    """Создаёт AMIClient с замоканным _dispatch_with_journal и заданным кешем _our_dids."""
    client = AMIClient()
    client._dispatch_with_journal = AsyncMock()
    monkeypatch.setattr("app.services.ami_client._our_dids", our_dids)
    return client


def _inbound_did_calls(mock_set):
    """Фильтрует вызовы redis_client.set только по ключам inbound_did:*."""
    return [c for c in mock_set.await_args_list if c.args[0].startswith("inbound_did:")]


class TestCapturesOurDid:
    """Happy path: Exten — наш активный DID → запись inbound_did:{uniqueid}."""

    async def test_captures_our_did(self, monkeypatch):
        client = _client(monkeypatch, {"7004982690"})
        mock_set = AsyncMock(return_value=True)
        message = {
            "Context": "from-trunk",
            "Channel": "SIP/altel_2gis_aktobe_7004982671-0000001a",
            "Uniqueid": "U1",
            "Linkedid": "U1",
            "Exten": "7004982690",
        }
        with patch("app.services.ami_client.redis_client.set", mock_set):
            await client._handle_newchannel(None, message)

        did_calls = _inbound_did_calls(mock_set)
        assert len(did_calls) == 1
        call = did_calls[0]
        assert call.args == ("inbound_did:U1", "7004982690")
        assert call.kwargs.get("ex") == 7200
        # linkedid == uniqueid (первая нога) — ветка linkedid не вызывается
        assert not call.kwargs.get("nx")
        client._dispatch_with_journal.assert_awaited_once()


class TestIgnoresOperatorTransitNumber:
    """Ключевой сценарий фикса: номер оператора-транзита 77072374305 не пишется."""

    async def test_ignores_operator_transit_number(self, monkeypatch):
        client = _client(monkeypatch, {"7004982690"})
        mock_set = AsyncMock(return_value=True)
        message = {
            "Context": "from-trunk",
            "Channel": "SIP/altel_2gis_aktobe_7004982671-0000001b",
            "Uniqueid": "U2",
            "Linkedid": "U2",
            "Exten": "77072374305",
        }
        with patch("app.services.ami_client.redis_client.set", mock_set):
            await client._handle_newchannel(None, message)

        assert _inbound_did_calls(mock_set) == []


class TestIgnoresForeignNumber:
    """Чужой номер 77007544476 (не наш DID) — тоже не пишется."""

    async def test_ignores_foreign_number(self, monkeypatch):
        client = _client(monkeypatch, {"7004982690"})
        mock_set = AsyncMock(return_value=True)
        message = {
            "Context": "from-trunk",
            "Channel": "SIP/altel_2gis_aktobe_7004982671-0000001c",
            "Uniqueid": "U3",
            "Linkedid": "U3",
            "Exten": "77007544476",
        }
        with patch("app.services.ami_client.redis_client.set", mock_set):
            await client._handle_newchannel(None, message)

        assert _inbound_did_calls(mock_set) == []


class TestIgnoresExtenS:
    """Exten=s (DID физически отсутствует, транзит SIP/trunk_) — не пишется."""

    async def test_ignores_exten_s(self, monkeypatch):
        client = _client(monkeypatch, {"7004982690"})
        mock_set = AsyncMock(return_value=True)
        message = {
            "Context": "from-trunk",
            "Channel": "SIP/trunk_77072374305-0000001d",
            "Uniqueid": "U4",
            "Linkedid": "U4",
            "Exten": "s",
        }
        with patch("app.services.ami_client.redis_client.set", mock_set):
            await client._handle_newchannel(None, message)

        assert _inbound_did_calls(mock_set) == []


class TestLinkedidWriteUsesNx:
    """Вторая нога (linkedid != uniqueid) — inbound_did:{linkedid} пишется с nx=True."""

    async def test_linkedid_write_uses_nx(self, monkeypatch):
        client = _client(monkeypatch, {"7004982690"})
        mock_set = AsyncMock(return_value=True)
        message = {
            "Context": "from-trunk",
            "Channel": "SIP/altel_2gis_aktobe_7004982671-0000001e",
            "Uniqueid": "U2",
            "Linkedid": "L2",
            "Exten": "7004982690",
        }
        with patch("app.services.ami_client.redis_client.set", mock_set):
            await client._handle_newchannel(None, message)

        did_calls = _inbound_did_calls(mock_set)
        assert len(did_calls) == 2

        by_key = {c.args[0]: c for c in did_calls}
        uniqueid_call = by_key["inbound_did:U2"]
        linkedid_call = by_key["inbound_did:L2"]

        assert uniqueid_call.args[1] == "7004982690"
        assert uniqueid_call.kwargs.get("ex") == 7200
        assert not uniqueid_call.kwargs.get("nx")

        assert linkedid_call.args[1] == "7004982690"
        assert linkedid_call.kwargs.get("ex") == 7200
        assert linkedid_call.kwargs.get("nx") is True


class TestLinkedidForStillWritten:
    """Регресс-гард: linkedid_for:{uniqueid} пишется для ЛЮБОГО Newchannel,
    даже если Exten — не наш DID (эта запись не относится к inbound_did-гейту)."""

    async def test_linkedid_for_still_written(self, monkeypatch):
        client = _client(monkeypatch, {"7004982690"})
        mock_set = AsyncMock(return_value=True)
        message = {
            "Context": "from-internal",
            "Channel": "SIP/268-0000001f",
            "Uniqueid": "U3",
            "Linkedid": "L3",
            "Exten": "268",
        }
        with patch("app.services.ami_client.redis_client.set", mock_set):
            await client._handle_newchannel(None, message)

        # inbound_did:* не пишется (268 — не наш DID)
        assert _inbound_did_calls(mock_set) == []

        # linkedid_for:U3 -> L3 пишется независимо от гейта захвата DID
        linkedid_for_calls = [
            c for c in mock_set.await_args_list if c.args[0] == "linkedid_for:U3"
        ]
        assert len(linkedid_for_calls) == 1
        assert linkedid_for_calls[0].args == ("linkedid_for:U3", "L3")
        assert linkedid_for_calls[0].kwargs.get("ex") == 3600
