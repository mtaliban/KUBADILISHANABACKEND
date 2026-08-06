import logging
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from pymongo.errors import DuplicateKeyError
from ...db import get_db
from ...security import hash_password, verify_password, create_access_token, normalize_phone, current_user
from ...events.publisher import publish
from ...events.topics import TOPIC_USER_REGISTERED
from .schemas import (
    RegisterRequest, RegisterResponse, LoginRequest, LoginResponse,
    ForgotPasswordRequest, ResetPasswordRequest,
)

logger = logging.getLogger(__name__)
RESET_CODE_TTL_MINUTES = 15

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest):
    db = get_db()
    try:
        phone = normalize_phone(body.phone_primary)
    except ValueError as e:
        raise HTTPException(422, str(e))
    phone_alt = None
    if body.phone_alt:
        try:
            phone_alt = normalize_phone(body.phone_alt)
        except ValueError as e:
            raise HTTPException(422, f"phone_alt: {e}")

    cadre = await db.cadres.find_one({"code": body.cadre_code}, {"_id": 0})
    if not cadre:
        raise HTTPException(422, f"Unknown cadre_code: {body.cadre_code}")
    if cadre["category"] != body.category:
        raise HTTPException(422, f"cadre {body.cadre_code} belongs to '{cadre['category']}', not '{body.category}'")
    if cadre.get("requires_subjects") and not body.subjects:
        raise HTTPException(422, "This cadre requires at least one subject")

    now = datetime.now(timezone.utc)
    doc = {
        "full_name": body.full_name.strip(),
        "phone_primary": phone, "phone_alt": phone_alt,
        "password_hash": hash_password(body.password),
        "category": body.category, "cadre_code": body.cadre_code,
        "cadre_display": cadre["display_name"], "subjects": body.subjects,
        "current_station": body.current_station.model_dump(),
        "desired_destinations": [d.model_dump() for d in body.desired_destinations],
        "status": "active", "is_verified": False, "is_admin": False,
        "notification_prefs": {"new_matches": True, "messages": True},
        "created_at": now, "updated_at": now, "last_seen_at": now,
    }
    try:
        result = await db.users.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Phone number already registered")

    uid = str(result.inserted_id)
    publish(TOPIC_USER_REGISTERED, {
        "event": "user.registered", "user_id": uid,
        "category": body.category, "cadre_code": body.cadre_code,
        "subjects": body.subjects,
        "current_station": doc["current_station"],
        "desired_destinations": doc["desired_destinations"],
        "occurred_at": now.isoformat(),
    })

    token = create_access_token(uid, {"category": body.category, "cadre": body.cadre_code})
    return RegisterResponse(user_id=uid, full_name=doc["full_name"], phone_primary=phone,
                            category=body.category, cadre_code=body.cadre_code, access_token=token)


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest):
    db = get_db()
    try:
        phone = normalize_phone(body.phone)
    except ValueError as e:
        raise HTTPException(422, str(e))
    user = await db.users.find_one({"phone_primary": phone})
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Invalid credentials")
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"last_seen_at": datetime.now(timezone.utc)}})
    token = create_access_token(str(user["_id"]), {"category": user["category"], "cadre": user["cadre_code"]})
    return LoginResponse(user_id=str(user["_id"]), full_name=user["full_name"], access_token=token)


@router.get("/me")
async def me(user=Depends(current_user)):
    return {
        "user_id": str(user["_id"]),
        "full_name": user["full_name"],
        "phone_primary": user["phone_primary"],
        "phone_alt": user.get("phone_alt"),
        "category": user["category"],
        "cadre_code": user["cadre_code"],
        "cadre_display": user.get("cadre_display", user["cadre_code"]),
        "subjects": user.get("subjects", []),
        "current_station": user["current_station"],
        "desired_destinations": user.get("desired_destinations", []),
        "status": user.get("status", "active"),
        "is_verified": user.get("is_verified", False),
        "is_admin": user.get("is_admin", False),
    }


@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordRequest):
    """
    Issues a 6-digit reset code, saves it hashed with 15-min TTL.
    DEV: code is logged to backend stdout (view: `docker logs kv_backend`).
    PROD: integrate with SMS provider (Beem Africa / Africa's Talking).
    """
    try:
        phone = normalize_phone(body.phone)
    except ValueError as e:
        raise HTTPException(422, str(e))

    db = get_db()
    user = await db.users.find_one({"phone_primary": phone}, {"_id": 1, "full_name": 1})
    # Do NOT reveal whether the account exists
    if user:
        code = f"{secrets.randbelow(1_000_000):06d}"
        now = datetime.now(timezone.utc)
        await db.password_resets.update_one(
            {"user_id": user["_id"]},
            {"$set": {
                "user_id": user["_id"], "phone": phone,
                "code_hash": hash_password(code),
                "expires_at": now + timedelta(minutes=RESET_CODE_TTL_MINUTES),
                "created_at": now, "used": False,
            }}, upsert=True,
        )
        logger.warning(f"🔑 Password reset code for {phone} ({user['full_name']}): {code}  (valid {RESET_CODE_TTL_MINUTES} min)")
    return {"ok": True, "message": f"Kama namba ipo, umepata code kwa SMS. Halali dakika {RESET_CODE_TTL_MINUTES}."}


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest):
    try:
        phone = normalize_phone(body.phone)
    except ValueError as e:
        raise HTTPException(422, str(e))
    db = get_db()
    user = await db.users.find_one({"phone_primary": phone})
    if not user:
        raise HTTPException(400, "Code batili au imekwisha muda")
    rec = await db.password_resets.find_one({"user_id": user["_id"], "used": False})
    if not rec:
        raise HTTPException(400, "Code batili au imekwisha muda")
    if rec["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(400, "Code imekwisha muda — omba mpya")
    if not verify_password(body.code, rec["code_hash"]):
        raise HTTPException(400, "Code batili au imekwisha muda")
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"password_hash": hash_password(body.new_password)}})
    await db.password_resets.update_one({"_id": rec["_id"]}, {"$set": {"used": True, "used_at": datetime.now(timezone.utc)}})
    logger.info(f"Password reset for {phone}")
    return {"ok": True, "message": "Password imebadilishwa. Ingia sasa."}


@router.get("/check-phone/{phone}")
async def check_phone(phone: str):
    try:
        p = normalize_phone(phone)
    except ValueError:
        return {"available": False, "reason": "invalid_format"}
    exists = await get_db().users.find_one({"phone_primary": p}, {"_id": 1})
    return {"available": not exists, "phone_normalized": p}
