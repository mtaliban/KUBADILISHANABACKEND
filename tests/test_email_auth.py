"""Admin email login + email verification flow.

Admins authenticate with EMAIL (never a phone). Before admin access is granted
the email must be verified via a 6-digit code (request → confirm).
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from bson import ObjectId
import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

import app.modules.auth.routes as auth_routes
import app.modules.users.routes as users_routes
import app.modules.admin.routes as admin_routes
import app.security as security

from app.modules.auth.routes import router as auth_router
from app.modules.users.routes import router as users_router
from app.modules.admin.routes import router as admin_router
from app.events.topics import TOPIC_EMAIL_VERIFICATION_REQUESTED, TOPIC_EMAIL_VERIFIED
from app.security import create_access_token, hash_password, normalize_email

pytestmark = pytest.mark.asyncio

_ROUTERS = (auth_router, users_router, admin_router)
_DB_MODULES = (auth_routes, users_routes, admin_routes)


@pytest.fixture
def app(db, monkeypatch):
    application = FastAPI()
    for r in _ROUTERS:
        application.include_router(r)
    for mod in _DB_MODULES:
        monkeypatch.setattr(mod, "get_db", lambda: db)
    monkeypatch.setattr(security, "get_db", lambda: db)
    return application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def captured_events(monkeypatch):
    events: list[tuple[str, dict]] = []

    def fake(topic, payload, qos=1):
        events.append((topic, payload))

    for mod in _DB_MODULES:
        monkeypatch.setattr(mod, "publish", fake)
    return events


def _admin_doc(phone: str = "0763795801", *, email: str = "admin@kubadilishana.go.tz",
               email_verified: bool = False, password: str = "admin1234") -> dict:
    now = datetime.now(timezone.utc)
    return {
        "_id": ObjectId(),
        "full_name": "Admin Mkuu", "phone_primary": f"+255{phone[1:]}",
        "password_hash": hash_password(password),
        "category": "health", "cadre_code": "CO", "cadre_display": "Clinical Officer",
        "subjects": [],
        "current_station": {"region_id": 17, "region_name": "Mwanza", "district_id": 1701,
                            "district_name": "Nyamagana Dc", "facility_id": None, "facility_name": None},
        "desired_destinations": [], "status": "active", "is_verified": False,
        "is_admin": True, "email": email, "email_verified": email_verified,
        "created_at": now, "updated_at": now, "last_seen_at": now,
    }


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ─── Email verification flow ────────────────────────────────────────

async def test_email_verify_request_and_confirm_flow(app, db, client, captured_events):
    admin = _admin_doc(email_verified=False)
    await db.users.insert_one(admin)
    # Force a deterministic 6-digit code
    with patch.object(auth_routes.secrets, "randbelow", lambda n: 654321):
        r = await client.post("/auth/email/verify-request", json={
            "email": "admin@kubadilishana.go.tz", "password": "admin1234",
            "phone": "0763795801",
        })
    assert r.status_code == 200, r.text
    assert "code" in r.text.lower() or "umetumwa" in r.text

    # Email attached to the admin
    fresh = await db.users.find_one({"_id": admin["_id"]})
    assert fresh["email"] == "admin@kubadilishana.go.tz"
    assert fresh.get("email_verified") is not True

    # Verification event published
    assert any(t == TOPIC_EMAIL_VERIFICATION_REQUESTED for t, _ in captured_events)

    # Wrong code → rejected
    r = await client.post("/auth/email/verify", json={
        "email": "admin@kubadilishana.go.tz", "code": "000000",
    })
    assert r.status_code == 400

    # Correct code → verified
    r = await client.post("/auth/email/verify", json={
        "email": "admin@kubadilishana.go.tz", "code": "654321",
    })
    assert r.status_code == 200, r.text
    assert r.json()["email_verified"] is True

    fresh = await db.users.find_one({"_id": admin["_id"]})
    assert fresh["email_verified"] is True
    assert any(t == TOPIC_EMAIL_VERIFIED for t, _ in captured_events)


async def test_email_verify_confirmation_rate_limited(app, db, client, monkeypatch):
    """6-digit code must not be brute-forceable — verify is rate limited."""
    from app import security as sec
    monkeypatch.setattr(auth_routes, "client_ip", lambda req: "9.9.9.9")
    monkeypatch.setattr(sec, "_attempts", {})
    admin = _admin_doc(email_verified=False)
    await db.users.insert_one(admin)
    now = datetime.now(timezone.utc)
    await db.email_verifications.insert_one({
        "user_id": admin["_id"], "email": "admin@kubadilishana.go.tz",
        "code_hash": hash_password("654321"),
        "expires_at": now + timedelta(minutes=15), "created_at": now, "used": False,
    })
    last = None
    for _ in range(15):
        last = await client.post("/auth/email/verify", json={
            "email": "admin@kubadilishana.go.tz", "code": "999999",
        })
    assert last.status_code == 429, f"expected 429 after many wrong codes, got {last.status_code}"


async def test_email_verify_request_rejects_wrong_password(app, db, client):
    admin = _admin_doc(email_verified=False)
    await db.users.insert_one(admin)
    r = await client.post("/auth/email/verify-request", json={
        "email": "admin@kubadilishana.go.tz", "password": "wrongpass",
    })
    assert r.status_code == 401
    # Email must NOT be attached on failed attempt
    fresh = await db.users.find_one({"_id": admin["_id"]})
    assert fresh.get("email") == "admin@kubadilishana.go.tz"  # unchanged (pre-set)


async def test_email_verify_request_rejects_non_admin(app, db, client):
    now = datetime.now(timezone.utc)
    await db.users.insert_one({
        "_id": ObjectId(), "full_name": "Mtu", "phone_primary": "+255762222222",
        "password_hash": hash_password("secret123"), "category": "health",
        "cadre_code": "CO", "cadre_display": "Clinical Officer", "subjects": [],
        "current_station": {}, "desired_destinations": [], "status": "active",
        "is_admin": False, "created_at": now, "updated_at": now,
    })
    r = await client.post("/auth/email/verify-request", json={
        "email": "mtu@test.go.tz", "password": "secret123", "phone": "0762222222",
    })
    assert r.status_code == 403


async def test_email_verify_rejects_invalid_email_format(app, db, client):
    admin = _admin_doc(email_verified=False)
    await db.users.insert_one(admin)
    r = await client.post("/auth/email/verify-request", json={
        "email": "not-an-email", "password": "admin1234",
    })
    assert r.status_code == 422


# ─── Admin email login ──────────────────────────────────────────────

async def test_admin_email_login_success(app, db, client):
    admin = _admin_doc(email_verified=True)
    await db.users.insert_one(admin)
    r = await client.post("/auth/admin/login", json={
        "email": "admin@kubadilishana.go.tz", "password": "admin1234",
    })
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]


async def test_admin_email_login_blocked_until_verified(app, db, client):
    admin = _admin_doc(email_verified=False)
    await db.users.insert_one(admin)
    r = await client.post("/auth/admin/login", json={
        "email": "admin@kubadilishana.go.tz", "password": "admin1234",
    })
    assert r.status_code == 403
    assert "thibitisha" in r.text.lower() or "verified" in r.text.lower()


async def test_admin_email_login_wrong_password(app, db, client):
    admin = _admin_doc(email_verified=True)
    await db.users.insert_one(admin)
    r = await client.post("/auth/admin/login", json={
        "email": "admin@kubadilishana.go.tz", "password": "wrong",
    })
    assert r.status_code == 401


async def test_admin_email_login_non_admin_forbidden(app, db, client):
    now = datetime.now(timezone.utc)
    await db.users.insert_one({
        "_id": ObjectId(), "full_name": "Mtu", "phone_primary": "+255762233333",
        "password_hash": hash_password("secret123"), "category": "health",
        "cadre_code": "CO", "cadre_display": "Clinical Officer", "subjects": [],
        "current_station": {}, "desired_destinations": [], "status": "active",
        "is_admin": False, "email": "mtu@test.go.tz", "email_verified": True,
        "created_at": now, "updated_at": now,
    })
    r = await client.post("/auth/admin/login", json={
        "email": "mtu@test.go.tz", "password": "secret123",
    })
    assert r.status_code == 403


# ─── Phone login blocked for admins ─────────────────────────────────

async def test_admin_cannot_login_with_phone(app, db, client):
    admin = _admin_doc(email_verified=True)
    await db.users.insert_one(admin)
    r = await client.post("/auth/login", json={
        "phone": "0763795801", "password": "admin1234",
    })
    assert r.status_code == 403
    assert "EMAIL" in r.text.upper()


# ─── current_admin guard requires verified email ────────────────────

async def test_admin_endpoints_blocked_without_verified_email(app, db, client):
    admin = _admin_doc(email_verified=False)
    await db.users.insert_one(admin)
    tok = create_access_token(str(admin["_id"]))
    r = await client.get("/admin/stats", headers=_auth(tok))
    assert r.status_code == 403


async def test_admin_endpoints_work_with_verified_email(app, db, client):
    admin = _admin_doc(email_verified=True)
    await db.users.insert_one(admin)
    tok = create_access_token(str(admin["_id"]))
    r = await client.get("/admin/stats", headers=_auth(tok))
    assert r.status_code == 200, r.text


# ─── Email uniqueness ───────────────────────────────────────────────

async def test_email_cannot_be_taken_by_another_account(app, db, client):
    """Claiming an email owned by another account requires that account's password."""
    admin_a = _admin_doc("0763795801", email="admin@kubadilishana.go.tz",
                        email_verified=False, password="password-a")
    admin_b = _admin_doc("0763795802", email=None, email_verified=False, password="password-b")
    await db.users.insert_many([admin_a, admin_b])
    # admin_b (own password) tries to claim admin_a's email
    r = await client.post("/auth/email/verify-request", json={
        "email": "admin@kubadilishana.go.tz", "password": "password-b",
        "phone": "0763795802",
    })
    assert r.status_code == 401
    # Admin_b's own email stays untouched
    fresh_b = await db.users.find_one({"_id": admin_b["_id"]})
    assert fresh_b.get("email") is None


# ─── Normalization ──────────────────────────────────────────────────

async def test_normalize_email_lowercases_and_trims():
    assert normalize_email("  ADMIN@Kubadilishana.Go.Tz  ") == "admin@kubadilishana.go.tz"


async def test_normalize_email_rejects_bad_values():
    for bad in ["", "  ", "a", "a@", "@b.com", "a b@c.com", "a@b", "a@b.c" * 60]:
        try:
            normalize_email(bad)
            assert False, f"expected rejection for {bad!r}"
        except ValueError:
            pass
