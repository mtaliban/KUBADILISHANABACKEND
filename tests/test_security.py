"""Security tests: auth guards, JWT edge cases, injection resistance, PII safety."""
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

async def test_invalid_announcement_id_400(app, db, client):
    u = _user_doc("0767000011")
    await db.users.insert_one(u)
    tok = create_access_token(str(u["_id"]))
    r = await client.post("/notifications/garbage/read", headers=_auth(tok))
    assert r.status_code == 400, r.text


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


# ─── Sector isolation: Wizara ya Afya ↔ TAMISEMI ────────────────────────

import app.modules.matches.routes as matches_routes
from app.modules.matches.routes import router as matches_router

@pytest.fixture
def app_with_matches(db, monkeypatch):
    """App with matches + admin router for board security tests."""
    application = FastAPI()
    for r in (auth_router, matches_router, admin_router):
        application.include_router(r)
    for mod in (auth_routes, matches_routes, admin_routes):
        monkeypatch.setattr(mod, "get_db", lambda: db)
    monkeypatch.setattr(security, "get_db", lambda: db)
    return application


@pytest.fixture
async def client_with_matches(app_with_matches):
    transport = ASGITransport(app=app_with_matches)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_wizara_afya_cannot_see_tamisemi(app_with_matches, db, client_with_matches):
    """Wizara ya Afya user should NOT see TAMISEMI users on the board."""
    await _seed_cadres(db)
    # Wizara ya Afya user
    viewer = _user_doc("0767000050")
    viewer["employment_sector"] = "wizara_afya"
    viewer["current_station"]["region_id"] = 4  # Dodoma
    await db.users.insert_one(viewer)
    vtok = create_access_token(str(viewer["_id"]))

    # TAMISEMI user who wants to come to Dodoma
    tamisemi = _user_doc("0767000051")
    tamisemi["employment_sector"] = "tamisemi"
    tamisemi["desired_destinations"] = [{"region_id": 4, "region_name": "Dodoma"}]
    await db.users.insert_one(tamisemi)

    r = await client_with_matches.get("/matches/board", headers=_auth(vtok))
    assert r.status_code == 200
    data = r.json()
    names = [c["full_name"] for c in data["candidates"]]
    assert "Mtu 0767000051" not in names, "TAMISEMI user should not appear for Wizara ya Afya viewer"


async def test_wizara_afya_cannot_see_old_users_without_sector(app_with_matches, db, client_with_matches):
    """Wizara ya Afya user should NOT see old users who lack employment_sector."""
    await _seed_cadres(db)
    viewer = _user_doc("0767000056")
    viewer["employment_sector"] = "wizara_afya"
    viewer["current_station"]["region_id"] = 4
    await db.users.insert_one(viewer)
    vtok = create_access_token(str(viewer["_id"]))

    # Old user WITHOUT employment_sector field (simulated — _user_doc has no sector)
    old_user = _user_doc("0767000057")
    old_user["desired_destinations"] = [{"region_id": 4, "region_name": "Dodoma"}]
    await db.users.insert_one(old_user)

    r = await client_with_matches.get("/matches/board", headers=_auth(vtok))
    assert r.status_code == 200
    data = r.json()
    names = [c["full_name"] for c in data["candidates"]]
    assert "Mtu 0767000057" not in names, "Old user without sector should not appear for Wizara ya Afya viewer"


async def test_tamisemi_cannot_see_wizara_afya(app_with_matches, db, client_with_matches):
    """TAMISEMI user should NOT see Wizara ya Afya users on the board."""
    await _seed_cadres(db)
    # TAMISEMI user
    viewer = _user_doc("0767000052")
    viewer["employment_sector"] = "tamisemi"
    viewer["current_station"]["region_id"] = 17  # Mwanza
    await db.users.insert_one(viewer)
    vtok = create_access_token(str(viewer["_id"]))

    # Wizara ya Afya user who wants to come to Mwanza
    wizara = _user_doc("0767000053")
    wizara["employment_sector"] = "wizara_afya"
    wizara["desired_destinations"] = [{"region_id": 17, "region_name": "Mwanza"}]
    await db.users.insert_one(wizara)

    r = await client_with_matches.get("/matches/board", headers=_auth(vtok))
    assert r.status_code == 200
    data = r.json()
    names = [c["full_name"] for c in data["candidates"]]
    assert "Mtu 0767000053" not in names, "Wizara ya Afya user should not appear for TAMISEMI viewer"


