import jwt
from typing import Optional
from bson import ObjectId
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from .config import settings
from .db import get_db

security = HTTPBearer()


def decode_jwt(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])


def user_id_from_token(token: str) -> Optional[str]:
    try:
        return decode_jwt(token).get("sub")
    except jwt.PyJWTError:
        return None


async def current_user(cred: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    try:
        payload = decode_jwt(cred.credentials)
        user_id = payload.get("sub")
    except jwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid token: {e}")
    user = await get_db().users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return user
