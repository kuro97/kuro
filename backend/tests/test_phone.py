"""Unit-тесты для утилиты normalize_phone."""

import sys
import os

# Добавляем backend в путь, чтобы импорт работал без установки пакета
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.core.phone import normalize_phone


class TestNormalizePhoneHappyPath:
    """Основные сценарии: стандартные форматы телефонов."""

    def test_plus_code_eleven_digits(self):
        """Номер с плюсом и кодом страны 7."""
        assert normalize_phone("+77004982670") == "7004982670"

    def test_eight_prefix_eleven_digits(self):
        """Номер с восьмёркой вместо +7."""
        assert normalize_phone("87004982670") == "7004982670"

    def test_ten_digits_plain(self):
        """Ровно 10 цифр — возвращаем как есть."""
        assert normalize_phone("7004982670") == "7004982670"

    def test_formatted_with_spaces_dashes_brackets(self):
        """Форматированный номер: пробелы, скобки, тире."""
        assert normalize_phone(" +7 (700) 498-26-70 ") == "7004982670"

    def test_eight_formatted(self):
        """Ещё один форматированный вариант с восьмёркой."""
        assert normalize_phone("8 (700) 498-26-70") == "7004982670"


class TestNormalizePhoneExtensions:
    """Короткие номера (extensions) — возвращаем цифры как есть."""

    def test_three_digit_extension(self):
        """Extension из 3 цифр."""
        assert normalize_phone("281") == "281"

    def test_single_digit(self):
        """Одна цифра."""
        assert normalize_phone("1") == "1"

    def test_nine_digits(self):
        """9 цифр — ещё не 10, возвращаем как есть."""
        assert normalize_phone("123456789") == "123456789"


class TestNormalizePhoneEdgeCases:
    """Граничные случаи: пустые значения, мусор."""

    def test_empty_string(self):
        """Пустая строка -> пустая строка."""
        assert normalize_phone("") == ""

    def test_none_returns_empty(self):
        """None -> пустая строка (без исключений)."""
        assert normalize_phone(None) == ""

    def test_only_letters(self):
        """Строка без цифр -> пустая строка."""
        assert normalize_phone("abc") == ""

    def test_only_special_chars(self):
        """Только спецсимволы без цифр -> пустая строка."""
        assert normalize_phone("+-()") == ""

    def test_very_long_number(self):
        """Длинный номер -> последние 10 цифр."""
        assert normalize_phone("77004982670123") == "4982670123"

    def test_whitespace_only(self):
        """Только пробелы -> пустая строка."""
        assert normalize_phone("   ") == ""
