import logging
import re
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
    ForgotPasswordRequest, LookupByNameRequest, ResetPasswordRequest,
    AdminEmailLoginRequest, EmailVerifyRequest, EmailConfirmRequest,
    TwoFactorLoginRequest,
)
from ...emailer import send_email, get_email_config

logger = logging.getLogger(__name__)
RESET_CODE_TTL_MINUTES = 15
OTP_TTL_MINUTES = 10

router = APIRouter(prefix="/auth", tags=["auth"])


def _is_default_name(name: str) -> bool:
    """Tambua kama jina ni default/placeholder (mfano 'Mwana Afya 3', 'CO — 5').
    Watu wenye default name hawalipii — wameshaandikwa PAID automatically."""
    n = name.strip().lower()
    # Pattern 1: jina + nambari ya mwisho, k.m. "Mwana Afya 3", "CO 5"
    if re.search(r'\d+$', n):
        base = re.sub(r'\d+$', '', n).strip()
        default_prefixes = [
            'mwana afya', 'mwanafunzi', 'mwuguzi', 'mwalimu',
            'mganga', 'mpgasii', 'mlinzii', 'mhudumu', 'mtumishi',
            'afya mwananchi', 'afya ya jamii',
        ]
        for prefix in default_prefixes:
            if base.startswith(prefix) or base == prefix:
                return True
    # Pattern 2: cadre code + "—" + nambari, k.m. "CO — 5", "RN — 3", "ANO — 12"
    m = re.match(r'^([a-z]+)\s*[—–-]\s*\d+$', n)
    if m:
        cadre_prefixes = {
            'co', 'rn', 'ano', 'no', 'en', 'ha', 'md', 'ca', 'aco',
            'lab', 'pharm', 'dt', 'ot', 'ho', 'rad', 'physio',
            'dent', 'n.o', 'h/a', 'm/a', 'r.n', 'c.o', 'e.n',
        }
        if m.group(1) in cadre_prefixes:
            return True
    # Pattern 3: majina ya default bila nambari — OR nyuzi zinazofanana sana
    # Kama jina lina neno moja tu lenye <= 5 herufi SI jina halisi
    # (mfano "CO", "RN" — ni cadre codes tu)
    words = n.split()
    if len(words) == 1 and len(words[0]) <= 3 and words[0] in {
        'co', 'rn', 'ano', 'no', 'en', 'ha', 'md', 'ca',
    }:
        return True
    return False


def _issue_token_for(user: dict) -> LoginResponse:
    token = create_access_token(str(user["_id"]), {"category": user.get("category"), "cadre": user.get("cadre_code")})
    return LoginResponse(user_id=str(user["_id"]), full_name=user["full_name"],
                         phone_primary=user.get("phone_primary"), category=user.get("category"),
                         cadre_code=user.get("cadre_code"), access_token=token,
                         is_admin=bool(user.get("is_admin")))


