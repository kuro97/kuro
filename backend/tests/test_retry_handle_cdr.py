"""Unit-тесты для _retry_handle_cdr (П1.6): какие ошибки ретраятся, какие — нет."""

import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from asyncpg.exceptions import TooManyConnectionsError, ConnectionDoesNotExistError  # noqa: E402
from sqlalchemy.exc import (  # noqa: E402
    DBAPIError,
    IntegrityError,
    OperationalError,
    TimeoutError as SATimeoutError,
)

from app.workers import call_processor  # noqa: E402
from app.workers.call_processor import _is_retriable_db_error  # noqa: E402


EVENT = {"uniqueid": "test-uid-1"}


async def _run_with_mocked_handle(side_effect):
    """Патчит _handle_cdr и asyncio.sleep, вызывает _retry_handle_cdr, возвращает mock."""
    mock_handle = AsyncMock(side_effect=side_effect)
    with patch.object(call_processor, "_handle_cdr", mock_handle), \
         patch.object(call_processor.asyncio, "sleep", AsyncMock()):
        try:
            await call_processor._retry_handle_cdr(EVENT, max_attempts=3)
        except Exception as exc:  # noqa: BLE001
            return mock_handle, exc
    return mock_handle, None


class TestIsRetriableDbError:
    """_is_retriable_db_error: классификация исключений БД."""

    def test_integrity_error_not_retriable(self):
        """IntegrityError (дубль) — НЕ ретраится, критично для отсутствия дублей."""
        exc = IntegrityError("stmt", {}, Exception("dup key"))
        assert _is_retriable_db_error(exc) is False

    def test_too_many_connections_retriable(self):
        assert _is_retriable_db_error(TooManyConnectionsError("too many")) is True

    def test_operational_error_retriable(self):
        exc = OperationalError("stmt", {}, Exception("conn lost"))
        assert _is_retriable_db_error(exc) is True

    def test_connection_does_not_exist_retriable(self):
        assert _is_retriable_db_error(ConnectionDoesNotExistError("gone")) is True

    def test_sqlalchemy_timeout_retriable(self):
        assert _is_retriable_db_error(SATimeoutError()) is True

    def test_key_error_not_retriable(self):
        """Логическая ошибка (KeyError) — не ретраится."""
        assert _is_retriable_db_error(KeyError("missing")) is False

    def test_attribute_error_not_retriable(self):
        """Логическая ошибка (AttributeError) — не ретраится."""
        assert _is_retriable_db_error(AttributeError("no attr")) is False

    def test_wrapped_too_many_connections_in_dbapi_orig_retriable(self):
        """asyncpg-исключение, обёрнутое SQLAlchemy: OperationalError.orig=TooManyConnections."""
        wrapped = OperationalError("stmt", {}, TooManyConnectionsError("too many"))
        assert _is_retriable_db_error(wrapped) is True

    def test_wrapped_dbapi_error_with_non_retriable_orig_not_retriable(self):
        """DBAPIError (не OperationalError!) с "неретраибельным" .orig — не ретраится.

        DBAPIError сам по себе НЕ входит в _RETRIABLE_DB_ERRORS (в отличие от его
        подкласса OperationalError, который ретраится безусловно) — поэтому решение
        принимается только по распакованному .orig. ValueError в .orig не входит
        в список транзиентных — итог False.
        """
        wrapped = DBAPIError("stmt", {}, ValueError("not a connection issue"))
        assert _is_retriable_db_error(wrapped) is False

    def test_dbapi_error_with_retriable_orig_retriable(self):
        """DBAPIError с .orig=TooManyConnectionsError — ретраится через распаковку .orig."""
        wrapped = DBAPIError("stmt", {}, TooManyConnectionsError("too many"))
        assert _is_retriable_db_error(wrapped) is True


