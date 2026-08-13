"""Admin: bulk user actions (select-all delete/suspend/enable), auto-generated
IDs for regions/districts, na data CRUD events (event-driven activities)."""

from datetime import datetime, timezone
from bson import ObjectId
import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

import app.modules.auth.routes as auth_routes
import app.modules.admin.routes as admin_routes
import app.security as security
from app.modules.auth.routes import router as auth_router
from app.modules.admin.routes import router as admin_router
from app.security import create_access_token, hash_password
from app.events import topics as event_topics


@pytest.fixture
def app(db, monkeypatch):
    application = FastAPI()
    application.include_router(auth_router)
    application.include_router(admin_router)
    monkeypatch.setattr(auth_routes, "get_db", lambda: db)
    monkeypatch.setattr(admin_routes, "get_db", lambda: db)
    monkeypatch.setattr(security, "get_db", lambda: db)
    return application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def published(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(admin_routes, "publish", lambda topic, payload, qos=1: events.append((topic, payload)))
    return events


def _user(phone: str, *, is_admin: bool = False, status: str = "active") -> dict:
    now = datetime.now(timezone.utc)
    return {
        "_id": ObjectId(),
        "full_name": f"Mtu {phone}",
        "phone_primary": phone,
        "password_hash": hash_password("secret123"),
        "category": "health",
        "cadre_code": "CO",
        "cadre_display": "Clinical Officer",
        "subjects": [],
        "current_station": {"region_id": 4, "region_name": "Dodoma", "district_id": 25,
                            "district_name": "Dodoma Cc", "facility_id": None, "facility_name": None},
        "desired_destinations": [],
        "status": status,
        "is_verified": False,
        "is_admin": is_admin,
        "email_verified": is_admin,
        "followed_regions": [],
        "created_at": now,
        "updated_at": now,
    }


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seed_admin(db):
    admin = _user("+255711000001", is_admin=True)
    await db.users.insert_one(admin)
    return admin


# ─── Auto-generated IDs (regions / districts) ────────────────────────────

async def test_region_auto_id_and_explicit(app, db, client):
    admin = await _seed_admin(db)
    token = create_access_token(str(admin["_id"]))
    h = _auth(token)

    # Bila id → inajiongezea yenyewe (max+1)
    r1 = await client.post("/admin/data/regions", json={"name": "Mkoa Mpya"}, headers=h)
    assert r1.status_code == 200
    assert r1.json()["region"]["id"] == 1

    r2 = await client.post("/admin/data/regions", json={"name": "Mkoa Mpya 2"}, headers=h)
    assert r2.json()["region"]["id"] == 2

    # id ikiwekwa → inatumiwa (bila kuleta usumbufu)
    r3 = await client.post("/admin/data/regions", json={"id": 99, "name": "Mkoa 99"}, headers=h)
    assert r3.json()["region"]["id"] == 99

    # duplicate id → 409
    r4 = await client.post("/admin/data/regions", json={"id": 99, "name": "Nyingine"}, headers=h)
    assert r4.status_code == 409


async def test_district_auto_id(app, db, client):
    admin = await _seed_admin(db)
    token = create_access_token(str(admin["_id"]))
    h = _auth(token)

    r = await client.post("/admin/data/districts", json={"region_id": 4, "name": "Wilaya Mpya"}, headers=h)
    assert r.status_code == 200
    body = r.json()["district"]
    assert body["id"] == 1  # auto max+1
    assert body["name"] == "Wilaya Mpya"
    assert body["region_id"] == 4


# ─── Data CRUD ina publish events (event-driven activities) ──────────────

async def test_data_crud_publishes_events(app, db, client, published):
    admin = await _seed_admin(db)
    token = create_access_token(str(admin["_id"]))
    h = _auth(token)

    await client.post("/admin/data/subjects", json={"code": "MATT", "name": "Mathematics T", "level": "Secondary"}, headers=h)
    assert any(t == event_topics.TOPIC_DATA_SUBJECTS_CHANGED and p["event"] == "data.subject_added"
               for t, p in published)

    await client.patch("/admin/data/subjects/MATT", json={"code": "MATT", "name": "Maths T", "level": "Secondary"}, headers=h)
    assert any(p.get("event") == "data.subject_updated" for _, p in published)

    await client.delete("/admin/data/subjects/MATT", headers=h)
    assert any(p.get("event") == "data.subject_deleted" for _, p in published)

    await client.post("/admin/data/regions", json={"name": "Mkoa B"}, headers=h)
    assert any(t == event_topics.TOPIC_DATA_REGIONS_CHANGED and p["event"] == "data.region_added"
               for t, p in published)


# ─── Bulk user actions (select-all) ───────────────────────────────────────

async def test_bulk_disable_enable(app, db, client):
    admin = await _seed_admin(db)
    u1 = _user("+255711000002"); u2 = _user("+255711000003"); other_admin = _user("+255711000004", is_admin=True)
    for u in (u1, u2, other_admin):
        await db.users.insert_one(u)
    token = create_access_token(str(admin["_id"]))
    h = _auth(token)

    # Funga wengi mara moja — admins haziguswi
    r = await client.post("/admin/users/bulk",
                          json={"user_ids": [str(u1["_id"]), str(u2["_id"]), str(other_admin["_id"])],
                                "action": "disable"}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["processed"] == 2
    assert body["skipped_admin"] == 1  # admin mwenzake hajafungiwa
    fresh1 = await db.users.find_one({"_id": u1["_id"]})
    fresh_admin = await db.users.find_one({"_id": other_admin["_id"]})
    assert fresh1["status"] == "disabled"
    assert fresh_admin["status"] == "active"

    # Fungua wengi
    r2 = await client.post("/admin/users/bulk",
                           json={"user_ids": [str(u1["_id"]), str(u2["_id"])], "action": "enable"}, headers=h)
    assert r2.json()["processed"] == 2
    assert (await db.users.find_one({"_id": u1["_id"]}))["status"] == "active"


async def test_bulk_delete_cleans_related_data(app, db, client):
    admin = await _seed_admin(db)
    u1 = _user("+255711000002"); u2 = _user("+255711000003")
    for u in (u1, u2):
        await db.users.insert_one(u)
    await db.matches.insert_one({"user_a_id": str(u1["_id"]), "user_b_id": str(u2["_id"]), "score": 1.0, "status": "new"})
    await db.messages.insert_one({"from_user_id": str(u1["_id"]), "to_user_id": str(u2["_id"]), "text": "hujambo"})
    await db.notifications.insert_one({"user_id": str(u1["_id"]), "type": "test"})
    token = create_access_token(str(admin["_id"]))
    h = _auth(token)

    r = await client.post("/admin/users/bulk",
                          json={"user_ids": [str(u1["_id"]), str(u2["_id"])], "action": "delete"}, headers=h)
    assert r.json()["processed"] == 2
    assert await db.users.count_documents({"_id": {"$in": [u1["_id"], u2["_id"]]}}) == 0
    # Data zinazohusiana zimeondolewa
    assert await db.matches.count_documents({}) == 0
    assert await db.messages.count_documents({}) == 0
    assert await db.notifications.count_documents({}) == 0


async def test_bulk_requires_admin(app, db, client):
    admin = await _seed_admin(db)
    user = _user("+255711000002")
    await db.users.insert_one(user)
    token = create_access_token(str(user["_id"]))
    r = await client.post("/admin/users/bulk",
                          json={"user_ids": [str(admin["_id"])], "action": "delete"},
                          headers=_auth(token))
    assert r.status_code == 403


async def test_bulk_invalid_ids(app, db, client):
    admin = await _seed_admin(db)
    token = create_access_token(str(admin["_id"]))
    r = await client.post("/admin/users/bulk", json={"user_ids": ["not-an-id"], "action": "delete"},
                          headers=_auth(token))
    assert r.status_code == 400
