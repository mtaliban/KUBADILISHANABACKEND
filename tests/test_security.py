"""Security tests: auth guards, JWT edge cases, injection resistance, rate limiting, PII safety."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from bson import ObjectId
import jwt
import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

import app.modules.auth.routes as auth_routes
import app.modules.locations.routes as loc_routes
import app.modules.users.routes as user_routes
import app.modules.messaging.routes as msg_routes
import app.modules.admin.routes as admin_routes
import app.modules.announcements.routes as ann_routes
import app.modules.notifications.routes as notif_routes
import app.security as security

from app.modules.auth.routes import router as auth_router
from app.modules.locations.routes import router as loc_router
from app.modules.users.routes import router as user_router
from app.modules.messaging.routes import router as msg_router
from app.modules.messaging.routes import ws_router as msg_ws_router
from app.modules.admin.routes import router as admin_router
from app.modules.announcements.routes import router as ann_router
from app.modules.notifications.routes import router as notif_router
from app.security import create_access_token, hash_password
from app.config import settings

pytestmark = pytest.mark.asyncio

_ROUTERS = (auth_router, loc_router, user_router, msg_router, msg_ws_router,
            admin_router, ann_router, notif_router)
_DB_MODULES = (auth_routes, loc_routes, user_routes, msg_routes, admin_routes,
               ann_routes, notif_routes)
_PUB_MODULES = (auth_routes, msg_routes, ann_routes)


@pytest.fixture
def app(db, monkeypatch):
    application = FastAPI()
    for r in _ROUTERS:
        application.include_router(r)
    for mod in _DB_MODULES:
        monkeypatch.setattr(mod, "get_db", lambda: db)
    monkeypatch.setattr(security, "get_db", lambda: db)

    # locations.cached normally talks to Redis; bypass with an identity loader.
    async def fake_cached(key, loader, ttl=None):
        return await loader()

    monkeypatch.setattr(loc_routes, "cached", fake_cached)
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

    for mod in _PUB_MODULES:
        monkeypatch.setattr(mod, "publish", fake)
    return events


def _user_doc(phone: str, *, category: str = "health", is_admin: bool = False,
              status: str = "active") -> dict:
    now = datetime.now(timezone.utc)
    return {
        "_id": ObjectId(),
        "full_name": f"Mtu {phone}", "phone_primary": f"+255{phone[1:]}",
        "password_hash": hash_password("secret123"),
        "category": category, "cadre_code": "CO" if category == "health" else "TT",
        "cadre_display": "Clinical Officer" if category == "health" else "Teacher",
        "subjects": [],
        "current_station": {"region_id": 17, "region_name": "Mwanza", "district_id": 1701,
                            "district_name": "Nyamagana Dc", "facility_id": None, "facility_name": None},
        "desired_destinations": [], "status": status, "is_verified": False,
        "is_admin": is_admin,
        "email": f"admin{phone[3:]}@test.go.tz" if is_admin else None,
        "email_verified": is_admin,
        "created_at": now, "updated_at": now, "last_seen_at": now,
    }


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seed_cadres(db):
    await db.cadres.insert_many([
        {"code": "CO", "category": "health", "display_name": "Clinical Officer",
         "requires_subjects": False},
        {"code": "TT", "category": "education", "display_name": "Teacher",
         "requires_subjects": True},
    ])


async def _register_body(phone: str) -> dict:
    return {"full_name": "Mtu Mkuu", "phone_primary": phone, "password": "secret123",
            "category": "health", "cadre_code": "CO", "subjects": [],
            "current_station": {"region_id": 17, "region_name": "Mwanza", "district_id": 1701,
                                "district_name": "Nyamagana Dc"},
            "desired_destinations": [{"region_id": 1, "region_name": "Arusha"}]}


# ─── JWT edge cases ───────────────────────────────────────────────────────

async def test_no_token_rejected(app, client):
    r = await client.get("/auth/me")
    assert r.status_code == 401


async def test_garbage_token_rejected(app, db, client):
    u = _user_doc("0767000001")
    await db.users.insert_one(u)
    r = await client.get("/auth/me", headers=_auth("not.a.jwt"))
    assert r.status_code == 401


async def test_expired_token_rejected(app, db, client):
    u = _user_doc("0767000002")
    await db.users.insert_one(u)
    now = datetime.now(timezone.utc)
    expired = jwt.encode(
        {"sub": str(u["_id"]), "iat": now - timedelta(hours=2), "exp": now - timedelta(hours=1)},
        settings.jwt_secret, algorithm=settings.jwt_alg,
    )
    r = await client.get("/auth/me", headers=_auth(expired))
    assert r.status_code == 401


async def test_forged_token_other_secret_rejected(app, db, client):
    u = _user_doc("0767000003")
    await db.users.insert_one(u)
    forged = jwt.encode({"sub": str(u["_id"])}, "wrong-secret", algorithm="HS256")
    r = await client.get("/auth/me", headers=_auth(forged))
    assert r.status_code == 401


async def test_invalid_token_subject_rejected(app, db, client):
    """sub is not a valid ObjectId → 401, NOT a 500 crash."""
    u = _user_doc("0767000004")
    await db.users.insert_one(u)
    bad = create_access_token("; DROP TABLE users; --")
    r = await client.get("/auth/me", headers=_auth(bad))
    assert r.status_code == 401


async def test_unknown_user_token_rejected(app, db, client):
    """Token for a user who no longer exists → 401."""
    ghost = create_access_token(str(ObjectId()))
    r = await client.get("/auth/me", headers=_auth(ghost))
    assert r.status_code == 401


async def test_disabled_account_immediately_blocked(app, db, client):
    """Disabled user is blocked on every request, not just at login."""
    u = _user_doc("0767000005", status="disabled")
    await db.users.insert_one(u)
    tok = create_access_token(str(u["_id"]))
    r = await client.get("/auth/me", headers=_auth(tok))
    assert r.status_code == 403
    await _seed_cadres(db)
    r = await client.post("/auth/register", json=await _register_body("0767000006"))
    assert r.status_code == 201


# ─── Admin authorization guard ────────────────────────────────────────────

async def test_non_admin_cannot_access_admin_endpoints(app, db, client):
    u = _user_doc("0767000007")
    await db.users.insert_one(u)
    tok = create_access_token(str(u["_id"]))
    for method, path in [
        ("get", "/admin/users"), ("get", "/admin/reports"),
        ("post", "/admin/announcements"), ("get", "/admin/stats"),
        ("get", "/admin/events"), ("get", "/admin/monitoring"),
    ]:
        r = await client.request(method, path, headers=_auth(tok))
        assert r.status_code == 403, f"{method.upper()} {path} → {r.status_code}"


async def test_admin_can_access_admin_endpoints(app, db, client):
    a = _user_doc("0767000008", is_admin=True)
    await db.users.insert_one(a)
    tok = create_access_token(str(a["_id"]))
    r = await client.get("/admin/stats", headers=_auth(tok))
    assert r.status_code == 200, r.text
    r = await client.get("/admin/users", headers=_auth(tok))
    assert r.status_code == 200, r.text


# ─── ObjectId / NoSQL injection resistance ────────────────────────────────

async def test_invalid_recipient_id_returns_400_not_500(app, db, client):
    """to_user_id that is not a valid ObjectId → 400 (no crash)."""
    u = _user_doc("0767000009")
    await db.users.insert_one(u)
    tok = create_access_token(str(u["_id"]))
    r = await client.post("/messages", headers=_auth(tok),
                          json={"to_user_id": "not-an-objectid", "text": "hi"})
    assert r.status_code == 400, r.text


async def test_nosql_injection_in_recipient_rejected(app, db, client):
    """Mongo operator injection ($ne) in to_user_id must NOT bypass lookups."""
    u = _user_doc("0767000010")
    await db.users.insert_one(u)
    tok = create_access_token(str(u["_id"]))
    r = await client.post("/messages", headers=_auth(tok),
                          json={"to_user_id": '{"$ne": null}', "text": "hi"})
    assert r.status_code in (400, 422), r.text


async def test_invalid_announcement_id_400(app, db, client):
    u = _user_doc("0767000011")
    await db.users.insert_one(u)
    tok = create_access_token(str(u["_id"]))
    r = await client.post("/notifications/garbage/read", headers=_auth(tok))
    assert r.status_code == 400, r.text


async def test_message_requires_text_length_limit(app, db, client):
    u = _user_doc("0767000012")
    other = _user_doc("0767000013")
    await db.users.insert_many([u, other])
    tok = create_access_token(str(u["_id"]))
    # Empty text → 422 from pydantic (min_length=1)
    r = await client.post("/messages", headers=_auth(tok),
                          json={"to_user_id": str(other["_id"]), "text": ""})
    assert r.status_code == 422, r.text
    # Over-long text → 422 from pydantic (max_length=2000)
    r = await client.post("/messages", headers=_auth(tok),
                          json={"to_user_id": str(other["_id"]), "text": "x" * 2001})
    assert r.status_code == 422, r.text


async def test_chat_history_cannot_read_other_conversation(app, db, client):
    """chat_history only returns messages between the two participants."""
    a = _user_doc("0767000014")
    b = _user_doc("0767000015")
    c = _user_doc("0767000016")
    await db.users.insert_many([a, b, c])
    now = datetime.now(timezone.utc)
    conv = "_".join(sorted([str(a["_id"]), str(b["_id"])]))
    await db.messages.insert_one({"conversation_id": conv, "from_user_id": str(a["_id"]),
                                  "to_user_id": str(b["_id"]), "text": "siri kati ya a-b",
                                  "is_read": False, "created_at": now})
    # c cannot see a↔b conversation even with crafted conv id
    c_tok = create_access_token(str(c["_id"]))
    r = await client.get(f"/messages/with/{a['_id']}", headers=_auth(c_tok))
    assert r.status_code == 200
    assert all(m["text"] != "siri kati ya a-b" for m in r.json()["messages"])


async def test_untrusted_xff_header_does_not_bypass_rate_limit(app, db, client):
    """Spoofed X-Forwarded-For must NOT grant a fresh bucket (trust disabled)."""
    from app import security as sec
    sec.clear_attempts("testclient")
    u = _user_doc("0767000050")
    await db.users.insert_one(u)
    limit = settings.rate_limit_max
    last = None
    # Rotate the XFF header each attempt — must still hit 429 because the
    # header is ignored and the real client host bucket is shared.
    for i in range(limit + 2):
        last = await client.post("/auth/login",
                                 json={"phone": "0767000050", "password": "wrong"},
                                 headers={"x-forwarded-for": f"10.0.0.{i}"})
    assert last.status_code == 429, f"expected 429, got {last.status_code}"


# ─── Rate limiting (brute-force protection) ──────────────────────────────

async def test_login_rate_limited(app, db, client, monkeypatch):
    from app import security as sec
    # Simulate being behind a trusted proxy so the XFF header is honored.
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    sec.clear_attempts("login:+255767000020:1.2.3.4")
    u = _user_doc("0767000020")
    await db.users.insert_one(u)
    limit = settings.rate_limit_max
    last = None
    # (limit + 2) requests: max allowed should be `limit`; request limit+1 → 429
    for i in range(limit + 2):
        last = await client.post("/auth/login",
                                 json={"phone": "0767000020", "password": "wrong"},
                                 headers={"x-forwarded-for": "1.2.3.4"})
    assert last.status_code == 429, f"expected 429 after {limit + 2} attempts, got {last.status_code}"


async def test_register_rate_limited(app, db, client, monkeypatch):
    from app import security as sec
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    sec.clear_attempts("register:5.6.7.8")
    await _seed_cadres(db)
    limit = settings.rate_limit_max
    last = None
    for i in range(limit + 2):
        last = await client.post("/auth/register", json=await _register_body(f"0767{i:06d}"),
                                 headers={"x-forwarded-for": "5.6.7.8"})
    assert last.status_code == 429


# ─── PII safety ───────────────────────────────────────────────────────────

async def test_user_payload_never_leaks_password_hash(app, db, client):
    u = _user_doc("0767000030")
    await db.users.insert_one(u)
    tok = create_access_token(str(u["_id"]))
    r = await client.get("/auth/me", headers=_auth(tok))
    assert "password_hash" not in r.text
    r = await client.get("/users/recent", headers=_auth(tok))
    assert "password_hash" not in r.text


# ─── Phone normalization / validation ─────────────────────────────────────

async def test_invalid_phone_rejected_on_login(app, client):
    r = await client.post("/auth/login", json={"phone": "abc", "password": "x"})
    assert r.status_code == 422


async def test_register_duplicate_phone_conflict(app, db, client, captured_events):
    await _seed_cadres(db)
    u = _user_doc("0767000040")
    await db.users.insert_one(u)
    r = await client.post("/auth/register", json=await _register_body("0767000040"))
    assert r.status_code == 409