async def test_wizara_afya_can_see_other_wizara_afya(app_with_matches, db, client_with_matches):
    """Wizara ya Afya user SHOULD see other Wizara ya Afya users."""
    await _seed_cadres(db)
    viewer = _user_doc("0767000054")
    viewer["employment_sector"] = "wizara_afya"
    viewer["current_station"]["region_id"] = 4
    await db.users.insert_one(viewer)
    vtok = create_access_token(str(viewer["_id"]))

    wizara = _user_doc("0767000055")
    wizara["employment_sector"] = "wizara_afya"
    wizara["desired_destinations"] = [{"region_id": 4, "region_name": "Dodoma"}]
    await db.users.insert_one(wizara)

    r = await client_with_matches.get("/matches/board", headers=_auth(vtok))
    assert r.status_code == 200
    data = r.json()
    names = [c["full_name"] for c in data["candidates"]]
    assert "Mtu 0767000055" in names, "Wizara ya Afya user SHOULD appear for Wizara ya Afya viewer"


# ─── Admin migration endpoint security ───────────────────────────────────

async def test_non_admin_cannot_migrate_default_names(app_with_matches, db, client_with_matches):
    """Non-admin cannot access migrate-default-names endpoint."""
    u = _user_doc("0767000060")
    await db.users.insert_one(u)
    tok = create_access_token(str(u["_id"]))
    r = await client_with_matches.post("/admin/users/migrate-default-names", headers=_auth(tok))
    assert r.status_code == 403, f"Non-admin got {r.status_code} on migrate endpoint"


async def test_unauthenticated_cannot_migrate_default_names(app_with_matches, client_with_matches):
    """Unauthenticated request cannot access migrate-default-names endpoint."""
    r = await client_with_matches.post("/admin/users/migrate-default-names")
    assert r.status_code == 401, f"Unauthenticated got {r.status_code}"


# ─── Payment security ────────────────────────────────────────────────────

async def test_board_card_shows_is_verified(app_with_matches, db, client_with_matches):
    """Board card should include is_verified field for payment status."""
    await _seed_cadres(db)
    viewer = _user_doc("0767000070")
    viewer["current_station"]["region_id"] = 4
    await db.users.insert_one(viewer)
    vtok = create_access_token(str(viewer["_id"]))

    # Verified (paid) user
    paid = _user_doc("0767000071")
    paid["is_verified"] = True
    paid["desired_destinations"] = [{"region_id": 4, "region_name": "Dodoma"}]
    await db.users.insert_one(paid)

    # Unverified user
    unpaid = _user_doc("0767000072")
    unpaid["is_verified"] = False
    unpaid["desired_destinations"] = [{"region_id": 4, "region_name": "Dodoma"}]
    await db.users.insert_one(unpaid)

    r = await client_with_matches.get("/matches/board", headers=_auth(vtok))
    assert r.status_code == 200
    data = r.json()
    for c in data["candidates"]:
        assert "is_verified" in c, "Card must include is_verified field"
    paid_cards = [c for c in data["candidates"] if c["full_name"] == "Mtu 0767000071"]
    unpaid_cards = [c for c in data["candidates"] if c["full_name"] == "Mtu 0767000072"]
    assert len(paid_cards) == 1 and paid_cards[0]["is_verified"] is True
    assert len(unpaid_cards) == 1 and unpaid_cards[0]["is_verified"] is False


# ─── Category isolation: health ↔ education ──────────────────────────────

async def test_health_user_cannot_see_education_on_board(app_with_matches, db, client_with_matches):
    """Health user should NOT see education users on the board."""
    await _seed_cadres(db)
    viewer = _user_doc("0767000080", category="health")
    viewer["current_station"]["region_id"] = 17
    await db.users.insert_one(viewer)
    vtok = create_access_token(str(viewer["_id"]))

    edu = _user_doc("0767000081", category="education")
    edu["desired_destinations"] = [{"region_id": 17, "region_name": "Mwanza"}]
    await db.users.insert_one(edu)

    r = await client_with_matches.get("/matches/board", headers=_auth(vtok))
    assert r.status_code == 200
    data = r.json()
    names = [c["full_name"] for c in data["candidates"]]
    assert "Mtu 0767000081" not in names, "Education user should not appear for health viewer"
