import logging
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from pymongo.errors import DuplicateKeyError
from ...db import get_db
from ...security import (
    hash_password, verify_password, create_access_token, normalize_phone, normalize_email,
    current_user,
)
from ...events.publisher import publish
from ...events.topics import (
    TOPIC_USER_REGISTERED, TOPIC_USER_PASSWORD_RESET_REQUESTED, TOPIC_USER_PASSWORD_RESET_COMPLETED,
    TOPIC_EMAIL_VERIFICATION_REQUESTED, TOPIC_EMAIL_VERIFIED,
)
from .schemas import (
    RegisterRequest, RegisterResponse, LoginRequest, LoginResponse,
    ForgotPasswordRequest, ResetPasswordRequest,
    AdminEmailLoginRequest, EmailVerifyRequest, EmailConfirmRequest,
    TwoFactorLoginRequest,
)
from ...emailer import send_email, get_email_config

logger = logging.getLogger(__name__)
RESET_CODE_TTL_MINUTES = 15
OTP_TTL_MINUTES = 10

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue_token_for(user: dict) -> LoginResponse:
    token = create_access_token(str(user["_id"]), {"category": user.get("category"), "cadre": user.get("cadre_code")})
    return LoginResponse(user_id=str(user["_id"]), full_name=user["full_name"],
                         phone_primary=user.get("phone_primary"), category=user.get("category"),
                         cadre_code=user.get("cadre_code"), access_token=token,
                         is_admin=bool(user.get("is_admin")))


