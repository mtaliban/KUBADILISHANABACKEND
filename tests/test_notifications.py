"""Notifications center, contact-stats and read receipts.

Subscriber unit tests use a sync in-memory Mongo + a fake MQTT client; API tests
use the standard module-fixture pattern with captured publishes.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
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
import app.modules.notifications.routes as notif_routes
import app.security as security

from app.modules.auth.routes import router as auth_router
from app.modules.users.routes import router as users_router
from app.modules.admin.routes import router as admin_router
from app.modules.messaging.routes import router as msg_router
from app.modules.notifications.routes import router as notif_router
from app.events.topics import (
    TOPIC_MATCH_FOUND, TOPIC_MESSAGE_SENT, TOPIC_CALL_INITIATED,
    TOPIC_PAYMENT_SUBMITTED, TOPIC_PAYMENT_APPROVED, TOPIC_USER_PROFILE_UPDATED,
)
from app.security import create_access_token, hash_password

_ROUTERS = (auth_router, users_router, admin_router, msg_router, notif_router)
# Modules whose get_db is patched (all of them) — but only modules that publish
# MQTT events get the publish stub (notifications routes never publish).
_DB_MODULES = (auth_routes, users_routes, admin_routes, msg_routes, notif_routes)
_PUB_MODULES = (auth_routes, users_routes, admin_routes, msg_routes)


# ─── API fixtures ───────────────────────────────────────────────────

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


def _user_doc(phone: str, *, is_admin: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    doc = {
        "_id": ObjectId(),
        "full_name": "Test Mtu",
        "phone_primary": f"+255{phone[1:]}",
        "password_hash": hash_password("secret123"),
        "category": "health", "cadre_code": "CO", "cadre_display": "Clinical Officer",
        "subjects": [],
        "current_station": {"region_id": 17, "region_name": "Mwanza", "district_id": 1701,
                            "district_name": "Nyamagana Dc", "facility_id": None, "facility_name": None},
        "desired_destinations": [],
        "status": "active", "is_verified": False, "is_admin": is_admin,
        "email": f"admin{phone[3:]}@test.go.tz" if is_admin else None,
        "email_verified": is_admin,
        "created_at": now, "updated_at": now,
    }
    return doc


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ─── Subscriber: notification generation ────────────────────────────

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


def _sub_env(monkeypatch):
    db = mongomock.MongoClient()["kv_test"]
    monkeypatch.setattr(sub, "get_sync_db", lambda: db)
    monkeypatch.setattr(sub, "_append_csv", lambda row: None)
    monkeypatch.setattr(sub, "_push_batch_to_users", lambda batch: None)
    return db


def _make_user(phone):
    now = datetime.now(timezone.utc)
    return {
        "_id": ObjectId(), "full_name": f"Mtu {phone}", "phone_primary": phone,
        "category": "health", "cadre_code": "CO", "status": "active",
        "created_at": now, "updated_at": now,
    }


def test_subscriber_notifies_both_users_on_match_found(monkeypatch):
    db = _sub_env(monkeypatch)
    a, b = _make_user("+255711000001"), _make_user("+255711000002")
    db.users.insert_many([a, b])
    client = _FakeClient()

    sub._generate_notifications(_FakeMsg(TOPIC_MATCH_FOUND, {
        "event": "match.found", "user_a_id": str(a["_id"]), "user_b_id": str(b["_id"]),
        "score": 0.85, "occurred_at": datetime.now(timezone.utc).isoformat(),
    }), client)

    notifs = list(db.notifications.find({}))
    assert len(notifs) == 2
    assert {n["user_id"] for n in notifs} == {str(a["_id"]), str(b["_id"])}
    assert all(n["type"] == "match.found" for n in notifs)
    # MQTT notification events emitted per user
    topics = [t for t, _, _ in client.published]
    assert f"kv/notification/{a['_id']}" in topics
    assert f"kv/notification/{b['_id']}" in topics


def test_subscriber_notifies_admins_on_payment_submitted(monkeypatch):
    db = _sub_env(monkeypatch)
    admin = _make_user("+255711000003")
    admin["is_admin"] = True
    db.users.insert_one(admin)
    client = _FakeClient()

    sub._generate_notifications(_FakeMsg(f"{TOPIC_PAYMENT_SUBMITTED}/owner123", {
        "event": "payment.submitted", "order_id": "kv_1", "amount": 5000,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }), client)

    notifs = list(db.notifications.find({}))
    assert len(notifs) == 1
    assert notifs[0]["user_id"] == str(admin["_id"])
    assert notifs[0]["type"] == "payment.submitted"


def test_subscriber_notifies_recipient_on_message_and_call(monkeypatch):
    db = _sub_env(monkeypatch)
    me = _make_user("+255711000004")
    db.users.insert_one(me)
    client = _FakeClient()

    sub._generate_notifications(_FakeMsg(f"{TOPIC_MESSAGE_SENT}/{me['_id']}", {
        "event": "message.sent", "from_user_id": "x", "from_full_name": "Juma",
        "to_user_id": str(me["_id"]), "text": "Habari!", "created_at": datetime.now(timezone.utc).isoformat(),
    }), client)
    sub._generate_notifications(_FakeMsg(f"{TOPIC_CALL_INITIATED}/{me['_id']}", {
        "event": "call.initiated", "from_user_id": "x", "from_full_name": "Juma",
        "to_user_id": str(me["_id"]), "initiated_at": datetime.now(timezone.utc).isoformat(),
    }), client)

    types = sorted(n["type"] for n in db.notifications.find({}))
    assert types == ["call.initiated", "message.sent"]
    assert all(n["user_id"] == str(me["_id"]) for n in db.notifications.find({}))


def test_subscriber_notifies_matches_on_profile_update(monkeypatch):
    db = _sub_env(monkeypatch)
    target = _make_user("+255711000005")
    partner = _make_user("+255711000006")
    db.users.insert_many([target, partner])
    db.matches.insert_one({"user_a_id": str(target["_id"]), "user_b_id": str(partner["_id"]),
                           "score": 0.85, "status": "new"})
    client = _FakeClient()

    sub._generate_notifications(_FakeMsg(TOPIC_USER_PROFILE_UPDATED, {
        "event": "user.profile_updated", "user_id": str(target["_id"]),
        "changed_fields": ["full_name"], "occurred_at": datetime.now(timezone.utc).isoformat(),
    }), client)

    notifs = list(db.notifications.find({}))
    assert len(notifs) == 1
    assert notifs[0]["user_id"] == str(partner["_id"])
    assert notifs[0]["type"] == "user.profile_updated"


# ─── Notifications API ──────────────────────────────────────────────

async def test_notifications_api_list_unread_read_flow(app, db, client, captured_events):
    u = _user_doc("0713000001")
    await db.users.insert_one(u)
    token = create_access_token(str(u["_id"]))
    h = _auth(token)

    for i in range(3):
        await db.notifications.insert_one({
            "user_id": str(u["_id"]), "type": "message.sent", "title": f"Ujumbe {i}",
            "body": "Hello", "data": {}, "read": False,
            "created_at": datetime.now(timezone.utc) - timedelta(minutes=i),
        })

    count = await client.get("/notifications/unread-count", headers=h)
    assert count.json()["unread"] == 3

    data = await client.get("/notifications", headers=h)
    body = data.json()
    assert body["total"] == 3
    assert len(body["notifications"]) == 3

    first_id = body["notifications"][0]["notification_id"]
    r = await client.post(f"/notifications/{first_id}/read", headers=h)
    assert r.status_code == 200
    count = await client.get("/notifications/unread-count", headers=h)
    assert count.json()["unread"] == 2

    r = await client.post("/notifications/read-all", headers=h)
    assert r.status_code == 200
    count = await client.get("/notifications/unread-count", headers=h)
    assert count.json()["unread"] == 0


# ─── Contact stats ──────────────────────────────────────────────────

async def test_contact_stats_counts_incoming_and_outgoing(app, db, client):
    me = _user_doc("0713000002")
    caller = _user_doc("0713000003")
    messager = _user_doc("0713000004")
    await db.users.insert_many([me, caller, messager])
    token = create_access_token(str(me["_id"]))
    h = _auth(token)

    now = datetime.now(timezone.utc)
    # 2 incoming calls from caller, 1 outgoing call to messager
    await db.call_logs.insert_many([
        {"from_user_id": str(caller["_id"]), "to_user_id": str(me["_id"]), "initiated_at": now, "status": "initiated"},
        {"from_user_id": str(caller["_id"]), "to_user_id": str(me["_id"]), "initiated_at": now, "status": "initiated"},
        {"from_user_id": str(me["_id"]), "to_user_id": str(messager["_id"]), "initiated_at": now, "status": "initiated"},
    ])
    # 1 incoming message from messager, 1 outgoing to caller
    await db.messages.insert_many([
        {"conversation_id": "c1", "from_user_id": str(messager["_id"]), "to_user_id": str(me["_id"]),
         "text": "Habari", "created_at": now},
        {"conversation_id": "c2", "from_user_id": str(me["_id"]), "to_user_id": str(caller["_id"]),
         "text": "Mambo", "created_at": now},
    ])

    res = await client.get("/messages/contact-stats", headers=h)
    assert res.status_code == 200
    s = res.json()
    assert s["incoming_calls"]["count"] == 1
    assert s["incoming_calls"]["people"][0]["user_id"] == str(caller["_id"])
    assert s["incoming_calls"]["people"][0]["count"] == 2
    assert s["outgoing_calls"]["count"] == 1
    assert s["outgoing_calls"]["people"][0]["user_id"] == str(messager["_id"])
    assert s["incoming_messages"]["count"] == 1
    assert s["incoming_messages"]["people"][0]["user_id"] == str(messager["_id"])
    assert s["outgoing_messages"]["count"] == 1
    assert s["outgoing_messages"]["people"][0]["user_id"] == str(caller["_id"])


# ─── Read receipts ──────────────────────────────────────────────────

async def test_chat_history_marks_read_and_returns_receipts(app, db, client):
    me = _user_doc("0713000005")
    other = _user_doc("0713000006")
    await db.users.insert_many([me, other])
    token = create_access_token(str(me["_id"]))
    h = _auth(token)
    conv = "_".join(sorted([str(me["_id"]), str(other["_id"])]))
    now = datetime.now(timezone.utc)
    await db.messages.insert_one({
        "conversation_id": conv, "from_user_id": str(other["_id"]), "to_user_id": str(me["_id"]),
        "text": "Habari", "is_read": False, "delivered_at": now, "created_at": now,
    })

    res = await client.get(f"/messages/with/{other['_id']}", headers=h)
    assert res.status_code == 200
    msg = res.json()["messages"][0]
    assert msg["is_read"] is True
    assert msg["delivered_at"] is not None
    assert msg["read_at"] is not None

    # persisted
    fresh = await db.messages.find_one({"conversation_id": conv})
    assert fresh["is_read"] is True
