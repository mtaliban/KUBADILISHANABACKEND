"""Usajili kwa idara isiyo na kada (k.m. Mifugo/Kilimo mpaka admin aweke kada).

Regression: idara mpya ilikuwa ISIWEZEKANE kujiunga — backend ilikataa
"Unknown cadre_code" na wizard ilikataza "rudi nyuma uchague idara nyingine".
Sasa mtumiaji anajiunga bila kada (cadre_code="") na bado anapatana na
wenzake wa idara hiyo (matching inalingana kwa idara + kada).
"""

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

import app.modules.auth.routes as auth_routes
import app.security as security
from app.modules.auth.routes import router as auth_router
from app.security import hash_password


@pytest.fixture
def app(db, monkeypatch):
    application = FastAPI()
    application.include_router(auth_router)
    monkeypatch.setattr(auth_routes, "get_db", lambda: db)
    monkeypatch.setattr(security, "get_db", lambda: db)
    return application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def published_events(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(auth_routes, "publish", lambda topic, payload, qos=1: events.append((topic, payload)))
    return events


def _payload(**over) -> dict:
    base = {
        "full_name": "Mfugaji Mkuu",
        "phone_primary": "0712000111",
        "category": "mifugo",
        "cadre_code": "",
        "subjects": [],
        "current_station": {"region_id": 3, "region_name": "Dar es Salaam",
                            "district_id": 30, "district_name": "Ilala"},
        "desired_destinations": [{"region_id": 17, "region_name": "Mwanza"}],
    }
    base.update(over)
    return base


async def test_register_without_cadre_allowed_when_department_has_none(client, db, published_events):
    """Idara 'mifugo' haina kada → usajili unafanikiwa na cadre_code wazi."""
    await db.departments.insert_one({"code": "mifugo", "name": "Mifugo", "status": "active"})

    res = await client.post("/auth/register", json=_payload())
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["category"] == "mifugo"
    assert body["cadre_code"] == ""

    user = await db.users.find_one({"phone_primary": "+255712000111"})
    assert user is not None
    assert user["cadre_code"] == ""
    assert user["cadre_display"] == ""


async def test_register_empty_cadre_rejected_when_department_has_cadres(client, db):
    """Idara yenye kada — cadre_code tupu hairuhusiwi (lazima achague)."""
    await db.cadres.insert_one({"code": "VET", "display_name": "Veterinary Officer",
                                "category": "mifugo", "requires_subjects": False})

    res = await client.post("/auth/register", json=_payload())
    assert res.status_code == 422
    assert "chagua kada" in res.json()["detail"]


async def test_register_with_valid_cadre_still_works(client, db):
    """Kada halisi ikichaguliwa — kila kitu kinaendelea kama kawaida."""
    await db.cadres.insert_one({"code": "VET", "display_name": "Veterinary Officer",
                                "category": "mifugo", "requires_subjects": False})

    res = await client.post("/auth/register", json=_payload(cadre_code="VET"))
    assert res.status_code == 201, res.text
    assert res.json()["cadre_code"] == "VET"


async def test_register_unknown_cadre_still_rejected(client, db):
    """Kada isiypo bado inakataliwa."""
    await db.departments.insert_one({"code": "mifugo", "name": "Mifugo", "status": "active"})

    res = await client.post("/auth/register", json=_payload(cadre_code="HAKUNA"))
    assert res.status_code == 422
    assert "Unknown cadre_code" in res.json()["detail"]
