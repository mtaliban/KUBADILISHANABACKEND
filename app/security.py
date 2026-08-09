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


# ─── Simple in-memory rate limiter (brute-force protection) ──────────────
# Tracks attempts per key (e.g. phone number or client IP) in a fixed window.
_attempts: dict[str, list[float]] = {}


def rate_limited(key: str, max_attempts: int | None = None, window: int | None = None) -> None:
    """Raise 429 if the key has exceeded allowed attempts in the window.

    Single-process in-memory design is appropriate for this monolith; for a
    multi-worker deployment swap this for Redis (already available in compose).
    """
    import time

    max_a = max_attempts if max_attempts is not None else settings.rate_limit_max
    win = window if window is not None else settings.rate_limit_window
    now = time.time()
    # Opportunistically drop stale keys so the map does not grow forever.
    stale = [k for k, v in _attempts.items() if not v or now - max(v) > win]
    for k in stale:
        _attempts.pop(k, None)
    recent = [t for t in _attempts.get(key, []) if now - t < win]
    if len(recent) >= max_a:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Too many attempts — subiri dakika chache kisha ujaribu tena")
    recent.append(now)
    _attempts[key] = recent


def clear_attempts(key: str) -> None:
    """Clear failed-attempt tracking after a successful action for that key."""
    _attempts.pop(key, None)


def client_ip(request) -> str:
    """Client IP used for rate limiting.

    We only trust `X-Forwarded-For` when the deployment sits behind a proxy
    that sets it (`settings.trust_proxy_headers`). Otherwise a client could
    spoof the header and rotate identity to bypass the brute-force guard.
    """
    if settings.trust_proxy_headers:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