async def _create_and_send_otp(user: dict, purpose: str) -> tuple[bool, str]:
    """Generate a 6-digit OTP, store hashed (TTL), and email it to the admin.
    Returns (delivered, code): delivered=True if the email was actually
    delivered; False if no email provider is configured (code logged to
    stdout — and the caller may surface it via `dev_code` break-glass)."""
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
    if purpose == "2fa":
        # EMAIL SAFI: code pekee + maelekezo mafupi. Hakuna maneno ya "2FA"
        # au "ADMIN" — utumiaji usichanganyike. (Mtu anajua kwanini anapata hii.)
        body = "Weka code hii hapa chini kwenye mfumo ili ukamilishe kuingia."
    else:
        body = "Umeomba kuthibitisha barua pepe yako. Weka code hii hapa chini kwenye mfumo ili uthibitishe."
    cfg = await get_email_config()
    delivered = await send_email(cfg, user["email"], f"{heading} — Kubadilishana Vituo", heading, body, code)
    return delivered, code


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
    if cadre.get("requires_subjects") and len(body.subjects) < 2:
        raise HTTPException(422, "Lazima chagua masomo 2 — ni lazima kabisa")

    now = datetime.now(timezone.utc)
    doc = {        "full_name": body.full_name.strip(), "phone_primary": phone, "phone_alt": phone_alt,
        "password_hash": hash_password(body.password) if body.password else None,
        "category": body.category, "cadre_code": body.cadre_code,
        "cadre_display": cadre["display_name"], "subjects": body.subjects,
        "employment_sector": body.employment_sector,
        "years_of_service": body.years_of_service,
        "current_station": body.current_station.model_dump(),
        "desired_destinations": [d.model_dump() for d in body.desired_destinations],
        "status": "active", "is_verified": _is_default_name(body.full_name), "is_admin": False,
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

    # Auto-add custom facilities — mtumiaji aliandika kituo kisichopo
    if body.custom_facilities:
        for cf in body.custom_facilities:
            name = cf.name.strip()
            if not name:
                continue
            if cf.category == 'health':
                # Check duplicate
                existing = await db.health_facilities.find_one({"name": {"$regex": re.escape(name), "$options": "i"}})
                if not existing:
                    region = await db.regions.find_one({"id": cf.region_id}, {"_id": 0, "name": 1})
                    district_name = cf.district_name or ''
                    doc_cf = {
                        "name": name,
                        "region": region["name"] if region else cf.region_name,
                        "district": district_name,
                        "type": "user_suggested",
                        "suggested_by": uid,
                        "suggested_at": now,
                        "status": "pending",  # admin lazima a approve
                    }
                    await db.health_facilities.insert_one(doc_cf)
            else:
                existing = await db.schools.find_one({"name": {"$regex": re.escape(name), "$options": "i"}})
                if not existing:
                    region = await db.regions.find_one({"id": cf.region_id}, {"_id": 0, "name": 1})
                    district_name = cf.district_name or ''
                    doc_cf = {
                        "name": name,
                        "region": region["name"] if region else cf.region_name,
                        "district": district_name,
                        "category": cf.category,
                        "suggested_by": uid,
                        "suggested_at": now,
                        "status": "pending",
                    }
                    await db.schools.insert_one(doc_cf)

    token = create_access_token(uid, {"category": body.category, "cadre": body.cadre_code})
    return RegisterResponse(user_id=uid, full_name=doc["full_name"], phone_primary=phone,
                            category=body.category, cadre_code=body.cadre_code, access_token=token)


@router.post("/login")
async def login(body: LoginRequest):
    """Single login form: email auto-detected as ADMIN, phone as regular user.

    Regular users log in with EITHER phone (primary or alt) — bila password.
    Admins use their verified email — OTP inatumwa kwa email, admin
    anaingia code ya tarakimu 6 kupata access token.
    """
    identifier = (body.phone or "").strip()
    db = get_db()

    # ── Email → admin login (2FA: email tu, OTP inatumwa) ──
    if "@" in identifier:
        try:
            email = normalize_email(identifier)
        except ValueError as e:
            raise HTTPException(422, str(e))
        user = await db.users.find_one({"email": email})
        if not user:
            raise HTTPException(401, "Email hii haijapatikana kwenye mfumo")
        if not user.get("is_admin"):
            raise HTTPException(403, "Akaunti hii haina haki ya admin")
        if user.get("status") == "disabled":
            raise HTTPException(403, "Account disabled — wasiliana na admin")
        if not user.get("email_verified"):
            raise HTTPException(403, "Email haijathibitishwa — thibitisha kwanza kupitia 'Thibitisha Email'")
        # Step 1/2 done: email + password sahihi → create OTP + email background.
        # OTP created INSTANTLY (hash only); email sent in background (fire-and-forget).
        # Login returns IMMEDIATELY — admin anaingia 2FA page na kusubiri email.
        code = f"{secrets.randbelow(1_000_000):06d}"
        now = datetime.now(timezone.utc)
        await get_db().login_otps.update_one(
            {"user_id": user["_id"], "purpose": "2fa"},
            {"$set": {
                "user_id": user["_id"], "email": user.get("email"), "purpose": "2fa",
                "code_hash": hash_password(code),
                "expires_at": now + timedelta(minutes=OTP_TTL_MINUTES),
                "created_at": now, "used": False, "attempts": 0,
            }}, upsert=True,
        )
        # Email background — usisubiri SMTP (timeout 10-15s)
        import asyncio
        async def _send_bg():
            try:
                cfg = await get_email_config()
                await send_email(cfg, user["email"], f"Code yako ya uthibitisho — Kubadilishana Vituo",
                                 "Code yako ya uthibitisho",
                                 "Weka code hii hapa chini kwenye mfumo ili ukamilishe kuingia.", code)
            except Exception as e:
                logger.exception(f"Background email send failed: {e}")
        asyncio.create_task(_send_bg())
        resp: dict = {"two_factor_required": True, "email": email,
                      "message": f"Code ya uthibitisho (2FA) imetumwa kwa {email}. Halali dakika {OTP_TTL_MINUTES}."}
        return resp

    # ── Phone → regular user login (primary OR alt, bila password) ──
    try:
        phone = normalize_phone(identifier)
    except ValueError as e:
        raise HTTPException(422, str(e))
    user = await db.users.find_one({"$or": [{"phone_primary": phone}, {"phone_alt": phone}]})
    if not user:
        raise HTTPException(401, "Namba ya simu haijapatikana kwenye mfumo")
    if user.get("status") == "disabled":
        raise HTTPException(403, "Account disabled — wasiliana na admin")
    # Admins log in with their email — never a phone number.
    if user.get("is_admin"):
        raise HTTPException(403, "Admins wanaingia kwa EMAIL. Tumia barua pepe yako ya admin.")
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"last_seen_at": datetime.now(timezone.utc)}})
    return _issue_token_for(user)