class TestRetriableErrors:
    """Транзиентные сбои БД → ретраятся до max_attempts."""

    async def test_too_many_connections_retries_3_times(self):
        # Всегда падает → 3 попытки, потом raise
        mock_handle, exc = await _run_with_mocked_handle(
            TooManyConnectionsError("too many")
        )
        assert mock_handle.await_count == 3
        assert isinstance(exc, TooManyConnectionsError)

    async def test_operational_error_retries(self):
        mock_handle, exc = await _run_with_mocked_handle(
            OperationalError("stmt", {}, Exception("conn lost"))
        )
        assert mock_handle.await_count == 3
        assert isinstance(exc, OperationalError)

    async def test_sqlalchemy_timeout_retries(self):
        mock_handle, exc = await _run_with_mocked_handle(SATimeoutError())
        assert mock_handle.await_count == 3
        assert isinstance(exc, SATimeoutError)

    async def test_connection_does_not_exist_retries(self):
        mock_handle, exc = await _run_with_mocked_handle(
            ConnectionDoesNotExistError("gone")
        )
        assert mock_handle.await_count == 3

    async def test_wrapped_asyncpg_in_dbapi_orig_retries(self):
        # asyncpg-исключение, обёрнутое SQLAlchemy: OperationalError с orig=TooManyConnections
        wrapped = OperationalError("stmt", {}, TooManyConnectionsError("too many"))
        mock_handle, exc = await _run_with_mocked_handle(wrapped)
        assert mock_handle.await_count == 3


class TestNonRetriableErrors:
    """IntegrityError и прочее → НЕ ретраится (одна попытка)."""

    async def test_integrity_error_not_retried(self):
        # IntegrityError — дубль, НЕ ретраим: ровно 1 вызов, потом raise сразу
        mock_handle, exc = await _run_with_mocked_handle(
            IntegrityError("stmt", {}, Exception("dup key"))
        )
        assert mock_handle.await_count == 1
        assert isinstance(exc, IntegrityError)

    async def test_value_error_not_retried(self):
        mock_handle, exc = await _run_with_mocked_handle(ValueError("logic bug"))
        assert mock_handle.await_count == 1
        assert isinstance(exc, ValueError)

    async def test_key_error_not_retried(self):
        """Логическая ошибка (KeyError) — 1 вызов, проброшена сразу, без ретраев."""
        mock_handle, exc = await _run_with_mocked_handle(KeyError("missing_field"))
        assert mock_handle.await_count == 1
        assert isinstance(exc, KeyError)


class TestSuccessPath:
    """Успех с первой попытки → один вызов, без исключений."""

    async def test_success_first_try(self):
        mock_handle = AsyncMock(return_value=None)
        with patch.object(call_processor, "_handle_cdr", mock_handle), \
             patch.object(call_processor.asyncio, "sleep", AsyncMock()):
            await call_processor._retry_handle_cdr(EVENT, max_attempts=3)
        assert mock_handle.await_count == 1

    async def test_recovers_on_second_attempt(self):
        # Первая попытка падает транзиентно, вторая успешна → 2 вызова, без raise
        calls = {"n": 0}

        async def side(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TooManyConnectionsError("too many")
            return None

        mock_handle = AsyncMock(side_effect=side)
        with patch.object(call_processor, "_handle_cdr", mock_handle), \
             patch.object(call_processor.asyncio, "sleep", AsyncMock()):
            await call_processor._retry_handle_cdr(EVENT, max_attempts=3)
        assert mock_handle.await_count == 2

    async def test_sleep_called_with_backoff_not_real_wait(self):
        """asyncio.sleep вызывается с бэкоффом 1,2 сек (замокан, тест не ждёт реально)."""
        mock_sleep = AsyncMock()
        mock_handle = AsyncMock(side_effect=TooManyConnectionsError("too many"))
        with patch.object(call_processor, "_handle_cdr", mock_handle), \
             patch.object(call_processor.asyncio, "sleep", mock_sleep):
            with pytest.raises(TooManyConnectionsError):
                await call_processor._retry_handle_cdr(EVENT, max_attempts=3)
        # 2 паузы между 3 попытками: 1с, 2с (backoff = 2**attempt)
        sleep_args = [call.args[0] for call in mock_sleep.await_args_list]
        assert sleep_args == [1, 2]
