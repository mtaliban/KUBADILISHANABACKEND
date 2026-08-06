import bcrypt
import jwt
from datetime import datetime, timedelta, timezone
from typing import Any
from bson import ObjectId
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from .config import settings
from .db import get_db


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


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


security = HTTPBearer()


async def current_user(cred: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    try:
        payload = decode_token(cred.credentials)
        user_id = payload.get("sub")
    except jwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid token: {e}")
    user = await get_db().users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return user


async def current_admin(user=Depends(current_user)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return user


def user_id_from_token(token: str):
    try:
        return decode_token(token).get("sub")
    except jwt.PyJWTError:
        return None
