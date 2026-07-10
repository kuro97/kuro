"""Unit-тесты guard'а _should_upgrade_foreign_row и upgrade-ветки _handle_cdr
(ARCH-attribution-fix): "добиваем" чужую неатрибуцированную строку нашей
атрибуцией вместо тихого skip, когда второй потребитель AMI (старый
docker-контейнер) создал Call первым.
"""

import os
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from app.workers import call_processor  # noqa: E402
from app.workers.call_processor import _should_upgrade_foreign_row  # noqa: E402


class TestShouldUpgradeForeignRow:
    """Чистый guard, без async/IO — четыре кейса из спеки (Test Scenarios §9)."""

    def test_upgrade_unattributed_foreign_with_our_did(self):
        """Дубль без атрибуции (project_id=None, linkedid=None) + у нас резолв -> True."""
        existing = SimpleNamespace(project_id=None, linkedid=None)
        assert _should_upgrade_foreign_row(existing, "proj-uuid") is True

    def test_skip_already_attributed(self):
        """Дубль уже атрибуцирован (project_id есть) -> False, готовую строку не портим."""
        existing = SimpleNamespace(project_id="p", linkedid=None)
        assert _should_upgrade_foreign_row(existing, "proj-uuid") is False

    def test_skip_row_with_linkedid(self):
        """Дубль — наша собственная строка (linkedid есть) -> False (идемпотентность)."""
        existing = SimpleNamespace(project_id=None, linkedid="L")
        assert _should_upgrade_foreign_row(existing, "proj-uuid") is False

    def test_skip_when_no_resolution(self):
        """У нас нет резолва (project_id=None, напр. Exten=s) -> False, skip как раньше."""
        existing = SimpleNamespace(project_id=None, linkedid=None)
        assert _should_upgrade_foreign_row(existing, None) is False


# ---------------------------------------------------------------------------
# Опциональный интеграционный тест _handle_cdr (спека §9, "если позволяет
# бюджет") — гоняет реальную функцию целиком с замоканными границами
# (redis, БД-сессия, recording_service, _push_to_amo), проверяет что
# upgrade-ветка действительно копирует поля и вызывает _push_to_amo, а
# ветка "нет резолва" — просто skip без _push_to_amo.
# ---------------------------------------------------------------------------


class _FakeResult:
    """Имитирует Result от db.execute(): только scalar_one_or_none()."""

    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """Имитирует AsyncSession с заданной очередью ответов execute() по порядку вызовов."""

    def __init__(self, execute_results: list):
        self._results = iter(execute_results)
        self.execute = AsyncMock(side_effect=lambda *a, **k: next(self._results))
        self.commit = AsyncMock()
        self.rollback = AsyncMock()


def _session_factory(*sessions: _FakeSession):
    """async_session() отдаёт сессии из sessions по очереди при каждом вызове."""
    it = iter(sessions)

    @asynccontextmanager
    async def _factory():
        yield next(it)

    return _factory


CDR_EVENT = {
    "event": "cdr",
    "uniqueid": "foreign-uid-1",
    "linkedid": "L-foreign-uid-1",
    "src": "77001234567",
    "dst": "702",
    "duration": "15",
    "billsec": "10",
    "disposition": "ANSWERED",
    "user_field": None,
}


