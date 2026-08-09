"""Event-driven coverage: every mutation must publish an MQTT event.

Module-level `app`/`client` fixtures shadow conftest ones so we can wire the
auth, users, admin, messaging and payments routers to the in-memory DB and
capture every `publish()` call. `security.get_db` is patched too because
`current_user`/`current_admin` resolve it from the security module namespace.
"""
import json
from datetime import datetime, timedelta, timezone
from bson import ObjectId
import pytest
import mongomock
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

import app.events.subscriber as sub
import app.modules.auth.routes as auth_routes
import app.modules.users.routes as users_routes
import app.modules.admin.routes as admin_routes
import app.modules.messaging.routes as msg_routes
import app.modules.payments.routes as pay_routes
import app.security as security

from app.modules.auth.routes import router as auth_router
from app.modules.users.routes import router as users_router
from app.modules.admin.routes import router as admin_router
from app.modules.messaging.routes import router as msg_router
from app.modules.payments.routes import router as pay_router
from app.events.topics import (
    TOPIC_USER_PASSWORD_RESET_REQUESTED, TOPIC_USER_PASSWORD_RESET_COMPLETED,
    TOPIC_USER_PREFS_UPDATED, TOPIC_USER_UPDATED_BY_ADMIN, TOPIC_USER_DELETED,
    TOPIC_USER_ADMIN_CHANGED, TOPIC_PAGE_VIEWED, TOPIC_MATCH_FOUND,
)
from app.security import create_access_token, hash_password, verify_password

_ROUTERS = (auth_router, users_router, admin_router, msg_router, pay_router)
_MODULES = (auth_routes, users_routes, admin_routes, msg_routes, pay_routes)


@pytest.fixture
def app(db, monkeypatch):
    application = FastAPI()
    for r in _ROUTERS:
        application.include_router(r)
    for mod in _MODULES:
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
    """Capture every publish() across all modules under test."""
    events: list[tuple[str, dict]] = []

    def fake(topic, payload, qos=1):
        events.append((topic, payload))

    for mod in _MODULES:
        monkeypatch.setattr(mod, "publish", fake)
    return events


# ─── helpers ────────────────────────────────────────────────────────

def _user_doc(phone: str, *, is_admin: bool = False, **overrides) -> dict:
    now = datetime.now(timezone.utc)
    doc = {
        "_id": ObjectId(),
        "full_name": "Test Mtumishi",
        "phone_primary": f"+255{phone[1:]}",
        "phone_alt": None,
        "password_hash": hash_password("secret123"),
        "category": "health",
        "cadre_code": "CO",
        "cadre_display": "Clinical Officer",
        "subjects": [],
        "current_station": {"region_id": 17, "region_name": "Mwanza", "district_id": 1701,
                            "district_name": "Nyamagana Dc", "facility_id": None, "facility_name": None},
        "desired_destinations": [{"region_id": 1, "region_name": "Arusha", "district_id": None,
                                  "district_name": None, "facility_id": None, "facility_name": None}],
        "status": "active",
        "is_verified": False,
        "is_admin": is_admin,
        "email": f"admin{phone[3:]}@test.go.tz" if is_admin else None,
        "email_verified": is_admin,
        "notification_prefs": {"new_matches": True, "messages": True},
        "created_at": now,
        "updated_at": now,
    }
    doc.update(overrides)
    return doc


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _swap_user(phone: str, *, in_arusha: bool = False) -> dict:
    """Two users who can swap: one in Mwanza wanting Arusha, one vice-versa."""
    mwanza_station = {"region_id": 17, "region_name": "Mwanza", "district_id": None,
                      "district_name": None, "facility_id": None, "facility_name": None}
    arusha_station = {"region_id": 1, "region_name": "Arusha", "district_id": None,
                      "district_name": None, "facility_id": None, "facility_name": None}
    mwanza_dest = {"region_id": 17, "region_name": "Mwanza", "district_id": None,
                   "district_name": None, "facility_id": None, "facility_name": None}
    arusha_dest = {"region_id": 1, "region_name": "Arusha", "district_id": None,
                   "district_name": None, "facility_id": None, "facility_name": None}
    return {
        "_id": ObjectId(),
        "full_name": "Swap Mtu",
        "phone_primary": phone,
        "category": "health",
        "cadre_code": "CO",
        "cadre_display": "Clinical Officer",
        "subjects": [],
        "current_station": arusha_station if in_arusha else mwanza_station,
        "desired_destinations": [mwanza_dest] if in_arusha else [arusha_dest],
        "status": "active",
    }


# ─── auth: password reset flow ──────────────────────────────────────

async def test_forgot_password_publishes_requested_event(app, db, client, captured_events):
    u = _user_doc("0712345670")
    await db.users.insert_one(u)

    res = await client.post("/auth/forgot-password", json={"phone": "0712345670"})
    assert res.status_code == 200

    ev = [p for t, p in captured_events if t == TOPIC_USER_PASSWORD_RESET_REQUESTED]
    assert len(ev) == 1
    assert ev[0]["user_id"] == str(u["_id"])
    assert "occurred_at" in ev[0]


async def test_reset_password_publishes_completed_event(app, db, client, captured_events):
    u = _user_doc("0712345671")
    await db.users.insert_one(u)
    now = datetime.now(timezone.utc)
    await db.password_resets.insert_one({
        "user_id": u["_id"], "phone": "+255712345671",
        "code_hash": hash_password("123456"),
        "expires_at": now + timedelta(minutes=15),
        "created_at": now, "used": False,
    })

    res = await client.post("/auth/reset-password", json={
        "phone": "0712345671", "code": "123456", "new_password": "brandnew123",
    })
    assert res.status_code == 200

    assert TOPIC_USER_PASSWORD_RESET_COMPLETED in [t for t, _ in captured_events]
    fresh = await db.users.find_one({"_id": u["_id"]})
    assert verify_password("brandnew123", fresh["password_hash"])


