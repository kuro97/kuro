"""Unit-тесты для утилиты _city_from_campaign."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.services.amocrm import _city_from_campaign


class TestCityFromCampaignHappyPath:
    """Основные сценарии: стандартные токены utm_campaign маркетологов."""

    def test_alm_suffix_returns_almaty(self):
        """Суффикс _alm → Алматы."""
        assert _city_from_campaign("traffic_mektep_alm") == "Алматы"

    def test_ast_suffix_returns_astana(self):
        """Суффикс _ast → Астана."""
        assert _city_from_campaign("traffic_mektep_ast") == "Астана"

    def test_aty_suffix_returns_atyrau(self):
        """Суффикс _aty → Атырау."""
        assert _city_from_campaign("traffic_mektep_aty") == "Атырау"

    def test_akt_suffix_returns_aktobe(self):
        """Суффикс _akt → Актобе."""
        assert _city_from_campaign("traffic_mektep_akt") == "Актобе"

    def test_shy_suffix_returns_shymkent(self):
        """Суффикс _shy → Шымкент."""
        assert _city_from_campaign("traffic_mektep_shy") == "Шымкент"

    def test_full_city_name_astana_returns_astana(self):
        """Полное название города в campaign → Астана."""
        assert _city_from_campaign("Poisk_BIL_Astana") == "Астана"

    def test_full_city_name_almaty_returns_almaty(self):
        """Полное название almaty в campaign → Алматы."""
        assert _city_from_campaign("search_almaty_brand") == "Алматы"

    def test_full_city_name_shymkent_returns_shymkent(self):
        """Полное название shymkent в campaign → Шымкент."""
        assert _city_from_campaign("poisk_shymkent_2024") == "Шымкент"


class TestCityFromCampaignCaseInsensitive:
    """Регистронезависимость: маркетологи вводят по-разному."""

    def test_uppercase_returns_almaty(self):
        """Верхний регистр → функция нечувствительна к регистру."""
        assert _city_from_campaign("TRAFFIC_MEKTEP_ALM") == "Алматы"

    def test_mixed_case_astana(self):
        """Смешанный регистр 'Astana' → Астана."""
        assert _city_from_campaign("Poisk_BIL_Astana") == "Астана"

    def test_uppercase_shymkent(self):
        """SHYMKENT верхним регистром → Шымкент."""
        assert _city_from_campaign("SHYMKENT_POISK") == "Шымкент"


class TestCityFromCampaignLongTokenPriority:
    """Приоритет длинного токена: 'almaty' (6 символов) перед '_alm' (4 символа)."""

    def test_almaty_alm_returns_almaty_via_long_token(self):
        """'almaty_alm' содержит оба токена, длинный 'almaty' находится первым."""
        # Функция сортирует токены по длине убывающей → 'almaty' (6) найдётся
        # раньше '_alm' (4). Оба токена → одинаковый результат Алматы.
        assert _city_from_campaign("almaty_alm") == "Алматы"

    def test_astana_ast_returns_astana_via_long_token(self):
        """'astana_ast' содержит оба токена для Астаны."""
        assert _city_from_campaign("astana_ast_promo") == "Астана"


class TestCityFromCampaignNegativeCases:
    """Случаи когда город не определяется."""

    def test_none_returns_none(self):
        """None → None (нет кампании)."""
        assert _city_from_campaign(None) is None

    def test_empty_string_returns_none(self):
        """Пустая строка → None."""
        assert _city_from_campaign("") is None

    def test_random_campaign_returns_none(self):
        """Случайная кампания без известных токенов → None."""
        assert _city_from_campaign("random_campaign_xyz") is None

    def test_brand_campaign_returns_none(self):
        """Брендовая кампания без токена города → None."""
        assert _city_from_campaign("brand_2024_summer") is None

    def test_partial_token_not_matching_returns_none_when_no_token_present(self):
        """Частичное совпадение без полного токена → None."""
        # Функция ищет токены как подстроки без границ слова:
        # "aktobetop" содержит токен "aktobe" => вернёт Актобе.
        # Реальный случай "нет токена" — строка без известных подстрок.
        assert _city_from_campaign("aktob") is None  # "aktob" не является токеном
        assert _city_from_campaign("xxxxxshyxxx") is None  # нет токена _shy или shymkent


@pytest.mark.parametrize("campaign,expected", [
    ("traffic_mektep_alm", "Алматы"),
    ("traffic_mektep_ast", "Астана"),
    ("traffic_mektep_aty", "Атырау"),
    ("traffic_mektep_akt", "Актобе"),
    ("traffic_mektep_shy", "Шымкент"),
    ("Poisk_BIL_Astana", "Астана"),
    ("search_almaty_brand", "Алматы"),
    ("TRAFFIC_MEKTEP_ALM", "Алматы"),
    (None, None),
    ("", None),
    ("random_xyz", None),
])
def test_city_from_campaign_parametrized(campaign, expected):
    """Параметризованный прогон основных сценариев."""
    assert _city_from_campaign(campaign) == expected
