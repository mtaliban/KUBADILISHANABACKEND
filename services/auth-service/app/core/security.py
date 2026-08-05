import bcrypt
import jwt
from datetime import datetime, timedelta, timezone
from typing import Any
from .config import settings


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def create_access_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(hours=settings.jwt_expire_hours),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])


def normalize_phone(phone: str) -> str:
    """Tanzania phone normalization: 0712345678 or +255712345678 -> +255712345678"""
    p = phone.strip().replace(" ", "").replace("-", "")
    if p.startswith("+255"):
        return p
    if p.startswith("255"):
        return "+" + p
    if p.startswith("0") and len(p) == 10:
        return "+255" + p[1:]
    raise ValueError(f"Invalid Tanzanian phone: {phone}")
