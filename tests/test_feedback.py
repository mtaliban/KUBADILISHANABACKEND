"""Tests for Maoni na Malalamiko (feedback): user submits, admin lists + replies."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from bson import ObjectId
import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

import app.modules.auth.routes as auth_routes
import app.modules.feedback.routes as fb_routes
import app.security as security

from app.modules.auth.routes import router as auth_router
from app.modules.feedback.routes import router as fb_router
from app.security import create_access_token, hash_password

pytestmark = pytest.mark.asyncio

_ROUTERS = (auth_router, fb_router)
_DB_MODULES = (auth_routes, fb_routes)


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


def _mk_user(*, name="Mtu A", is_admin=False, email=None, phone="+255712345678"):
    now = datetime.now(timezone.utc)
    return {
        "_id": ObjectId(),
        "full_name": name,
        "phone_primary": phone,
        "email": email,
        "password_hash": hash_password("secret123"),
        "is_admin": is_admin,
        "status": "active",
        "category": "education" if not is_admin else None,
        "created_at": now,
    }


@pytest.fixture
async def seeded(db):
    admin = _mk_user(name="Admin Mkuu", is_admin=True, email="admin@kv.go.tz", phone="+255700000001")
    admin["email_verified"] = True
    user = _mk_user(name="Mwalimu Juma")
    await db.users.insert_many([admin, user])
    return {"admin": admin, "user": user}


def _auth(uid: str):
    return {"Authorization": f"Bearer {create_access_token(uid)}"}


async def test_user_submits_feedback_and_admin_replies(client, db, seeded):
    user, admin = seeded["user"], seeded["admin"]

    # Mtumiaji anatuma maoni
    r = await client.post("/feedback",
                          json={"subject": "Tatizo la login", "message": "Siwezi kuingia tangu jana"},
                          headers=_auth(str(user["_id"])))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "open"
    fid = body["id"]

    # Maoni yamehifadhiwa kwenye DB
    stored = await db.feedback.find_one({"_id": ObjectId(fid)})
    assert stored["user_id"] == str(user["_id"])

    # Admin anaona maoni yote
    r = await client.get("/feedback/admin/all", headers=_auth(str(admin["_id"])))
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == fid
    assert r.json()["counts"]["open"] == 1

    # Admin anamjibu
    r = await client.post(f"/feedback/admin/{fid}/reply",
                          json={"reply": "Asante, tumerekebisha."},
                          headers=_auth(str(admin["_id"])))
    assert r.status_code == 200, r.text

    # Mtumiaji anaona jibu
    r = await client.get("/feedback/my", headers=_auth(str(user["_id"])))
    assert r.status_code == 200
    my = r.json()["items"][0]
    assert my["status"] == "replied"
    assert my["admin_reply"] == "Asante, tumerekebisha."


async def test_admin_requires_admin_role(client, db, seeded):
    user = seeded["user"]
    r = await client.get("/feedback/admin/all", headers=_auth(str(user["_id"])))
    assert r.status_code in (401, 403)


async def test_user_sees_only_own_feedback(client, db, seeded):
    u1, admin = seeded["user"], seeded["admin"]
    u2 = _mk_user(name="Mwalimu Neema", phone="+255712345679")
    await db.users.insert_one(u2)

    await client.post("/feedback", json={"subject": "S1", "message": "Kitu cha kwanza"},
                      headers=_auth(str(u1["_id"])))
    await client.post("/feedback", json={"subject": "S2", "message": "Kitu cha pili"},
                      headers=_auth(str(u2["_id"])))

    r = await client.get("/feedback/my", headers=_auth(str(u1["_id"])))
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["subject"] == "S1"
