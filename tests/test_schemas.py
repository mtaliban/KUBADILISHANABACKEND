import pytest
from pydantic import ValidationError

from app.modules.auth.schemas import RegisterRequest, LoginRequest, ResetPasswordRequest
from app.modules.payments.schemas import DonateRequest


STATION = {
    "region_id": 17, "region_name": "Mwanza",
    "district_id": 1701, "district_name": "Nyamagana Dc",
}
DESTINATION = {
    "region_id": 1, "region_name": "Arusha",
    "district_id": None, "district_name": None,
    "facility_id": None, "facility_name": None, "notes": None,
}


def valid_payload(**overrides) -> dict:
    payload = {
        "full_name": "Kieffer Madyedye",
        "phone_primary": "0712345678",
        "password": "siri-kali",
        "category": "health",
        "cadre_code": "CO",
        "subjects": [],
        "current_station": STATION,
        "desired_destinations": [DESTINATION],
    }
    payload.update(overrides)
    return payload


# ─── RegisterRequest ────────────────────────────────────────────────

def test_register_request_valid():
    req = RegisterRequest.model_validate(valid_payload())
    assert req.full_name == "Kieffer Madyedye"
    assert req.category == "health"


def test_register_request_rejects_short_name():
    with pytest.raises(ValidationError):
        RegisterRequest.model_validate(valid_payload(full_name="A"))


def test_register_request_rejects_short_password():
    with pytest.raises(ValidationError):
        RegisterRequest.model_validate(valid_payload(password="123"))


def test_register_request_rejects_invalid_category():
    with pytest.raises(ValidationError):
        RegisterRequest.model_validate(valid_payload(category="engineer"))


def test_register_request_requires_at_least_one_destination():
    with pytest.raises(ValidationError):
        RegisterRequest.model_validate(valid_payload(desired_destinations=[]))


def test_register_request_deduplicates_subjects():
    req = RegisterRequest.model_validate(
        valid_payload(subjects=["MATH", "PHYS", "MATH", "BIO"])
    )
    assert req.subjects == ["MATH", "PHYS", "BIO"]


def test_register_request_rejects_more_than_15_destinations():
    many = [DESTINATION] * 16
    with pytest.raises(ValidationError):
        RegisterRequest.model_validate(valid_payload(desired_destinations=many))


def test_register_request_allows_optional_fields():
    payload = valid_payload(phone_alt="0755123456")
    payload.pop("subjects")  # omitted entirely → defaults to []
    req = RegisterRequest.model_validate(payload)
    assert req.phone_alt == "0755123456"
    assert req.subjects == []


# ─── LoginRequest / ResetPasswordRequest ───────────────────────────

def test_login_request_valid():
    assert LoginRequest.model_validate({"phone": "0712345678", "password": "x"})


def test_reset_password_code_must_be_6_digits():
    with pytest.raises(ValidationError):
        ResetPasswordRequest.model_validate(
            {"phone": "0712345678", "code": "123", "new_password": "newpass"}
        )
    ResetPasswordRequest.model_validate(
        {"phone": "0712345678", "code": "123456", "new_password": "newpass"}
    )# ─── DonateRequest ──────────────────────────────────────────────────

def test_donate_requires_sms_text():
    with pytest.raises(ValidationError):
        DonateRequest.model_validate({"amount": 5000})


def test_donate_valid_defaults():
    req = DonateRequest.model_validate({"amount": 5000, "sms_text": "Confirmed. Received TZS 5000 from 0712345678"})
    assert req.purpose == "donation"
    assert req.phone is None


def test_donate_rejects_amount_below_minimum():
    with pytest.raises(ValidationError):
        DonateRequest.model_validate({"amount": 100, "sms_text": "Confirmed SMS text here"})


def test_donate_rejects_amount_above_maximum():
    with pytest.raises(ValidationError):
        DonateRequest.model_validate({"amount": 20_000_000, "sms_text": "Confirmed SMS text here"})


def test_donate_rejects_short_sms():
    with pytest.raises(ValidationError):
        DonateRequest.model_validate({"amount": 5000, "sms_text": "too short"})

def test_donate_accepts_optional_phone_and_purpose():
    req = DonateRequest.model_validate({
        "amount": 5000, "phone": "0712345678",
        "sms_text": "Confirmed. You have received TZS 5,000 from JOHN.",
        "purpose": "support",
    })
    assert req.phone == "0712345678"
    assert req.purpose == "support"