class TestHandleCdrUpgradeBranch:
    """_handle_cdr: чужой неатрибуцированный дубль + наш резолв -> UPDATE + _push_to_amo."""

    async def test_upgrade_copies_fields_and_pushes_to_amo(self, monkeypatch):
        project_id = str(uuid.uuid4())
        existing_foreign_call = SimpleNamespace(
            project_id=None,
            linkedid=None,
            tracking_did="702",
            source=None,
            medium=None,
            campaign=None,
            keyword=None,
            is_unique=False,
            is_target=False,
            recording_url=None,
        )

        # Redis: call_lock получен сразу (single leg, без поллинга)
        redis_set = AsyncMock(return_value=True)

        session_data = {"source": "facebook", "utm_medium": "cpc", "utm_campaign": "c1", "utm_keyword": "k1"}
        find_session = AsyncMock(return_value=(session_data, project_id))
        classify = AsyncMock(return_value={"is_unique": True, "is_target": True, "is_spam": False})
        push_to_amo = AsyncMock()

        # Единственный db.execute внутри основной сессии — поиск existing_call по uniqueid
        # (session_data передан напрямую -> _apply_source_attribution БД не трогает;
        # project_id уже резолвлен -> предварительный TrackingNumber-lookup блок не выполняется).
        fake_session = _FakeSession([_FakeResult(existing_foreign_call)])

        with patch("app.workers.call_processor.redis_client.set", redis_set), \
             patch("app.workers.call_processor._resolve_did", AsyncMock(return_value="7004982690")), \
             patch("app.workers.call_processor._find_session_by_did", find_session), \
             patch("app.workers.call_processor.classify_call", classify), \
             patch("app.workers.call_processor._push_to_amo", push_to_amo), \
             patch("app.workers.call_processor.recording_service.get_local_path", MagicMock(return_value="")), \
             patch("app.workers.call_processor.recording_service.upload_recording", AsyncMock(return_value=None)), \
             patch("app.workers.call_processor.async_session", _session_factory(fake_session)):
            await call_processor._handle_cdr(CDR_EVENT)

        # Поля чужой строки добиты нашей атрибуцией
        assert existing_foreign_call.project_id == uuid.UUID(project_id)
        assert existing_foreign_call.linkedid == CDR_EVENT["linkedid"]
        assert existing_foreign_call.tracking_did == "7004982690"
        assert existing_foreign_call.source == "facebook"
        assert existing_foreign_call.campaign == "c1"
        fake_session.commit.assert_awaited()
        push_to_amo.assert_awaited_once()
        # _push_to_amo вызван именно с обновлённой (существующей) строкой
        assert push_to_amo.await_args.args[1] is existing_foreign_call


class TestHandleCdrSkipWithoutResolution:
    """_handle_cdr: дубль без нашего резолва (project_id остаётся None) -> skip, без _push_to_amo."""

    async def test_skip_when_no_resolution_no_push(self, monkeypatch):
        existing_foreign_call = SimpleNamespace(
            project_id=None,
            linkedid=None,
            tracking_did="702",
            source=None,
            medium=None,
            campaign=None,
            keyword=None,
            is_unique=False,
            is_target=False,
            recording_url=None,
        )

        redis_set = AsyncMock(return_value=True)
        find_session = AsyncMock(return_value=(None, None))
        push_to_amo = AsyncMock()

        # did_raw="s" -> did_norm="" (normalize_phone) -> _apply_source_attribution
        # фолбэк-ветку не трогает БД (did_norm пуст).
        # 1-я сессия: "project_id is None" блок -> TrackingNumber lookup -> не найден.
        # 2-я сессия: основной блок -> единственный execute -> existing_call lookup.
        tn_session = _FakeSession([_FakeResult(None)])
        main_session = _FakeSession([_FakeResult(existing_foreign_call)])

        with patch("app.workers.call_processor.redis_client.set", redis_set), \
             patch("app.workers.call_processor._resolve_did", AsyncMock(return_value="s")), \
             patch("app.workers.call_processor._find_session_by_did", find_session), \
             patch("app.workers.call_processor._push_to_amo", push_to_amo), \
             patch("app.workers.call_processor.recording_service.get_local_path", MagicMock(return_value="")), \
             patch("app.workers.call_processor.recording_service.upload_recording", AsyncMock(return_value=None)), \
             patch("app.workers.call_processor.async_session", _session_factory(tn_session, main_session)):
            await call_processor._handle_cdr(CDR_EVENT)

        # Чужая строка НЕ тронута нашей атрибуцией
        assert existing_foreign_call.project_id is None
        assert existing_foreign_call.tracking_did == "702"
        push_to_amo.assert_not_awaited()
        main_session.commit.assert_not_awaited()
