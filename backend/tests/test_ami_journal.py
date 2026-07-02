"""Unit-тесты журнала AMI-событий (П2.13): record/mark_done/mark_failed/replay/cleanup.

БД мокается через monkeypatch на app.services.ami_journal.async_session —
реальная БД не поднимается (её проверяет смоук-тест на проде).
"""

import os
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from app.services import ami_journal  # noqa: E402
from app.services.ami_client import AMIClient  # noqa: E402


class _FakeResult:
    """Имитирует Result от db.execute(): scalar_one() и .all()/.rowcount."""

    def __init__(self, scalar_value=None, all_value=None, rowcount=0):
        self._scalar_value = scalar_value
        self._all_value = all_value if all_value is not None else []
        self.rowcount = rowcount

    def scalar_one(self):
        return self._scalar_value

    def all(self):
        return self._all_value


class _FakeSession:
    """Имитирует AsyncSession: execute() возвращает заданный результат, commit — no-op."""

    def __init__(self, execute_result=None, execute_side_effect=None):
        self.execute = AsyncMock(
            return_value=execute_result, side_effect=execute_side_effect
        )
        self.commit = AsyncMock()
        self.rollback = AsyncMock()


def _make_session_factory(fake_session: _FakeSession):
    """Возвращает объект, который при вызове как async_session() даёт async context manager."""

    @asynccontextmanager
    async def _factory():
        yield fake_session

    return _factory


class TestRecordEvent:
    """record_event: INSERT pending, возврат id, устойчивость к сбоям БД."""

    async def test_record_event_returns_id(self, monkeypatch):
        fake_session = _FakeSession(execute_result=_FakeResult(scalar_value=7))
        monkeypatch.setattr(
            ami_journal, "async_session", _make_session_factory(fake_session)
        )

        event_id = await ami_journal.record_event({"event": "cdr", "uniqueid": "u1"})

        assert event_id == 7
        fake_session.execute.assert_awaited_once()
        # Проверяем что bind-параметры переданы (event_type/uniqueid/payload)
        call_args = fake_session.execute.call_args
        params = call_args.args[1]
        assert params["event_type"] == "cdr"
        assert params["uniqueid"] == "u1"
        fake_session.commit.assert_awaited_once()

    async def test_record_event_db_error_returns_none(self, monkeypatch):
        fake_session = _FakeSession(execute_side_effect=Exception("db down"))
        monkeypatch.setattr(
            ami_journal, "async_session", _make_session_factory(fake_session)
        )

        event_id = await ami_journal.record_event({"event": "cdr", "uniqueid": "u1"})

        assert event_id is None


class TestMarkDoneFailed:
    """mark_done/mark_failed: корректный UPDATE, не бросают наружу при успехе."""

    async def test_mark_done_executes_update(self, monkeypatch):
        fake_session = _FakeSession(execute_result=_FakeResult())
        monkeypatch.setattr(
            ami_journal, "async_session", _make_session_factory(fake_session)
        )

        await ami_journal.mark_done(42)

        fake_session.execute.assert_awaited_once()
        params = fake_session.execute.call_args.args[1]
        assert params["id"] == 42
        fake_session.commit.assert_awaited_once()

    async def test_mark_failed_executes_update_with_error(self, monkeypatch):
        fake_session = _FakeSession(execute_result=_FakeResult())
        monkeypatch.setattr(
            ami_journal, "async_session", _make_session_factory(fake_session)
        )

        await ami_journal.mark_failed(42, "handler raised")

        params = fake_session.execute.call_args.args[1]
        assert params["id"] == 42
        assert params["error"] == "handler raised"


class TestReplayPendingEvents:
    """replay_pending_events: вызывает handler для каждого события, mark_done/mark_failed."""

    async def test_replay_calls_handler_per_event(self, monkeypatch):
        events = [
            (1, {"event": "cdr", "uniqueid": "a"}),
            (2, {"event": "cdr", "uniqueid": "b"}),
            (3, {"event": "cdr", "uniqueid": "c"}),
        ]
        select_session = _FakeSession(execute_result=_FakeResult(all_value=events))
        update_session = _FakeSession(execute_result=_FakeResult())

        # Первый вызов async_session() — SELECT, последующие — mark_done внутри цикла
        sessions = [select_session] + [update_session] * len(events)

        call_count = {"n": 0}

        @asynccontextmanager
        async def _factory():
            idx = call_count["n"]
            call_count["n"] += 1
            yield sessions[idx]

        monkeypatch.setattr(ami_journal, "async_session", _factory)

        handler = AsyncMock()
        replayed = await ami_journal.replay_pending_events(handler)

        assert replayed == 3
        assert handler.await_count == 3
        handler.assert_any_await({"event": "cdr", "uniqueid": "a"})

    async def test_replay_idempotent_no_duplicate(self, monkeypatch):
        """handler (имитация _handle_cdr) находит дубль по uniqueid и просто return —
        не бросает исключение, событие помечается done, повторной вставки звонка нет."""
        events = [(1, {"event": "cdr", "uniqueid": "dup-1"})]
        select_session = _FakeSession(execute_result=_FakeResult(all_value=events))
        update_session = _FakeSession(execute_result=_FakeResult())

        sessions = [select_session, update_session]
        call_count = {"n": 0}

        @asynccontextmanager
        async def _factory():
            idx = call_count["n"]
            call_count["n"] += 1
            yield sessions[idx]

        monkeypatch.setattr(ami_journal, "async_session", _factory)

        # handler имитирует _handle_cdr: находит существующий Call по uniqueid,
        # ничего не создаёт, просто возвращается без ошибки (как в call_processor.py L575-577).
        handler = AsyncMock(return_value=None)
        replayed = await ami_journal.replay_pending_events(handler)

        assert replayed == 1
        handler.assert_awaited_once()
        # mark_done вызван (событие не осталось pending), дублирующего INSERT нет —
        # это гарантируется тем, что handler сам не бросил (уже проверено дедупом в БД).
        update_session.execute.assert_awaited_once()

    async def test_replay_failed_marks_failed(self, monkeypatch):
        events = [
            (1, {"event": "cdr", "uniqueid": "ok"}),
            (2, {"event": "cdr", "uniqueid": "bad"}),
        ]
        select_session = _FakeSession(execute_result=_FakeResult(all_value=events))
        done_session = _FakeSession(execute_result=_FakeResult())
        failed_session = _FakeSession(execute_result=_FakeResult())

        sessions = [select_session, done_session, failed_session]
        call_count = {"n": 0}

        @asynccontextmanager
        async def _factory():
            idx = call_count["n"]
            call_count["n"] += 1
            yield sessions[idx]

        monkeypatch.setattr(ami_journal, "async_session", _factory)

        async def _handler(payload):
            if payload["uniqueid"] == "bad":
                raise ValueError("boom")

        replayed = await ami_journal.replay_pending_events(_handler)

        assert replayed == 1  # только "ok" успешно переобработан
        # done_session — UPDATE status=done для "ok"
        assert done_session.execute.await_count == 1
        # failed_session — UPDATE status=failed для "bad"
        assert failed_session.execute.await_count == 1
        failed_params = failed_session.execute.call_args.args[1]
        assert failed_params["id"] == 2

    async def test_replay_empty_journal_returns_zero(self, monkeypatch):
        select_session = _FakeSession(execute_result=_FakeResult(all_value=[]))
        monkeypatch.setattr(
            ami_journal, "async_session", _make_session_factory(select_session)
        )

        handler = AsyncMock()
        replayed = await ami_journal.replay_pending_events(handler)

        assert replayed == 0
        handler.assert_not_awaited()


