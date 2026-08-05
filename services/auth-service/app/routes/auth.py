from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, HTTPException, status
from pymongo.errors import DuplicateKeyError

from ..core.db import get_db
from ..core.security import (
    hash_password, verify_password, create_access_token, normalize_phone
)
from ..models.user import (
    RegisterRequest, RegisterResponse, LoginRequest, LoginResponse
)
from ..events.publisher import publish
from ..events.schemas import (
    UserRegisteredEvent, Station, Destination, TOPIC_USER_REGISTERED
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest):
    """
    Register a new user (Publisher of user.registered event).

    Flow:
    1. Validate & normalize phone
    2. Verify cadre_code exists and matches category
    3. If cadre requires subjects, ensure user provided some
    4. Insert user into MongoDB (unique on phone_primary)
    5. Publish `kv/user/registered` event to MQTT
    6. Return JWT so user can immediately access authenticated routes
    """
    db = get_db()

    # 1. normalize + validate phone
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

    # 2. verify cadre exists + matches category
    cadre = await db.cadres.find_one({"code": body.cadre_code}, {"_id": 0})
    if not cadre:
        raise HTTPException(422, f"Unknown cadre_code: {body.cadre_code}")
    if cadre["category"] != body.category:
        raise HTTPException(
            422,
            f"cadre {body.cadre_code} belongs to '{cadre['category']}', "
            f"not '{body.category}'",
        )

    # 3. subjects required for TEACHER_SECONDARY
    if cadre.get("requires_subjects") and not body.subjects:
        raise HTTPException(422, "This cadre requires at least one subject")

    # 4. build user doc
    now = datetime.now(timezone.utc)
    user_doc = {
        "full_name": body.full_name.strip(),
        "phone_primary": phone,
        "phone_alt": phone_alt,
        "password_hash": hash_password(body.password),
        "category": body.category,
        "cadre_code": body.cadre_code,
        "cadre_display": cadre["display_name"],
        "subjects": body.subjects,
        "current_station": body.current_station.model_dump(),
        "desired_destinations": [d.model_dump() for d in body.desired_destinations],
        "status": "active",
        "is_verified": False,
        "notification_prefs": {"new_matches": True, "messages": True},
        "created_at": now,
        "updated_at": now,
        "last_seen_at": now,
    }

    try:
        result = await db.users.insert_one(user_doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Phone number already registered")

    user_id = str(result.inserted_id)

    # 5. publish event — subscribers (match-service, analytics-service) handle async
    event = UserRegisteredEvent(
        user_id=user_id,
        category=body.category,
        cadre_code=body.cadre_code,
        subjects=body.subjects,
        current_station=Station(**body.current_station.model_dump()),
        desired_destinations=[Destination(**d.model_dump()) for d in body.desired_destinations],
    )
    publish(TOPIC_USER_REGISTERED, event.model_dump(mode="json"))

    # 6. issue JWT
    token = create_access_token(
        subject=user_id,
        extra={"category": body.category, "cadre": body.cadre_code},
    )

    return RegisterResponse(
        user_id=user_id,
        full_name=user_doc["full_name"],
        phone_primary=phone,
        category=body.category,
        cadre_code=body.cadre_code,
        access_token=token,
    )


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest):
    db = get_db()
    try:
        phone = normalize_phone(body.phone)
    except ValueError as e:
        raise HTTPException(422, str(e))

    user = await db.users.find_one({"phone_primary": phone})
    if not user:
        raise HTTPException(401, "Invalid credentials")
    if not verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Invalid credentials")

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"last_seen_at": datetime.now(timezone.utc)}},
    )

    token = create_access_token(
        subject=str(user["_id"]),
        extra={"category": user["category"], "cadre": user["cadre_code"]},
    )
    return LoginResponse(
        user_id=str(user["_id"]),
        full_name=user["full_name"],
        access_token=token,
    )


@router.get("/check-phone/{phone}")
async def check_phone_available(phone: str):
    """Kabla ya kusubmit form — kagua kama simu tayari imesajiliwa."""
    try:
        p = normalize_phone(phone)
    except ValueError:
        return {"available": False, "reason": "invalid_format"}
    exists = await get_db().users.find_one({"phone_primary": p}, {"_id": 1})
    return {"available": not exists, "phone_normalized": p}
