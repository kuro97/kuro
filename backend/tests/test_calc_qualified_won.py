"""Unit-тесты для метода _calc_qualified_won из AmoSyncService."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.services.amo_sync import AmoSyncService

# Константы из amo_sync.py (проверены при чтении кода)
_AMO_STATUS_WON = 142
_AMO_STATUS_LOST = 143
_SORT_PAID = 150


@pytest.fixture
def svc():
    """Инстанс AmoSyncService без зависимостей (метод чистый)."""
    return AmoSyncService()


class TestCalcQualifiedWonQualified:
    """amo_qualified: зависит только от кастомного поля 'Квалификация пройдена'."""

    def test_da_returns_qualified_true(self, svc):
        """'Да' → amo_qualified=True."""
        qualified, _ = svc._calc_qualified_won(None, None, "Да")
        assert qualified is True

    def test_da_lowercase_returns_qualified_true(self, svc):
        """'да' (строчные) → amo_qualified=True (проверка strip().lower())."""
        qualified, _ = svc._calc_qualified_won(None, None, "да")
        assert qualified is True

    def test_yes_returns_qualified_true(self, svc):
        """'yes' → amo_qualified=True."""
        qualified, _ = svc._calc_qualified_won(None, None, "yes")
        assert qualified is True

    def test_true_string_returns_qualified_true(self, svc):
        """'true' → amo_qualified=True."""
        qualified, _ = svc._calc_qualified_won(None, None, "true")
        assert qualified is True

    def test_one_string_returns_qualified_true(self, svc):
        """'1' → amo_qualified=True."""
        qualified, _ = svc._calc_qualified_won(None, None, "1")
        assert qualified is True

    def test_net_returns_qualified_false(self, svc):
        """'Нет' → amo_qualified=False."""
        qualified, _ = svc._calc_qualified_won(None, None, "Нет")
        assert qualified is False

    def test_none_returns_qualified_false(self, svc):
        """None → amo_qualified=False."""
        qualified, _ = svc._calc_qualified_won(None, None, None)
        assert qualified is False

    def test_empty_string_returns_qualified_false(self, svc):
        """'' → amo_qualified=False."""
        qualified, _ = svc._calc_qualified_won(None, None, "")
        assert qualified is False

    def test_random_string_returns_qualified_false(self, svc):
        """Произвольная строка не из списка → amo_qualified=False."""
        qualified, _ = svc._calc_qualified_won(None, None, "Возможно")
        assert qualified is False


class TestCalcQualifiedWonWon:
    """amo_won: зависит от status_id (142=won, 143=lost) и sort-порога 150."""

    def test_status_won_142_returns_won_true(self, svc):
        """status_id=142 (системный Won) → amo_won=True независимо от sort."""
        _, won = svc._calc_qualified_won(142, None, None)
        assert won is True

    def test_status_won_142_with_low_sort_returns_won_true(self, svc):
        """status_id=142 даже при sort=50 → amo_won=True (status перекрывает)."""
        _, won = svc._calc_qualified_won(142, 50, None)
        assert won is True

    def test_sort_at_threshold_returns_won_true(self, svc):
        """sort=150 (ровно порог _SORT_PAID) → amo_won=True."""
        _, won = svc._calc_qualified_won(None, 150, None)
        assert won is True

    def test_sort_above_threshold_returns_won_true(self, svc):
        """sort=200 (выше порога) → amo_won=True."""
        _, won = svc._calc_qualified_won(None, 200, None)
        assert won is True

    def test_sort_below_threshold_returns_won_false(self, svc):
        """sort=100 (ниже порога) → amo_won=False."""
        _, won = svc._calc_qualified_won(None, 100, None)
        assert won is False

    def test_sort_just_below_threshold_returns_won_false(self, svc):
        """sort=149 (на 1 ниже порога) → amo_won=False (граничное значение)."""
        _, won = svc._calc_qualified_won(None, 149, None)
        assert won is False

    def test_sort_at_threshold_but_lost_returns_won_false(self, svc):
        """sort=150 но status_id=143 (Lost) → amo_won=False: lost перебивает sort."""
        _, won = svc._calc_qualified_won(143, 150, None)
        assert won is False

    def test_sort_above_threshold_but_lost_returns_won_false(self, svc):
        """sort=200 но status_id=143 → amo_won=False."""
        _, won = svc._calc_qualified_won(143, 200, None)
        assert won is False

    def test_sort_none_returns_won_false(self, svc):
        """sort=None → amo_won=False (нет данных о сортировке)."""
        _, won = svc._calc_qualified_won(None, None, None)
        assert won is False

    def test_status_lost_143_without_sort_returns_won_false(self, svc):
        """status_id=143 без sort → amo_won=False."""
        _, won = svc._calc_qualified_won(143, None, None)
        assert won is False


class TestCalcQualifiedWonCombined:
    """Комбинированные проверки кортежа (amo_qualified, amo_won)."""

    def test_all_none_returns_false_false(self, svc):
        """Все параметры None → (False, False)."""
        assert svc._calc_qualified_won(None, None, None) == (False, False)

    def test_qualified_and_won_both_true(self, svc):
        """Квалифицирован + оплата → (True, True)."""
        assert svc._calc_qualified_won(None, 150, "Да") == (True, True)

    def test_qualified_but_not_won(self, svc):
        """Квалифицирован без оплаты → (True, False)."""
        assert svc._calc_qualified_won(None, 80, "Да") == (True, False)

    def test_won_but_not_qualified(self, svc):
        """Оплата без квалификации → (False, True)."""
        assert svc._calc_qualified_won(142, None, None) == (False, True)

    def test_lost_with_qualified_field(self, svc):
        """Lost + квал поле заполнено → (True, False): квалифицирован, но не won."""
        assert svc._calc_qualified_won(143, 200, "Да") == (True, False)


@pytest.mark.parametrize("status_id,sort,field,expected", [
    # (amo_qualified, amo_won)
    (None, None, "Да",    (True, False)),
    (None, None, "да",    (True, False)),
    (None, None, "yes",   (True, False)),
    (None, None, "true",  (True, False)),
    (None, None, "1",     (True, False)),
    (None, None, "Нет",   (False, False)),
    (None, None, None,    (False, False)),
    (None, None, "",      (False, False)),
    (142,  None, None,    (False, True)),
    (142,  50,   None,    (False, True)),
    (None, 150,  None,    (False, True)),
    (None, 200,  None,    (False, True)),
    (143,  150,  None,    (False, False)),
    (143,  200,  None,    (False, False)),
    (None, 100,  None,    (False, False)),
    (None, 149,  None,    (False, False)),
    (None, 150,  "Да",    (True, True)),
    (None, None, None,    (False, False)),
])
def test_calc_qualified_won_parametrized(status_id, sort, field, expected):
    """Параметризованный прогон всех комбинаций."""
    svc = AmoSyncService()
    assert svc._calc_qualified_won(status_id, sort, field) == expected
