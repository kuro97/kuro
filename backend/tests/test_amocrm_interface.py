"""Smoke-контракт: все методы amocrm_client, вызываемые из воркеров, существуют (П1.10.3).

Ловит регрессии вроде удаления add_call_note при живом вызове в call_processor.
"""

import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from app.services.amocrm import AmoCRMClient  # noqa: E402

_BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..")
_WORKER_FILES = [
    os.path.join(_BACKEND_DIR, "app", "workers", "call_processor.py"),
    os.path.join(_BACKEND_DIR, "app", "workers", "reconciliation.py"),
]


def _collect_amocrm_method_calls(source: str) -> set[str]:
    """Возвращает множество имён X из вызовов amocrm_client.X(...) в исходнике."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        # Ищем Call, у которого func = Attribute(value=Name('amocrm_client'), attr='X')
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func
            if isinstance(attr.value, ast.Name) and attr.value.id == "amocrm_client":
                names.add(attr.attr)
    return names


def _all_called_methods() -> set[str]:
    called: set[str] = set()
    for path in _WORKER_FILES:
        with open(path, "r", encoding="utf-8") as f:
            called |= _collect_amocrm_method_calls(f.read())
    return called


def test_worker_files_exist():
    for path in _WORKER_FILES:
        assert os.path.exists(path), f"нет файла воркера: {path}"


def test_at_least_expected_methods_detected():
    """Sanity: парсер реально что-то нашёл (иначе тест бессмыслен)."""
    called = _all_called_methods()
    # Эти два вызова заведомо есть в коде — если пропали, что-то сломалось в парсере/коде
    assert "create_lead_from_call" in called
    assert "add_call_note" in called


@pytest.mark.parametrize("method_name", sorted(_all_called_methods()))
def test_amocrm_client_has_method(method_name):
    """Каждый вызываемый из воркеров метод должен существовать у AmoCRMClient."""
    assert hasattr(AmoCRMClient, method_name), (
        f"AmoCRMClient не имеет метода '{method_name}', "
        f"но он вызывается на amocrm_client в воркерах — регрессия интерфейса"
    )
