"""Tests for admin announcements (matangazo): send to all/category/user, active, dismiss."""
from datetime import datetime, timezone
from unittest.mock import patch
from bson import ObjectId
import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

import app.modules.auth.routes as auth_routes
import app.modules.admin.routes as admin_routes
import app.modules.announcements.routes as ann_routes
import app.security as security

from app.modules.auth.routes import router as auth_router
from app.modules.admin.routes import router as admin_router
from app.modules.announcements.routes import router as ann_router
from app.security import create_access_token, hash_password

pytestmark = pytest.mark.asyncio

_ROUTERS = (auth_router, admin_router, ann_router)
_DB_MODULES = (auth_routes, admin_routes, ann_routes)
_PUB_MODULES = (auth_routes, admin_routes, ann_routes)


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

    for mod in _PUB_MODULES:
        monkeypatch.setattr(mod, "publish", fake)
    return events


def _user_doc(phone: str, *, category: str = "health", is_admin: bool = False) -> dict:
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
        "desired_destinations": [], "status": "active", "is_verified": False,
        "is_admin": is_admin,
        "email": f"admin{phone[3:]}@test.go.tz" if is_admin else None,
        "email_verified": is_admin,
        "created_at": now, "updated_at": now,
    }


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_announcement_flow_all_audience(app, db, client, captured_events):
    donor = _user_doc("0767000001")
    teacher = _user_doc("0767000002", category="education")
    await db.users.insert_many([donor, teacher])
    await db.users.update_one({"_id": donor["_id"]}, {"$set": {"is_admin": True,
                                                              "email": f"admin{donor['_id']}@test.go.tz",
                                                              "email_verified": True}})
    admin_token = create_access_token(str(donor["_id"]))

    r = await client.post("/admin/announcements",
                          json={"title": "Karibu wote", "message": "Tangazo la jumla", "audience": "all"},
                          headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sent_to"] == 2
    # One MQTT event per recipient
    topics = [t for t, _ in captured_events]
    assert f"kv/announcement/{donor['_id']}" in topics
    assert f"kv/announcement/{teacher['_id']}" in topics

    # Both see it active + unread badge
    for u in (donor, teacher):
        tok = create_access_token(str(u["_id"]))
        r = await client.get("/announcements/active", headers=_auth(tok))
        assert r.status_code == 200
        assert any(a["title"] == "Karibu wote" for a in r.json()["announcements"])
        r = await client.get("/announcements/unread-count", headers=_auth(tok))
        assert r.json()["unread"] == 1

    # Teacher dismisses → gone for him, stays for donor
    teacher_tok = create_access_token(str(teacher["_id"]))
    r = await client.get("/announcements/active", headers=_auth(teacher_tok))
    ann_id = r.json()["announcements"][0]["announcement_id"]
    r = await client.post(f"/announcements/{ann_id}/dismiss", headers=_auth(teacher_tok))
    assert r.status_code == 200, r.text

    r = await client.get("/announcements/active", headers=_auth(teacher_tok))
    assert all(a["title"] != "Karibu wote" for a in r.json()["announcements"])
    r = await client.get("/announcements/unread-count", headers=_auth(teacher_tok))
    assert r.json()["unread"] == 0

    donor_tok = create_access_token(str(donor["_id"]))
    r = await client.get("/announcements/active", headers=_auth(donor_tok))
    assert any(a["title"] == "Karibu wote" for a in r.json()["announcements"])


async def test_announcement_targeted_category(app, db, client):
    donor = _user_doc("0767000003")
    teacher = _user_doc("0767000004", category="education")
    await db.users.insert_many([donor, teacher])
    await db.users.update_one({"_id": donor["_id"]}, {"$set": {"is_admin": True,
                                                              "email": f"admin{donor['_id']}@test.go.tz",
                                                              "email_verified": True}})
    admin_token = create_access_token(str(donor["_id"]))

    with patch("app.modules.announcements.routes.publish"):
        r = await client.post("/admin/announcements",
                              json={"title": "Kwa Walimu", "message": "Mkutano wa walimu",
                                    "audience": "education"},
                              headers=_auth(admin_token))
        assert r.status_code == 200, r.text
        assert r.json()["sent_to"] == 1

    teacher_tok = create_access_token(str(teacher["_id"]))
    r = await client.get("/announcements/active", headers=_auth(teacher_tok))
    assert any(a["title"] == "Kwa Walimu" for a in r.json()["announcements"])

    # Health user does NOT receive education-only tangazo
    health_tok = create_access_token(str(donor["_id"]))
    r = await client.get("/announcements/active", headers=_auth(health_tok))
    assert all(a["title"] != "Kwa Walimu" for a in r.json()["announcements"])


async def test_announcement_single_user_and_guard(app, db, client):
    admin = _user_doc("0767000005", is_admin=True)
    target = _user_doc("0767000006")
    await db.users.insert_many([admin, target])
    admin_tok = create_access_token(str(admin["_id"]))

    with patch("app.modules.announcements.routes.publish"):
        r = await client.post("/admin/announcements",
                              json={"title": "Kwako tu", "message": "Ujumbe maalum",
                                    "audience": "user", "target_user_id": str(target["_id"])},
                              headers=_auth(admin_tok))
        assert r.status_code == 200, r.text
        assert r.json()["sent_to"] == 1

    # Non-admin cannot send
    target_tok = create_access_token(str(target["_id"]))
    r = await client.post("/admin/announcements",
                          json={"title": "Hack", "message": "siwezi", "audience": "all"},
                          headers=_auth(target_tok))
    assert r.status_code == 403

    # target_user_id required for audience=user
    r = await client.post("/admin/announcements",
                          json={"title": "Kosa", "message": "bila mtu", "audience": "user"},
                          headers=_auth(admin_tok))
    assert r.status_code == 400


async def test_subscriber_notifies_announcement_recipient(monkeypatch):
    """kv/announcement/{uid} → notification kwa mtu aliyelengwa."""
    import json
    import mongomock
    import app.events.subscriber as sub

    db = mongomock.MongoClient()["kv_test"]
    monkeypatch.setattr(sub, "get_sync_db", lambda: db)
    monkeypatch.setattr(sub, "_append_csv", lambda row: None)

    # Browser listens on the authenticated WebSocket — capture WS pushes.
    ws_batch: list[tuple[dict, str]] = []
    monkeypatch.setattr(sub, "_push_batch_to_users", lambda batch: ws_batch.extend(batch))

    class _FakeMsg:
        topic = "kv/announcement/abc123"
        payload = json.dumps({"event": "announcement.new", "announcement_id": "x1",
                              "title": "Sherehe 🎉", "message": "Njoo siku ya ijumaa",
                              "user_id": "abc123"}).encode()

    class _FakeClient:
        def __init__(self): self.published = []
        def publish(self, topic, payload, qos=1): self.published.append((topic, payload))

    client = _FakeClient()
    sub._generate_notifications(_FakeMsg(), client)

    notifs = list(db.notifications.find({}))
    assert len(notifs) == 1
    assert notifs[0]["user_id"] == "abc123"
    assert notifs[0]["type"] == "announcement"
    assert "Sherehe" in notifs[0]["title"]
    # Notification delivered via authenticated WS to the recipient
    assert any(uid == "abc123" for _, uid in ws_batch)
    assert any(p.get("event") == "notification" for p, uid in ws_batch if uid == "abc123")