async def _create_and_send_otp(user: dict, purpose: str) -> bool:
    """Generate a 6-digit OTP, store hashed (TTL), and email it to the admin.
    Returns True if the email was actually delivered; False if no email
    provider is configured (code logged to stdout for dev)."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = datetime.now(timezone.utc)
    await get_db().login_otps.update_one(
        {"user_id": user["_id"], "purpose": purpose},
        {"$set": {
            "user_id": user["_id"], "email": user.get("email"), "purpose": purpose,
            "code_hash": hash_password(code),
            "expires_at": now + timedelta(minutes=OTP_TTL_MINUTES),
            "created_at": now, "used": False, "attempts": 0,
        }}, upsert=True,
    )
    heading = "Code yako ya uthibitisho" if purpose == "2fa" else "Thibitisha Email yako"
    body = ("Unaingia kwenye akaunti yako ya ADMIN. Weka code hii hapa chini kwenye mfumo ili ukamilishe kuingia (2FA)."
            if purpose == "2fa" else
            "Umeomba kuthibitisha barua pepe yako ya admin. Weka code hii hapa chini kwenye mfumo ili uthibitishe.")
    cfg = await get_email_config()
    return await send_email(cfg, user["email"], f"{heading} — Kubadilishana Vituo", heading, body, code)


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
        "followed_regions": [],
        "created_at": now, "updated_at": now, "last_seen_at": now,
    }
    try:
        result = await db.users.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Phone number already registered")

    uid = str(result.inserted_id)
    # Rich payload so live dashboards can render the request card immediately
    # (Uber-style request feed) without extra lookups.
    publish(TOPIC_USER_REGISTERED, {
        "event": "user.registered", "user_id": uid,
        "full_name": doc["full_name"], "phone_primary": phone,
        "category": body.category, "cadre_code": body.cadre_code,
        "cadre_display": cadre["display_name"], "subjects": body.subjects,
        "current_station": doc["current_station"],
        "desired_destinations": doc["desired_destinations"],
        "occurred_at": now.isoformat(),
    })

    token = create_access_token(uid, {"category": body.category, "cadre": body.cadre_code})
    return RegisterResponse(user_id=uid, full_name=doc["full_name"], phone_primary=phone,
                            category=body.category, cadre_code=body.cadre_code, access_token=token)


@router.post("/login")
async def login(body: LoginRequest):
    """Single login form: email auto-detected as ADMIN, phone as regular user.

    Regular users can log in with EITHER phone (primary or alt). Admins must
    use their verified email — a phone number never grants admin access.
    Admin login now uses TWO-FACTOR AUTHENTICATION: correct email+password
    issues an OTP that is emailed to the admin; the admin submits it via
    `POST /auth/login/2fa` to receive the access token.
    """
    identifier = (body.phone or "").strip()
    db = get_db()

    # ── Email → admin login (2FA: password step first, OTP emailed next) ──
    if "@" in identifier:
        try:
            email = normalize_email(identifier)
        except ValueError as e:
            raise HTTPException(422, str(e))
        user = await db.users.find_one({"email": email})
        if not user or not verify_password(body.password, user["password_hash"]):
            raise HTTPException(401, "Email au password haiko sahihi")
        if not user.get("is_admin"):
            raise HTTPException(403, "Akaunti hii haina haki ya admin")
        if user.get("status") == "disabled":
            raise HTTPException(403, "Account disabled — wasiliana na admin")
        if not user.get("email_verified"):
            raise HTTPException(403, "Email haijathibitishwa — thibitisha kwanza kupitia 'Thibitisha Email'")
        # Step 1/2 done: email + password sahihi → email the 6-digit OTP.
        delivered = await _create_and_send_otp(user, "2fa")
        hint = "" if delivered else " (email haijafika? Weka mipangilio ya SMTP kwenye Admin → Mipangilio)"
        return {"two_factor_required": True, "email": email,
                "message": f"Code ya uthibitisho (2FA) imetumwa kwa {email}. Halali dakika {OTP_TTL_MINUTES}.{hint}"}

    # ── Phone → regular user login (primary OR alt) ──
    try:
        phone = normalize_phone(identifier)
    except ValueError as e:
        raise HTTPException(422, str(e))
    user = await db.users.find_one({"$or": [{"phone_primary": phone}, {"phone_alt": phone}]})
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Invalid credentials")
    if user.get("status") == "disabled":
        raise HTTPException(403, "Account disabled — wasiliana na admin")
    # Admins log in with their email — never a phone number.
    if user.get("is_admin"):
        raise HTTPException(403, "Admins wanaingia kwa EMAIL. Tumia barua pepe yako ya admin.")
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"last_seen_at": datetime.now(timezone.utc)}})
    return _issue_token_for(user)


@router.get("/me")
async def me(user=Depends(current_user)):
    return {
        "user_id": str(user["_id"]),
        "full_name": user["full_name"],
        "phone_primary": user["phone_primary"],
        "phone_alt": user.get("phone_alt"),
        "email": user.get("email"),
        "email_verified": user.get("email_verified", False),
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
    user = await db.users.find_one({"$or": [{"phone_primary": phone}, {"phone_alt": phone}]},
                                   {"_id": 1, "full_name": 1})
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
        # user_id suffices for audit — phone is PII and already stored elsewhere.
        publish(TOPIC_USER_PASSWORD_RESET_REQUESTED, {
            "event": "user.password_reset_requested", "user_id": str(user["_id"]),
            "occurred_at": now.isoformat(),
        })
    return {"ok": True, "message": f"Kama namba ipo, umepata code kwa SMS. Halali dakika {RESET_CODE_TTL_MINUTES}."}


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest):
    try:
        phone = normalize_phone(body.phone)
    except ValueError as e:
        raise HTTPException(422, str(e))
    db = get_db()
    user = await db.users.find_one({"$or": [{"phone_primary": phone}, {"phone_alt": phone}]})
    if not user:
        raise HTTPException(400, "Code batili au imekwisha muda")
    rec = await db.password_resets.find_one({"user_id": user["_id"], "used": False})
    if not rec:
        raise HTTPException(400, "Code batili au imekwisha muda")
    # BSON/PyMongo inarudisha datetimes zisizo na tzinfo (naive UTC) —
    # linganisha kwa utulivu kabla ya kufanya comparison.
    expires = rec["expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise HTTPException(400, "Code imekwisha muda — omba mpya")
    if not verify_password(body.code, rec["code_hash"]):
        raise HTTPException(400, "Code batili au imekwisha muda")
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"password_hash": hash_password(body.new_password)}})
    await db.password_resets.update_one({"_id": rec["_id"]}, {"$set": {"used": True, "used_at": datetime.now(timezone.utc)}})
    publish(TOPIC_USER_PASSWORD_RESET_COMPLETED, {
        "event": "user.password_reset_completed", "user_id": str(user["_id"]),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    })
    logger.info(f"Password reset for {phone}")
    return {"ok": True, "message": "Password imebadilishwa. Ingia sasa."}


@router.get("/check-phone/{phone}")
async def check_phone(phone: str):
    try:
        p = normalize_phone(phone)
    except ValueError:
        return {"available": False, "reason": "invalid_format"}
    # Taken if used as primary OR alt on any account.
    exists = await get_db().users.find_one({"$or": [{"phone_primary": p}, {"phone_alt": p}]}, {"_id": 1})
    return {"available": not exists, "phone_normalized": p}


# ─── Admin email login + verification ────────────────────────────────────

@router.post("/email/verify-request")
async def request_email_verification(body: EmailVerifyRequest):
    """Attach + verify the admin's own email.

    `phone` identifies the account (first-time enrolment, before email exists),
    `password` proves ownership, and `email` is what gets verified. A 6-digit
    code is generated (DEV: logged to backend stdout). This is intentionally
    login-free so the very first admin can enrol.
    """
    try:
        email = normalize_email(body.email)
    except ValueError as e:
        raise HTTPException(422, str(e))
    db = get_db()

    # Find by email first (re-verification / later flow), else by phone (enrolment).
    user = await db.users.find_one({"email": email}, {"password_hash": 1, "is_admin": 1, "full_name": 1, "email": 1})
    if not user and body.phone:
        try:
            phone = normalize_phone(body.phone)
        except ValueError:
            phone = None
        if phone:
            user = await db.users.find_one({"phone_primary": phone},
                                           {"password_hash": 1, "is_admin": 1, "full_name": 1, "email": 1})
    if not user or not verify_password(body.password, user["password_hash"]):
        # Generic reply — never reveal whether an account exists.
        raise HTTPException(401, "Email, password au namba haiko sahihi")
    if not user.get("is_admin"):
        raise HTTPException(403, "Email verification ni kwa admins tu")
    if user.get("status") == "disabled":
        raise HTTPException(403, "Account disabled — wasiliana na admin")

    # Attach email (may be the first time for legacy admin accounts). The unique
    # sparse index enforces the one-account-per-email rule — catch the race.
    try:
        await db.users.update_one({"_id": user["_id"]}, {"$set": {"email": email}})
    except DuplicateKeyError:
        raise HTTPException(409, "Email hii inatumiwa na akaunti nyingine")

    code = f"{secrets.randbelow(1_000_000):06d}"
    now = datetime.now(timezone.utc)
    await db.email_verifications.update_one(
        {"user_id": user["_id"]},
        {"$set": {"user_id": user["_id"], "email": email,
                  "code_hash": hash_password(code),
                  "expires_at": now + timedelta(minutes=RESET_CODE_TTL_MINUTES),
                  "created_at": now, "used": False}}, upsert=True,
    )
    cfg = await get_email_config()
    await send_email(cfg, email, "Thibitisha Email yako — Kubadilishana Vituo",
                     "Thibitisha Email yako",
                     f"Habari {user['full_name']}, weka code hii kwenye mfumo kuthibitisha barua pepe yako ya admin.",
                     code)
    publish(TOPIC_EMAIL_VERIFICATION_REQUESTED, {
        "event": "email.verification_requested", "user_id": str(user["_id"]),
        "email": email, "occurred_at": now.isoformat(),
    })
    return {"ok": True, "message": f"Code ya uthibitisho imetumwa kwa {email}. Halali dakika {RESET_CODE_TTL_MINUTES}."}


@router.post("/email/verify")
async def confirm_email_verification(body: EmailConfirmRequest):
    """Submit the 6-digit code to mark the email as verified."""
    try:
        email = normalize_email(body.email)
    except ValueError as e:
        raise HTTPException(422, str(e))
    db = get_db()
    user = await db.users.find_one({"email": email}, {"_id": 1, "is_admin": 1})
    if not user:
        raise HTTPException(400, "Code batili au imekwisha muda")
    rec = await db.email_verifications.find_one({"user_id": user["_id"], "used": False})
    if not rec:
        raise HTTPException(400, "Code batili au imekwisha muda")
    expires = rec["expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise HTTPException(400, "Code imekwisha muda — omba mpya")
    if not verify_password(body.code, rec["code_hash"]):
        raise HTTPException(400, "Code batili au imekwisha muda")
    await db.users.update_one({"_id": user["_id"]},
                              {"$set": {"email_verified": True, "updated_at": datetime.now(timezone.utc)}})
    await db.email_verifications.update_one({"_id": rec["_id"]},
                                            {"$set": {"used": True, "used_at": datetime.now(timezone.utc)}})
    publish(TOPIC_EMAIL_VERIFIED, {
        "event": "email.verified", "user_id": str(user["_id"]),
        "email": email, "occurred_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"ok": True, "email_verified": True, "message": "Email imethibitishwa. Unaweza kuingia sasa."}


@router.post("/login/2fa")
async def login_2fa(body: TwoFactorLoginRequest):
    """Second step of admin login: submit the emailed OTP to get the token.
    Guards against brute-force: max 5 wrong attempts per OTP."""
    try:
        email = normalize_email(body.email)
    except ValueError as e:
        raise HTTPException(422, str(e))
    db = get_db()
    user = await db.users.find_one({"email": email})
    if not user or not user.get("is_admin"):
        raise HTTPException(400, "Code batili au imekwisha muda")
    rec = await db.login_otps.find_one({"user_id": user["_id"], "purpose": "2fa", "used": False})
    if not rec:
        raise HTTPException(400, "Code batili au imekwisha muda")
    expires = rec["expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise HTTPException(400, "Code imekwisha muda — ingia tena kupata code mpya")
    if (rec.get("attempts") or 0) >= 5:
        raise HTTPException(429, "Majruba mengi sana — ingia tena kupata code mpya")
    if not verify_password(body.code, rec["code_hash"]):
        await db.login_otps.update_one({"_id": rec["_id"]}, {"$inc": {"attempts": 1}})
        raise HTTPException(400, "Code batili au imekwisha muda")
    await db.login_otps.update_one({"_id": rec["_id"]}, {"$set": {"used": True, "used_at": datetime.now(timezone.utc)}})
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"last_seen_at": datetime.now(timezone.utc)}})
    return _issue_token_for(user)


@router.post("/admin/login")
async def admin_login(body: AdminEmailLoginRequest):
    """Admin logs in with EMAIL + password (never a phone number).
    (2FA: email+password sahihi → OTP inatumwa kwa email; kamilisha kwa
    `POST /auth/login/2fa` ili kupata token.)"""
    try:
        email = normalize_email(body.email)
    except ValueError as e:
        raise HTTPException(422, str(e))
    db = get_db()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Email au password haiko sahihi")
    if not user.get("is_admin"):
        raise HTTPException(403, "Akaunti hii haina haki ya admin")
    if user.get("status") == "disabled":
        raise HTTPException(403, "Account disabled — wasiliana na admin")
    if not user.get("email_verified"):
        raise HTTPException(403, "Email haijathibitishwa — thibitisha kwanza kupitia 'Thibitisha Email'")
    delivered = await _create_and_send_otp(user, "2fa")
    hint = "" if delivered else " (email haijafika? Weka mipangilio ya SMTP kwenye Admin → Mipangilio)"
    return {"two_factor_required": True, "email": email,
            "message": f"Code ya uthibitisho (2FA) imetumwa kwa {email}. Halali dakika {OTP_TTL_MINUTES}.{hint}"}