# ─── users: notification prefs ──────────────────────────────────────

async def test_update_prefs_publishes_event(app, db, client, captured_events):
    u = _user_doc("0712345672")
    await db.users.insert_one(u)
    token = create_access_token(str(u["_id"]))

    res = await client.put("/users/me/notification-prefs",
                           json={"new_matches": False, "messages": False},
                           headers=_auth(token))
    assert res.status_code == 200

    ev = [p for t, p in captured_events if t == TOPIC_USER_PREFS_UPDATED]
    assert len(ev) == 1
    assert ev[0]["notification_prefs"] == {"new_matches": False, "messages": False}


# ─── admin: user management ─────────────────────────────────────────

async def test_admin_update_user_publishes_event(app, db, client, captured_events):
    admin = _user_doc("0712345673", is_admin=True)
    target = _user_doc("0712345674")
    await db.users.insert_one(admin)
    await db.users.insert_one(target)
    token = create_access_token(str(admin["_id"]))

    new_station = {"region_id": 1, "region_name": "Arusha", "district_id": 101,
                   "district_name": "Arusha Dc", "facility_id": None, "facility_name": None}
    res = await client.patch(f"/admin/users/{target['_id']}",
                             json={"current_station": new_station},
                             headers=_auth(token))
    assert res.status_code == 200

    ev = [p for t, p in captured_events if t == TOPIC_USER_UPDATED_BY_ADMIN]
    assert len(ev) == 1
    assert ev[0]["user_id"] == str(target["_id"])
    assert "current_station" in ev[0]["changed_fields"]


async def test_admin_delete_user_publishes_event(app, db, client, captured_events):
    admin = _user_doc("0712345675", is_admin=True)
    target = _user_doc("0712345676")
    await db.users.insert_one(admin)
    await db.users.insert_one(target)
    token = create_access_token(str(admin["_id"]))

    res = await client.delete(f"/admin/users/{target['_id']}", headers=_auth(token))
    assert res.status_code == 200
    assert await db.users.count_documents({"_id": target["_id"]}) == 0
    assert TOPIC_USER_DELETED in [t for t, _ in captured_events]


async def test_admin_grant_and_revoke_publish_events(app, db, client, captured_events):
    admin = _user_doc("0712345677", is_admin=True)
    target = _user_doc("0712345678")
    await db.users.insert_one(admin)
    await db.users.insert_one(target)
    token = create_access_token(str(admin["_id"]))
    h = _auth(token)

    r1 = await client.post(f"/admin/users/{target['_id']}/grant-admin", headers=h)
    r2 = await client.post(f"/admin/users/{target['_id']}/revoke-admin", headers=h)
    assert r1.status_code == 200 and r2.status_code == 200

    grants = [p for t, p in captured_events if t == TOPIC_USER_ADMIN_CHANGED]
    assert len(grants) == 2
    assert grants[0]["is_admin"] is True and grants[1]["is_admin"] is False


# ─── admin: page views (persist + publish) ──────────────────────────

async def test_page_view_persists_directly_and_publishes_event(app, db, client, captured_events):
    u = _user_doc("0712345679")
    await db.users.insert_one(u)
    token = create_access_token(str(u["_id"]))

    res = await client.post("/admin/page-view",
                            json={"path": "/dashboard", "referrer": "/login"},
                            headers=_auth(token))
    assert res.status_code == 200

    ev = [p for t, p in captured_events if t == TOPIC_PAGE_VIEWED]
    assert len(ev) == 1
    assert ev[0]["path"] == "/dashboard" and ev[0]["user_id"] == str(u["_id"])
    # persisted reliably for reports AND published for the audit stream
    assert await db.page_views.count_documents({"path": "/dashboard"}) == 1


# ─── subscriber: event consumers ────────────────────────────────────

class _FakeMsg:
    def __init__(self, topic: str, payload: dict):
        self.topic = topic
        self.payload = json.dumps(payload, default=str).encode()


class _FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=1):
        self.published.append((topic, payload, qos))
        return self


def test_subscriber_recomputes_matches_on_admin_update(monkeypatch):
    """The admin-update event must trigger match recomputation and drop stale
    matches (bug fix: admin edits silently skipped matching)."""
    db = mongomock.MongoClient()["kv_test"]
    target = _swap_user("+255713000001")
    candidate = _swap_user("+255713000002", in_arusha=True)
    db.users.insert_one(target)
    db.users.insert_one(candidate)
    # A stale match (e.g. target's cadre/category changed since) must be cleaned.
    db.matches.insert_one({"user_a_id": str(target["_id"]), "user_b_id": "old-user",
                           "score": 0.85, "status": "new"})

    monkeypatch.setattr(sub, "get_sync_db", lambda: db)
    monkeypatch.setattr(sub, "_append_csv", lambda row: None)
    client = _FakeClient()

    sub._on_message(client, None, _FakeMsg(TOPIC_USER_UPDATED_BY_ADMIN, {
        "event": "user.updated_by_admin", "user_id": str(target["_id"]),
    }))

    matches = list(db.matches.find({}))
    assert len(matches) == 1  # stale match removed, fresh pair recomputed
    assert matches[0]["user_b_id"] == str(candidate["_id"])
    topics = [t for t, _, _ in client.published]
    assert TOPIC_MATCH_FOUND in topics
