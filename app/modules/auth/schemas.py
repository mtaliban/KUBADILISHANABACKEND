from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator


class StationInput(BaseModel):
    region_id: int
    region_name: str
    district_id: int
    district_name: str
    facility_id: Optional[str] = None
    facility_name: Optional[str] = None
    facility_type: Optional[str] = None


class DestinationInput(BaseModel):
    region_id: int
    region_name: str
    district_id: Optional[int] = None
    district_name: Optional[str] = None
    facility_id: Optional[str] = None
    facility_name: Optional[str] = None
    notes: Optional[str] = Field(None, max_length=200)


class RegisterRequest(BaseModel):
    full_name: str = Field(..., min_length=3, max_length=100)
    phone_primary: str
    phone_alt: Optional[str] = None
    password: str = Field(..., min_length=6, max_length=128)
    category: str  # code ya idara (health/education au nyingine)
    cadre_code: str
    subjects: list[str] = Field(default_factory=list)
    current_station: StationInput
    desired_destinations: list[DestinationInput] = Field(..., min_length=1, max_length=15)

    @field_validator("subjects")
    @classmethod
    def dedupe(cls, v): return list(dict.fromkeys(v))


class RegisterResponse(BaseModel):
    user_id: str
    full_name: str
    phone_primary: str
    category: str
    cadre_code: str
    access_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    phone: str
    password: str | None = None


class LoginResponse(BaseModel):
    user_id: str
    full_name: str
    phone_primary: str | None = None
    category: str | None = None
    cadre_code: str | None = None
    access_token: str
    token_type: str = "bearer"
    is_admin: bool = False


class ForgotPasswordRequest(BaseModel):
    phone: str
    full_name: str = Field(default="", max_length=100)


class LookupByNameRequest(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=100)


class ResetPasswordRequest(BaseModel):
    phone: str
    code: str | None = Field(None, min_length=6, max_length=6)
    new_password: str = Field(..., min_length=6, max_length=128)


class AdminEmailLoginRequest(BaseModel):
    """Admin login — email + password (admins do NOT log in with a phone)."""
    email: str
    password: str


class EmailVerifyRequest(BaseModel):
    """Attach + verify the admin's own email.

    `phone` identifies the existing account and `password` proves ownership so
    the first admin (created before emails existed) can self-enrol.
    """
    email: str
    password: str
    phone: str | None = None


class EmailConfirmRequest(BaseModel):
    """Submit the 6-digit code received for the target email."""
    email: str
    code: str = Field(..., min_length=6, max_length=6)


class TwoFactorLoginRequest(BaseModel):
    """Second step of admin login — submit the OTP emailed to the admin."""
    email: str
    code: str = Field(..., min_length=6, max_length=6)
