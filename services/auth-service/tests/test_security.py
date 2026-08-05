"""Pure-function tests — no DB or MQTT required."""
import pytest
from app.core.security import normalize_phone, hash_password, verify_password, create_access_token, decode_token


def test_normalize_phone_local_zero():
    assert normalize_phone("0712345678") == "+255712345678"


def test_normalize_phone_country_code_no_plus():
    assert normalize_phone("255712345678") == "+255712345678"


def test_normalize_phone_already_e164():
    assert normalize_phone("+255712345678") == "+255712345678"


def test_normalize_phone_spaces_dashes():
    assert normalize_phone(" 0712-345-678 ") == "+255712345678"


def test_normalize_phone_invalid_too_short():
    with pytest.raises(ValueError):
        normalize_phone("07123")


def test_normalize_phone_invalid_no_leading_zero():
    with pytest.raises(ValueError):
        normalize_phone("712345678")


def test_password_hash_and_verify():
    hashed = hash_password("mySecret123")
    assert hashed != "mySecret123"
    assert verify_password("mySecret123", hashed) is True
    assert verify_password("wrongPassword", hashed) is False


def test_jwt_round_trip():
    token = create_access_token("user_abc", extra={"category": "health"})
    payload = decode_token(token)
    assert payload["sub"] == "user_abc"
    assert payload["category"] == "health"