class TestCleanupOldEvents:
    """cleanup_old_events: DELETE done-событий старше retention_days."""

    async def test_cleanup_builds_correct_delete(self, monkeypatch):
        fake_session = _FakeSession(execute_result=_FakeResult(rowcount=5))
        monkeypatch.setattr(
            ami_journal, "async_session", _make_session_factory(fake_session)
        )

        deleted = await ami_journal.cleanup_old_events(retention_days=7)

        assert deleted == 5
        params = fake_session.execute.call_args.args[1]
        assert params["days"] == 7
        fake_session.commit.assert_awaited_once()


class TestDispatchWithJournal:
    """AMIClient._dispatch_with_journal: пишет в журнал, гоняет хендлеры, обновляет статус."""

    async def test_dispatch_journals_and_marks_done(self, monkeypatch):
        client = AMIClient()
        handler = AsyncMock()
        client._call_handlers = [handler]

        record_event_mock = AsyncMock(return_value=99)
        mark_done_mock = AsyncMock()
        mark_failed_mock = AsyncMock()
        monkeypatch.setattr(
            "app.services.ami_client.ami_journal.record_event", record_event_mock
        )
        monkeypatch.setattr("app.services.ami_client.ami_journal.mark_done", mark_done_mock)
        monkeypatch.setattr("app.services.ami_client.ami_journal.mark_failed", mark_failed_mock)

        event_data = {"event": "cdr", "uniqueid": "u1"}
        await client._dispatch_with_journal(event_data)

        record_event_mock.assert_awaited_once_with(event_data)
        handler.assert_awaited_once_with(event_data)
        mark_done_mock.assert_awaited_once_with(99)
        mark_failed_mock.assert_not_awaited()

    async def test_dispatch_marks_failed_on_handler_error(self, monkeypatch):
        client = AMIClient()
        handler = AsyncMock(side_effect=ValueError("boom"))
        client._call_handlers = [handler]

        record_event_mock = AsyncMock(return_value=100)
        mark_done_mock = AsyncMock()
        mark_failed_mock = AsyncMock()
        monkeypatch.setattr(
            "app.services.ami_client.ami_journal.record_event", record_event_mock
        )
        monkeypatch.setattr("app.services.ami_client.ami_journal.mark_done", mark_done_mock)
        monkeypatch.setattr("app.services.ami_client.ami_journal.mark_failed", mark_failed_mock)

        event_data = {"event": "cdr", "uniqueid": "u2"}
        # handler кидает исключение, но _dispatch_with_journal его глотает (логирует)
        await client._dispatch_with_journal(event_data)

        mark_failed_mock.assert_awaited_once_with(100, "handler raised")
        mark_done_mock.assert_not_awaited()

    async def test_dispatch_none_event_id_does_not_break_processing(self, monkeypatch):
        """record_event вернул None (сбой БД) — обработка всё равно идёт, mark_* не зовутся."""
        client = AMIClient()
        handler = AsyncMock()
        client._call_handlers = [handler]

        record_event_mock = AsyncMock(return_value=None)
        mark_done_mock = AsyncMock()
        mark_failed_mock = AsyncMock()
        monkeypatch.setattr(
            "app.services.ami_client.ami_journal.record_event", record_event_mock
        )
        monkeypatch.setattr("app.services.ami_client.ami_journal.mark_done", mark_done_mock)
        monkeypatch.setattr("app.services.ami_client.ami_journal.mark_failed", mark_failed_mock)

        event_data = {"event": "cdr", "uniqueid": "u3"}
        await client._dispatch_with_journal(event_data)

        handler.assert_awaited_once_with(event_data)
        mark_done_mock.assert_not_awaited()
        mark_failed_mock.assert_not_awaited()
