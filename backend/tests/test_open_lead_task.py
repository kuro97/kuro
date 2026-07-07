"""Unit-тесты для дедупа лида по открытому лиду клиента (ARCH-lead-dedup).

Покрывает: _find_open_lead_by_caller, _create_followup_task,
ветвление create_lead_from_call (подавление дубля / создание нового / мультилег).

Паттерн — как test_round_robin_city.py: инстанс AmoCRMClient(), unittest.mock
(AsyncMock/MagicMock/patch), asyncio_mode=auto (плейн async def, без декораторов).
Моки только на границе: переданный client.get/.post, settings, httpx.AsyncClient
(для тестов create_lead_from_call, где клиент создаётся внутри метода).
"""

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402
import pytest  # noqa: E402
from app.models.call import Call  # noqa: E402
from app.services.amocrm import AmoCRMClient  # noqa: E402


@pytest.fixture
def client():
    return AmoCRMClient()


def _make_call(
    source: str | None = "2gis_astana",
    campaign: str | None = None,
    medium: str | None = None,
    keyword: str | None = None,
    tracking_did: str = "7052699005",
    caller_number: str = "+77052699005",
    uniqueid: str = "call-uid-1",
    disposition: str = "ANSWERED",
    billsec: int = 30,
) -> Call:
    """Собирает Call без обращения к БД (только атрибуты, используемые amocrm.py)."""
    return Call(
        uniqueid=uniqueid,
        caller_number=caller_number,
        tracking_did=tracking_did,
        source=source,
        campaign=campaign,
        medium=medium,
        keyword=keyword,
        disposition=disposition,
        billsec=billsec,
    )


def _resp(status_code: int = 200, json_data=None, content: bytes = b"{}"):
    """Мок HTTP-ответа httpx: атрибуты status_code/content + методы json()/raise_for_status()."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = content
    resp.json = MagicMock(return_value=json_data)
    resp.raise_for_status = MagicMock()
    return resp


def _async_client_cm(client_mock):
    """Оборачивает mock-клиент в async context manager — замена httpx.AsyncClient(...)."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client_mock)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


_OPEN_LEAD_SAMPLE = {
    "id": 31819407,
    "status_id": 47837654,
    "pipeline_id": 3321094,
    "responsible_user_id": 11220133,
    "created_at": 1720000000,
    "updated_at": 1720003600,
}