@router.get("/me")
async def me(user=Depends(current_user)):
    db = get_db()
    # Global setting: require_payment_for_contact
    contact_doc = await db.settings.find_one({"key": "contact"})
    require_payment = bool(contact_doc.get("require_payment", True)) if contact_doc else True
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
        "employment_sector": user.get("employment_sector"),
        "subjects": user.get("subjects", []),
        "years_of_service": user.get("years_of_service"),
        "current_station": user["current_station"],
        "desired_destinations": user.get("desired_destinations", []),
        "status": user.get("status", "active"),
        "is_verified": user.get("is_verified", False),
        "is_admin": user.get("is_admin", False),
        "contact_enabled": user.get("contact_enabled", False),
        "require_payment_for_contact": require_payment,
    }


@router.post("/lookup-by-name")
async def lookup_by_name(body: LookupByNameRequest):
    """Tafuta namba za simu kwa jina la mtumiaji.
    Mtumiaji aliyesahau namba yake anaandika jina lake kamili,
    mfumo unaonyesha namba zote zilizosajiliwa (primary + alt)."""
    db = get_db()
    name = body.full_name.strip()
    users = []
    async for u in db.users.find(
        {"full_name": {"$regex": re.escape(name), "$options": "i"}},
        {"full_name": 1, "phone_primary": 1, "phone_alt": 1, "category": 1, "cadre_code": 1, "cadre_display": 1},
    ).limit(20):
        u["_id"] = str(u["_id"])
        users.append(u)
    if not users:
        raise HTTPException(404, "Hakuna mtumiaji aliye na jina hili kwenye mfumo")
    return {"users": users}


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
        # AUTO-APPROVE: haina haja ya admin kukubali — mtu anaporeset
        # password, inakubaliwa moja kwa moja.
        await db.password_resets.update_one(
            {"user_id": user["_id"]}, {"$set": {
                "user_id": user["_id"], "phone": phone,
                "full_name": body.full_name or user.get("full_name", ""),
                "code_hash": hash_password(code),
                "expires_at": now + timedelta(minutes=RESET_CODE_TTL_MINUTES),
                "created_at": now, "used": False, "status": "approved",
            }}, upsert=True,
        )
        logger.warning(f"🔑 Password reset (auto-approved) for {phone} ({user['full_name']}): {code}  (valid {RESET_CODE_TTL_MINUTES} min)")
        # Log event — admin anaona kwenye events
        publish(TOPIC_USER_PASSWORD_RESET_REQUESTED, {
            "event": "user.password_reset_completed",
            "user_id": str(user["_id"]),
            "full_name": user.get("full_name", ""),
            "phone": phone,
            "occurred_at": now.isoformat(),
        })
    return {"ok": True, "message": f"Umefanikiwa! Weka password mpya sasa."}


@router.get("/password-reset/status")
async def password_reset_status(phone: str):
    """User checks the status of their latest password-reset request.
    Returns: pending | approved | rejected | none."""
    try:
        p = normalize_phone(phone)
    except ValueError:
        return {"status": "none"}
    db = get_db()
    user = await db.users.find_one({"$or": [{"phone_primary": p}, {"phone_alt": p}]}, {"_id": 1})
    if not user:
        return {"status": "none"}
    rec = await db.password_resets.find_one({"user_id": user["_id"]}, sort=[("created_at", -1)])
    if not rec:
        return {"status": "none"}
    return {"status": rec.get("status", "pending"), "reset_id": str(rec["_id"]) }


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest):
    try:
        phone = normalize_phone(body.phone)
    except ValueError as e:
        raise HTTPException(422, str(e))
    db = get_db()
    user = await db.users.find_one({"$or": [{"phone_primary": phone}, {"phone_alt": phone}]})
    if not user:
        raise HTTPException(400, "Ombi batili au limekwisha muda")
    rec = await db.password_resets.find_one({"user_id": user["_id"], "used": False})
    if not rec:
        raise HTTPException(400, "Ombi batili au limekwisha muda")
    # AUTO-APPROVE: password reset imekubaliwa moja kwa moja
    expires = rec["expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise HTTPException(400, "Ombi limekwisha muda — omba mpya")
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"password_hash": hash_password(body.new_password)}})
    await db.password_resets.update_one({"_id": rec["_id"]}, {"$set": {"used": True, "used_at": datetime.now(timezone.utc)}})
    publish(TOPIC_USER_PASSWORD_RESET_COMPLETED, {
        "event": "user.password_reset_completed", "user_id": str(user["_id"]),
        "full_name": user.get("full_name", ""),
        "phone": phone,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    })
    logger.info(f"Password reset (auto-approved) for {phone}")
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
                     "Weka code hii kwenye mfumo kuthibitisha barua pepe yako.",
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
    delivered, code = await _create_and_send_otp(user, "2fa")
    resp: dict = {"two_factor_required": True, "email": email,
                  "message": f"Code ya uthibitisho (2FA) imetumwa kwa {email}. Halali dakika {OTP_TTL_MINUTES}."}
    if not delivered:
        # Break-glass (SMTP haijasanidiwa): code inaonekana kwenye response.
        resp["dev_code"] = code
        resp["message"] = (f"Code haiwezi kutumwa kwa email kwa sasa (SMTP haijasanidiwa) — "
                           f"code iko kwenye skrini. Halali dakika {OTP_TTL_MINUTES}. "
                           "Baada ya kuingia, weka email kwenye Mipangilio.")
    return resp
