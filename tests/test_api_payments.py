"""Manual donation verification flow: donate → verifying → approved/rejected.

Module-level fixtures shadow conftest ones and wire the auth, users, admin and
payments routers to the in-memory DB, capturing every publish() call.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from bson import ObjectId
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

import app.modules.auth.routes as auth_routes
import app.modules.users.routes as users_routes
import app.modules.admin.routes as admin_routes
import app.modules.payments.routes as pay_routes
import app.security as security

from app.modules.auth.routes import router as auth_router
from app.modules.users.routes import router as users_router
from app.modules.admin.routes import router as admin_router
from app.modules.payments.routes import router as pay_router
from app.events.topics import TOPIC_PAYMENT_SUBMITTED, TOPIC_PAYMENT_APPROVED, TOPIC_PAYMENT_REJECTED
from app.security import create_access_token, hash_password

_ROUTERS = (auth_router, users_router, admin_router, pay_router)
_MODULES = (auth_routes, users_routes, admin_routes, pay_routes)

SMS = "Confirmed. You have received TZS 5,000.00 from JOHN KAMWENDA 0712345678 on 08/08/2026 at 10:30. Ref: C2H8MZ3JX1"


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
    events: list[tuple[str, dict]] = []

    def fake(topic, payload, qos=1):
        events.append((topic, payload))

    for mod in _MODULES:
        monkeypatch.setattr(mod, "publish", fake)
    return events


def _user_doc(phone: str, *, is_admin: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    doc = {
        "_id": ObjectId(),
        "full_name": "Mchangiaji Test",
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


async def test_donation_info_returns_admin_phone(app, db, client, monkeypatch):
    u = _user_doc("0712000001")
    await db.users.insert_one(u)
    token = create_access_token(str(u["_id"]))

    # donation_phone comes from settings — patch it for a deterministic assertion
    monkeypatch.setattr(pay_routes, "settings",
                        SimpleNamespace(donation_phone="0755123456", payment_currency="TZS"))
    res = await client.get("/payments/info", headers=_auth(token))
    assert res.status_code == 200
    assert res.json()["phone"] == "0755123456"


async def test_donate_creates_verifying_donation_and_publishes(app, db, client, captured_events):
    u = _user_doc("0712000002")
    await db.users.insert_one(u)
    token = create_access_token(str(u["_id"]))

    res = await client.post("/payments/donate", headers=_auth(token), json={
        "amount": 5000, "phone": "0712000002", "sms_text": SMS,
    })
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "verifying"
    assert body["amount"] == 5000

    doc = await db.payments.find_one({"_id": body["order_id"]})
    assert doc["user_id"] == str(u["_id"])
    assert doc["sms_text"] == SMS

    topics = [t for t, _ in captured_events]
    assert any(t.startswith(TOPIC_PAYMENT_SUBMITTED) for t in topics)


async def test_admin_approve_flips_status_and_publishes(app, db, client, captured_events):
    donor = _user_doc("0712000003")
    admin = _user_doc("0712000004", is_admin=True)
    await db.users.insert_one(donor)
    await db.users.insert_one(admin)
    donor_token = create_access_token(str(donor["_id"]))
    admin_token = create_access_token(str(admin["_id"]))

    order_id = (await client.post("/payments/donate", headers=_auth(donor_token),
                                  json={"amount": 10000, "sms_text": SMS})).json()["order_id"]

    res = await client.post(f"/payments/admin/{order_id}/approve", headers=_auth(admin_token), json={"note": "Imethibitishwa"})
    assert res.status_code == 200
    assert res.json()["status"] == "approved"

    doc = await db.payments.find_one({"_id": order_id})
    assert doc["status"] == "approved"
    assert doc["note"] == "Imethibitishwa"

    # donor sees approved status
    st = await client.get(f"/payments/status/{order_id}", headers=_auth(donor_token))
    assert st.json()["status"] == "approved"

    topics = [t for t, _ in captured_events]
    assert any(t.startswith(TOPIC_PAYMENT_APPROVED) for t in topics)
    assert all(not t.startswith(TOPIC_PAYMENT_REJECTED) for t in topics)


async def test_admin_reject_flips_status_and_publishes(app, db, client, captured_events):
    donor = _user_doc("0712000005")
    admin = _user_doc("0712000006", is_admin=True)
    await db.users.insert_one(donor)
    await db.users.insert_one(admin)
    donor_token = create_access_token(str(donor["_id"]))
    admin_token = create_access_token(str(admin["_id"]))

    order_id = (await client.post("/payments/donate", headers=_auth(donor_token),
                                  json={"amount": 5000, "sms_text": SMS})).json()["order_id"]

    res = await client.post(f"/payments/admin/{order_id}/reject", headers=_auth(admin_token), json={"note": "Hakuna malipo"})
    assert res.status_code == 200
    assert res.json()["status"] == "rejected"

    st = await client.get(f"/payments/status/{order_id}", headers=_auth(donor_token))
    assert st.json()["status"] == "rejected"
    assert st.json()["note"] == "Hakuna malipo"

    topics = [t for t, _ in captured_events]
    assert any(t.startswith(TOPIC_PAYMENT_REJECTED) for t in topics)


async def test_approve_only_works_once(app, db, client):
    donor = _user_doc("0712000007")
    admin = _user_doc("0712000008", is_admin=True)
    await db.users.insert_one(donor)
    await db.users.insert_one(admin)
    donor_token = create_access_token(str(donor["_id"]))
    admin_token = create_access_token(str(admin["_id"]))

    order_id = (await client.post("/payments/donate", headers=_auth(donor_token),
                                  json={"amount": 5000, "sms_text": SMS})).json()["order_id"]

    first = await client.post(f"/payments/admin/{order_id}/approve", headers=_auth(admin_token))
    second = await client.post(f"/payments/admin/{order_id}/approve", headers=_auth(admin_token))
    assert first.status_code == 200
    assert second.status_code == 400  # no longer verifying


async def test_non_admin_cannot_approve(app, db, client):
    donor = _user_doc("0712000009")
    await db.users.insert_one(donor)
    donor_token = create_access_token(str(donor["_id"]))

    order_id = (await client.post("/payments/donate", headers=_auth(donor_token),
                                  json={"amount": 5000, "sms_text": SMS})).json()["order_id"]

    res = await client.post(f"/payments/admin/{order_id}/approve", headers=_auth(donor_token))
    assert res.status_code == 403


async def test_admin_all_lists_donations_and_total(app, db, client):
    donor = _user_doc("0712000010")
    admin = _user_doc("0712000011", is_admin=True)
    await db.users.insert_one(donor)
    await db.users.insert_one(admin)
    donor_token = create_access_token(str(donor["_id"]))
    admin_token = create_access_token(str(admin["_id"]))

    o1 = (await client.post("/payments/donate", headers=_auth(donor_token),
                            json={"amount": 5000, "sms_text": SMS})).json()["order_id"]
    await client.post("/payments/donate", headers=_auth(donor_token),
                      json={"amount": 7000, "sms_text": SMS})
    await client.post(f"/payments/admin/{o1}/approve", headers=_auth(admin_token))

    res = await client.get("/payments/admin/all", headers=_auth(admin_token))
    body = res.json()
    assert body["count"] == 2
    assert body["total_approved_tzs"] == 5000

    pending = await client.get("/payments/admin/all?status=verifying", headers=_auth(admin_token))
    assert pending.json()["count"] == 1