class TestFindOpenLeadByCaller:
    """_find_open_lead_by_caller: детекция открыт/закрыт, fail-open на ошибках."""

    async def test_find_open_lead_returns_open(self, client):
        fake_client = AsyncMock()
        fake_client.get = AsyncMock(
            return_value=_resp(json_data={"_embedded": {"leads": [_OPEN_LEAD_SAMPLE]}})
        )
        result = await client._find_open_lead_by_caller(fake_client, "+77052699005")
        assert result is not None
        assert result["id"] == 31819407

    async def test_find_open_lead_skips_closed_142(self, client):
        lead = {**_OPEN_LEAD_SAMPLE, "status_id": 142}
        fake_client = AsyncMock()
        fake_client.get = AsyncMock(return_value=_resp(json_data={"_embedded": {"leads": [lead]}}))
        result = await client._find_open_lead_by_caller(fake_client, "+77052699005")
        assert result is None

    async def test_find_open_lead_skips_closed_143(self, client):
        lead = {**_OPEN_LEAD_SAMPLE, "status_id": 143}
        fake_client = AsyncMock()
        fake_client.get = AsyncMock(return_value=_resp(json_data={"_embedded": {"leads": [lead]}}))
        result = await client._find_open_lead_by_caller(fake_client, "+77052699005")
        assert result is None

    async def test_find_open_lead_picks_newest_open(self, client):
        older = {**_OPEN_LEAD_SAMPLE, "id": 1, "updated_at": 1000}
        newer = {**_OPEN_LEAD_SAMPLE, "id": 2, "updated_at": 5000}
        fake_client = AsyncMock()
        fake_client.get = AsyncMock(
            return_value=_resp(json_data={"_embedded": {"leads": [older, newer]}})
        )
        result = await client._find_open_lead_by_caller(fake_client, "+77052699005")
        assert result["id"] == 2

    async def test_find_open_lead_204_returns_none(self, client):
        fake_client = AsyncMock()
        fake_client.get = AsyncMock(return_value=_resp(status_code=204, content=b""))
        result = await client._find_open_lead_by_caller(fake_client, "+77052699005")
        assert result is None

    async def test_find_open_lead_non200_returns_none(self, client):
        fake_client = AsyncMock()
        fake_client.get = AsyncMock(return_value=_resp(status_code=500))
        result = await client._find_open_lead_by_caller(fake_client, "+77052699005")
        assert result is None

    async def test_find_open_lead_401_returns_none(self, client):
        """401/403 — fail-open, метод НЕ бросает (в отличие от _find_recent_lead_by_caller)."""
        fake_client = AsyncMock()
        fake_client.get = AsyncMock(return_value=_resp(status_code=401))
        result = await client._find_open_lead_by_caller(fake_client, "+77052699005")
        assert result is None

    async def test_find_open_lead_timeout_returns_none(self, client):
        fake_client = AsyncMock()
        fake_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        result = await client._find_open_lead_by_caller(fake_client, "+77052699005")
        assert result is None

    async def test_find_open_lead_bad_json_returns_none(self, client):
        resp = _resp(status_code=200)
        resp.json = MagicMock(side_effect=ValueError("bad json"))
        fake_client = AsyncMock()
        fake_client.get = AsyncMock(return_value=resp)
        result = await client._find_open_lead_by_caller(fake_client, "+77052699005")
        assert result is None

    async def test_find_open_lead_empty_number_returns_none(self, client):
        """caller='' -> normalize_phone даёт '' -> None, client.get вообще не вызывается."""
        fake_client = AsyncMock()
        fake_client.get = AsyncMock()
        result = await client._find_open_lead_by_caller(fake_client, "")
        assert result is None
        fake_client.get.assert_not_called()

    async def test_find_open_lead_normalizes_number(self, client):
        fake_client = AsyncMock()
        fake_client.get = AsyncMock(
            return_value=_resp(json_data={"_embedded": {"leads": [_OPEN_LEAD_SAMPLE]}})
        )
        await client._find_open_lead_by_caller(fake_client, "+77052699005")
        _, kwargs = fake_client.get.call_args
        assert kwargs["params"]["query"] == "7052699005"

    async def test_find_open_lead_no_timestamps_uses_zero_fallback(self, client):
        """Лид без updated_at/created_at — max() с fallback 0 не должен падать."""
        lead = {"id": 999, "status_id": 47837654, "responsible_user_id": 5}
        fake_client = AsyncMock()
        fake_client.get = AsyncMock(return_value=_resp(json_data={"_embedded": {"leads": [lead]}}))
        result = await client._find_open_lead_by_caller(fake_client, "+77052699005")
        assert result is not None
        assert result["id"] == 999

    async def test_find_open_lead_null_embedded_returns_none(self, client):
        """{"_embedded": null} на HTTP 200 — аномалия формата, fail-open, без исключения."""
        fake_client = AsyncMock()
        fake_client.get = AsyncMock(return_value=_resp(json_data={"_embedded": None}))
        result = await client._find_open_lead_by_caller(fake_client, "+77052699005")
        assert result is None

    async def test_find_open_lead_json_not_dict_returns_none(self, client):
        """json() вернул список вместо объекта — аномалия формата, fail-open, без исключения."""
        fake_client = AsyncMock()
        fake_client.get = AsyncMock(return_value=_resp(json_data=[]))
        result = await client._find_open_lead_by_caller(fake_client, "+77052699005")
        assert result is None


