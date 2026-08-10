import logging
import re

import bcrypt
import jwt
from datetime import datetime, timedelta, timezone
from typing import Any
from bson import ObjectId
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from .config import settings
from .db import get_db

logger = logging.getLogger(__name__)

# RFC 5322-ish — strict enough for a real-world admin inbox, lenient enough
# to not reject legit addresses (no exotic comment forms).
_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def normalize_email(email: str) -> str:
    """Lowercase + trim an email address, raising ValueError if malformed."""
    e = (email or "").strip().lower()
    if not e or len(e) > 254 or not _EMAIL_RE.match(e):
        raise ValueError(f"Invalid email: {email!r}")
    return e


def create_access_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": subject, "iat": now, "exp": now + timedelta(hours=settings.jwt_expire_hours)}
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])


def normalize_phone(phone: str) -> str:
    p = phone.strip().replace(" ", "").replace("-", "")
    if p.startswith("+255"): return p
    if p.startswith("255"): return "+" + p
    if p.startswith("0") and len(p) == 10: return "+255" + p[1:]
    raise ValueError(f"Invalid Tanzanian phone: {phone}")


security = HTTPBearer(auto_error=False)


def _is_valid_object_id(value: str) -> bool:
    return bool(value) and len(value) == 24 and all(c in "0123456789abcdefABCDEF" for c in value)


async def current_user(cred: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if cred is None or not cred.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        payload = decode_token(cred.credentials)
        user_id = payload.get("sub")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expired")
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    if not user_id or not _is_valid_object_id(user_id):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token subject")
    user = await get_db().users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    # Block disabled accounts immediately (not just at login)
    if user.get("status") == "disabled":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account disabled")
    return user


async def current_admin(user=Depends(current_user)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    if not user.get("email_verified"):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Thibitisha email yako kwanza — tuma code kupitia 'Thibitisha Email' kwenye login page.")
    return user


def user_id_from_token(token: str):
    try:
        sub = decode_token(token).get("sub")
        return sub if sub and _is_valid_object_id(sub) else None
    except jwt.PyJWTError:
        return None

