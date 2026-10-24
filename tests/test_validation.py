from __future__ import annotations

import pytest

from app.services.errors import InputError, InvalidNameError, InvalidPhoneError
from app.services.validation import (
    mask_phone,
    normalize_contact_phone,
    normalize_phone,
    validate_comment,
    validate_name,
)


@pytest.mark.parametrize(
    "raw", ["+998 90 123-45-67", "998901234567", "901234567", "(90) 123 45 67"]
)
def test_normalize_phone_ok(raw: str) -> None:
    assert normalize_phone(raw) == "+998901234567"


@pytest.mark.parametrize("raw", ["12345", "+79161234567", "abcdefghi", "", "+99890123456"])
def test_normalize_phone_rejects(raw: str) -> None:
    with pytest.raises(InvalidPhoneError):
        normalize_phone(raw)


def test_contact_phone_foreign_flag() -> None:
    assert normalize_contact_phone("998901234567") == ("+998901234567", False)
    assert normalize_contact_phone("+7 916 123 45 67") == ("+79161234567", True)
    with pytest.raises(InvalidPhoneError):
        normalize_contact_phone("123")


@pytest.mark.parametrize("name", ["O'tkir", "Gʻayrat", "Ali Valiyev", "Анна-Мария"])
def test_names_ok(name: str) -> None:
    assert validate_name(name) == name


@pytest.mark.parametrize("name", ["A", "x" * 51, "<b>x</b>", "123", "  ", "Ali1"])
def test_names_rejected(name: str) -> None:
    with pytest.raises(InvalidNameError):
        validate_name(name)


def test_name_collapses_spaces() -> None:
    assert validate_name("  Ali    Vali ") == "Ali Vali"


def test_comment() -> None:
    assert validate_comment("") is None
    assert validate_comment("   ") is None
    assert validate_comment("  <b>soqol</b> ") == "<b>soqol</b>"
    assert validate_comment("a\x00b​c") == "abc"
    assert validate_comment("x" * 300) == "x" * 300
    with pytest.raises(InputError) as e:
        validate_comment("x" * 301)
    assert e.value.ctx["field"] == "comment"


def test_mask_phone() -> None:
    assert mask_phone("+998901234567") == "+998 90 *** ** 67"
    assert mask_phone("") == ""
    assert "*" in mask_phone("+79161234567")