class TestCreateFollowupTask:
    """_create_followup_task: форма payload, fallback ответственного, fail-safe."""

    async def test_create_task_payload_shape(self, client):
        call = _make_call(source="2gis_astana")
        fake_client = AsyncMock()
        fake_client.post = AsyncMock(return_value=_resp(status_code=200))
        before = int(time.time())

        ok = await client._create_followup_task(fake_client, 1, 99, call, "7052699005")

        assert ok is True
        _, kwargs = fake_client.post.call_args
        task = kwargs["json"][0]
        assert task["entity_type"] == "leads"
        assert task["entity_id"] == 1
        assert task["task_type_id"] == 1
        assert task["responsible_user_id"] == 99
        assert "Перезвоните" in task["text"]
        assert "Астана" in task["text"]
        assert task["complete_till"] > before

    async def test_create_task_fallback_responsible(self, client):
        call = _make_call()
        fake_client = AsyncMock()
        fake_client.post = AsyncMock(return_value=_resp(status_code=200))
        with patch("app.services.amocrm.settings.amo_responsible_user_id", 7):
            await client._create_followup_task(fake_client, 1, None, call, "7052699005")
        _, kwargs = fake_client.post.call_args
        assert kwargs["json"][0]["responsible_user_id"] == 7

    async def test_create_task_omits_responsible_when_all_none(self, client):
        call = _make_call()
        fake_client = AsyncMock()
        fake_client.post = AsyncMock(return_value=_resp(status_code=200))
        with patch("app.services.amocrm.settings.amo_responsible_user_id", None):
            await client._create_followup_task(fake_client, 1, None, call, "7052699005")
        _, kwargs = fake_client.post.call_args
        assert "responsible_user_id" not in kwargs["json"][0]

    async def test_create_task_post_fails_returns_false(self, client):
        call = _make_call()
        fake_client = AsyncMock()
        fake_client.post = AsyncMock(side_effect=RuntimeError("boom"))
        ok = await client._create_followup_task(fake_client, 1, 99, call, "7052699005")
        assert ok is False


class TestCreateLeadFromCallOpenBranch:
    """Ветвление create_lead_from_call: подавление дубля / новый лид / мультилег."""

    async def test_create_lead_open_branch_suppresses_new(self, client):
        call = _make_call()
        fake_client = AsyncMock()
        fake_client.post = AsyncMock()  # не должен вызваться для /leads/complex

        with patch("app.services.amocrm.settings.amo_subdomain", "qadam"), \
             patch("app.services.amocrm.settings.amo_token", "tok"), \
             patch(
                 "app.services.amocrm.httpx.AsyncClient",
                 return_value=_async_client_cm(fake_client),
             ), \
             patch.object(
                 AmoCRMClient, "_find_recent_lead_by_caller", AsyncMock(return_value=(None, False))
             ), \
             patch.object(
                 AmoCRMClient,
                 "_find_open_lead_by_caller",
                 AsyncMock(return_value={"id": 31819407, "responsible_user_id": 11220133}),
             ), \
             patch.object(AmoCRMClient, "_create_followup_task", AsyncMock(return_value=True)) as mock_task:
            result = await client.create_lead_from_call(call, "+77052699005")

        assert result == 31819407
        mock_task.assert_called_once()
        fake_client.post.assert_not_called()

    async def test_create_lead_no_open_creates_new(self, client):
        call = _make_call()
        fake_client = AsyncMock()
        fake_client.post = AsyncMock(
            return_value=_resp(status_code=200, json_data=[{"id": 555, "contact_id": 1}])
        )

        with patch("app.services.amocrm.settings.amo_subdomain", "qadam"), \
             patch("app.services.amocrm.settings.amo_token", "tok"), \
             patch(
                 "app.services.amocrm.httpx.AsyncClient",
                 return_value=_async_client_cm(fake_client),
             ), \
             patch.object(
                 AmoCRMClient, "_find_recent_lead_by_caller", AsyncMock(return_value=(None, False))
             ), \
             patch.object(AmoCRMClient, "_find_open_lead_by_caller", AsyncMock(return_value=None)), \
             patch.object(AmoCRMClient, "_create_followup_task", AsyncMock()) as mock_task:
            result = await client.create_lead_from_call(call, "+77052699005")

        assert result == 555
        fake_client.post.assert_called_once()
        called_url = fake_client.post.call_args.args[0]
        assert "/leads/complex" in called_url
        mock_task.assert_not_called()

    async def test_create_lead_multileg_ours_no_task(self, client):
        call = _make_call()
        fake_client = AsyncMock()

        with patch("app.services.amocrm.settings.amo_subdomain", "qadam"), \
             patch("app.services.amocrm.settings.amo_token", "tok"), \
             patch(
                 "app.services.amocrm.httpx.AsyncClient",
                 return_value=_async_client_cm(fake_client),
             ), \
             patch.object(
                 AmoCRMClient,
                 "_find_recent_lead_by_caller",
                 AsyncMock(return_value=(31819407, True)),
             ), \
             patch.object(AmoCRMClient, "_find_open_lead_by_caller", AsyncMock()) as mock_open, \
             patch.object(AmoCRMClient, "_create_followup_task", AsyncMock()) as mock_task:
            result = await client.create_lead_from_call(call, "+77052699005")

        assert result == 31819407
        mock_open.assert_not_called()
        mock_task.assert_not_called()
