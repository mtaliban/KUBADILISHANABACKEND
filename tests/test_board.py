"""Dashboard board (stats ad-board) + followed-regions (fuata mikoa) tests.

Semantiki: walimu waone stats za elimu pekee, afya waone afya pekee —
USICHANGANYE idara. Lakini ndani ya idara yake, mtumiaji anaona KADA ZOTE
(sio kada moja tu).
"""

from datetime import datetime, timezone
from bson import ObjectId
import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

import app.modules.auth.routes as auth_routes
import app.modules.users.routes as users_routes
import app.modules.matches.routes as matches_routes
import app.security as security

from app.modules.auth.routes import router as auth_router
from app.modules.users.routes import router as users_router
from app.modules.matches.routes import router as matches_router
from app.security import create_access_token, hash_password

_ROUTERS = (auth_router, users_router, matches_router)
_MODULES = (auth_routes, users_routes, matches_routes)


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


def _user(phone: str, *, region_id: int, region_name: str, district_id: int = 0,
          district_name: str = "", cadre: str = "CO", cadre_display: str = "Clinical Officer",
          category: str = "health", dest_region_ids: list[int] | None = None,
          is_admin: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    dests = [{"region_id": rid, "region_name": f"Reg{rid}", "district_id": None,
              "district_name": None, "facility_id": None, "facility_name": None}
             for rid in (dest_region_ids or [])]
    return {
        "_id": ObjectId(),
        "full_name": f"Mtu {phone}",
        "phone_primary": phone,
        "phone_alt": None,
        "password_hash": hash_password("secret123"),
        "category": category,
        "cadre_code": cadre,
        "cadre_display": cadre_display,
        "subjects": [],
        "current_station": {"region_id": region_id, "region_name": region_name,
                            "district_id": district_id, "district_name": district_name,
                            "facility_id": None, "facility_name": None},
        "desired_destinations": dests,
        "status": "active",
        "is_verified": False,
        "is_admin": is_admin,
        "email_verified": is_admin,
        "followed_regions": [],
        "created_at": now,
        "updated_at": now,
    }


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ─── /matches/board ─────────────────────────────────────────────────

async def test_board_incoming_same_category_all_cadres(app, db, client):
    """Idara yako tu, lakini kada ZOTE wanaotaka kuja mkoa wako."""
    me = _user("+255711000001", region_id=4, region_name="Dodoma", district_id=401,
               district_name="Chamwino Dc", dest_region_ids=[1])  # CO afya, nataka Dar
    cand_co = _user("+255711000002", region_id=1, region_name="Dar Es Salaam", district_id=101,
                    district_name="Ilala Mc", dest_region_ids=[4])  # CO Dar → nataka Dodoma
    cand_nurse = _user("+255711000003", region_id=1, region_name="Dar Es Salaam", district_id=102,
                       district_name="Kinondoni Mc", cadre="NO", cadre_display="Nurse Officer",
                       dest_region_ids=[4])  # KADA TOFAUTI (NO) — bado anaonekana!
    cand_mwanza = _user("+255711000004", region_id=17, region_name="Mwanza", district_id=1701,
                        district_name="Nyamagana Dc", dest_region_ids=[4])  # anataka Dodoma pia
    teacher = _user("+255711000005", region_id=1, region_name="Dar Es Salaam", district_id=101,
                    district_name="Ilala Mc", cadre="TEACHER_PRIMARY",
                    cadre_display="Mwalimu wa Elimu ya Msingi", category="education",
                    dest_region_ids=[4])  # Mwalimu — HAIWEZI KUONEKANA kwa mtumiaji wa afya!
    for u in (me, cand_co, cand_nurse, cand_mwanza, teacher):
        await db.users.insert_one(u)
    token = create_access_token(str(me["_id"]))

    res = await client.get("/matches/board", headers=_auth(token))
    assert res.status_code == 200
    body = res.json()
    assert body["scope"] == "incoming"
    # CO + Nurse (kada zote za afya wanaotaka Dodoma) — mwanza pia anataka Dodoma
    assert body["total"] == 3
    ids = {c["user_id"] for c in body["candidates"]}
    assert ids == {str(cand_co["_id"]), str(cand_nurse["_id"]), str(cand_mwanza["_id"])}
    # Hakuna mwalimu (education) kwenye board ya afya
    assert str(teacher["_id"]) not in ids
    # Stats zinagawanya kwa mkoa/district/wilaya ya candidate
    assert {r["region_name"] for r in body["by_region"]} == {"Dar Es Salaam", "Mwanza"}
    assert any(d["district_name"] == "Ilala Mc" for d in body["by_district"])


async def test_board_all_scope_same_category_only(app, db, client):
    """scope=all — watu wa idara yako pekee (sio kuchanganywa na idara nyingine)."""
    me = _user("+255711000001", region_id=4, region_name="Dodoma", dest_region_ids=[1])
    a = _user("+255711000002", region_id=1, region_name="Dar Es Salaam", dest_region_ids=[4])
    b = _user("+255711000003", region_id=17, region_name="Mwanza", dest_region_ids=[4])
    teacher = _user("+255711000004", region_id=4, region_name="Dodoma",
                    cadre="TEACHER_PRIMARY", cadre_display="Mwalimu", category="education",
                    dest_region_ids=[1])
    admin = _user("+255711000005", region_id=4, region_name="Dodoma", is_admin=True)
    for u in (me, a, b, teacher, admin):
        await db.users.insert_one(u)
    token = create_access_token(str(me["_id"]))

    res = await client.get("/matches/board?scope=all", headers=_auth(token))
    assert res.status_code == 200
    body = res.json()
    # Wote wa idara ya AFYA (isipokuwa admin na mimi) — mwalimu HAONEKANI
    assert body["total"] == 2
    assert {c["user_id"] for c in body["candidates"]} == {str(a["_id"]), str(b["_id"])}
    assert body["by_region"]


async def test_board_filters_by_region_district(app, db, client):
    me = _user("+255711000001", region_id=4, region_name="Dodoma", dest_region_ids=[1])
    dar_ilala = _user("+255711000002", region_id=1, region_name="Dar Es Salaam", district_id=101,
                      district_name="Ilala Mc", dest_region_ids=[4])
    dar_kinondoni = _user("+255711000003", region_id=1, region_name="Dar Es Salaam", district_id=102,
                          district_name="Kinondoni Mc", dest_region_ids=[4])
    mwanza = _user("+255711000004", region_id=17, region_name="Mwanza", dest_region_ids=[4])
    for u in (me, dar_ilala, dar_kinondoni, mwanza):
        await db.users.insert_one(u)
    token = create_access_token(str(me["_id"]))

    # Filter kwa mkoa wa Dar → wawili wa Dar
    r = await client.get("/matches/board?region_id=1", headers=_auth(token))
    assert r.json()["total"] == 2

    # Filter kwa wilaya Ilala → mmoja tu
    r2 = await client.get("/matches/board?region_id=1&district_id=101", headers=_auth(token))
    assert r2.json()["total"] == 1
    assert r2.json()["candidates"][0]["user_id"] == str(dar_ilala["_id"])


async def test_board_filters_by_multiple_regions(app, db, client):
    """region_ids (comma-separated) — default ya mtumiaji aliyejiandikisha
    destinations nyingi: anapata wale wanaokuja kwake kutoka MIKOA YOTE
    aliyojiandikisha (sio ya kwanza tu)."""
    me = _user("+255711000001", region_id=4, region_name="Dodoma",
               dest_region_ids=[1, 17])  # nataka kwenda Dar NA Mwanza
    dar = _user("+255711000002", region_id=1, region_name="Dar Es Salaam", dest_region_ids=[4])
    mwanza = _user("+255711000003", region_id=17, region_name="Mwanza", dest_region_ids=[4])
    tabora = _user("+255711000004", region_id=24, region_name="Tabora", dest_region_ids=[4])
    for u in (me, dar, mwanza, tabora):
        await db.users.insert_one(u)
    token = create_access_token(str(me["_id"]))

    # Mikoa miwili (Dar + Mwanza) → wote wawili waonekane, Tabora asionekane
    r = await client.get("/matches/board?region_ids=1,17", headers=_auth(token))
    body = r.json()
    assert body["total"] == 2
    ids = {c["user_id"] for c in body["candidates"]}
    assert ids == {str(dar["_id"]), str(mwanza["_id"])}

    # Badili default → Tabora pekee
    r2 = await client.get("/matches/board?region_ids=24", headers=_auth(token))
    assert r2.json()["total"] == 1
    assert r2.json()["candidates"][0]["user_id"] == str(tabora["_id"])

    # region_id (moja) bado inafanya kazi — backward compatible
    r3 = await client.get("/matches/board?region_id=17", headers=_auth(token))
    assert r3.json()["total"] == 1
    assert r3.json()["candidates"][0]["user_id"] == str(mwanza["_id"])


# ─── /users/me/followed-regions ─────────────────────────────────────

async def test_followed_regions_get_and_put(app, db, client):
    me = _user("+255711000001", region_id=4, region_name="Dodoma", dest_region_ids=[1])
    await db.users.insert_one(me)
    token = create_access_token(str(me["_id"]))
    h = _auth(token)

    # Default → empty (frontend inatumia destinations kama default sources)
    g = await client.get("/users/me/followed-regions", headers=h)
    assert g.status_code == 200
    assert g.json()["region_ids"] == []

    # Weka kuifuata Pwani (region_id=3) pia
    p = await client.put("/users/me/followed-regions", json={"region_ids": [1, 3]}, headers=h)
    assert p.status_code == 200
    assert p.json()["region_ids"] == [1, 3]

    g2 = await client.get("/users/me/followed-regions", headers=h)
    assert g2.json()["region_ids"] == [1, 3]

    # Imehifadhiwa kwenye user doc
    fresh = await db.users.find_one({"_id": me["_id"]})
    assert fresh["followed_regions"] == [1, 3]


async def test_followed_regions_dedupe_and_require_auth(app, db, client):
    me = _user("+255711000001", region_id=4, region_name="Dodoma", dest_region_ids=[1])
    await db.users.insert_one(me)
    token = create_access_token(str(me["_id"]))

    res = await client.put("/users/me/followed-regions", json={"region_ids": [1, 1, 3, 3]},
                           headers=_auth(token))
    assert res.json()["region_ids"] == [1, 3]

    # Bila token → 401
    unauth = await client.get("/users/me/followed-regions")
    assert unauth.status_code == 401
