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
          subjects: list[str] | None = None, is_admin: bool = False) -> dict:
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
        "subjects": subjects or [],
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


async def test_board_education_level_filter(app, db, client):
    """Mwalimu wa MSINGI aone walimu wa MSINGI tu (sio sekondari)."""
    await db.cadres.insert_one({"code": "TEACHER_PRIMARY", "category": "education", "level": "Primary"})
    await db.cadres.insert_one({"code": "TEACHER_SECONDARY", "category": "education", "level": "Secondary"})

    me = _user("+255711000001", region_id=4, region_name="Dodoma", dest_region_ids=[3],
               cadre="TEACHER_PRIMARY", cadre_display="Mwalimu wa Elimu ya Msingi", category="education")
    primary_dar = _user("+255711000002", region_id=3, region_name="Dar Es Salaam", dest_region_ids=[4],
                        cadre="TEACHER_PRIMARY", cadre_display="Mwalimu wa Elimu ya Msingi", category="education")
    secondary_dar = _user("+255711000003", region_id=3, region_name="Dar Es Salaam", dest_region_ids=[4],
                          cadre="TEACHER_SECONDARY", cadre_display="Mwalimu wa Elimu ya Sekondari",
                          category="education")
    for u in (me, primary_dar, secondary_dar):
        await db.users.insert_one(u)
    token = create_access_token(str(me["_id"]))

    res = await client.get("/matches/board?scope=all", headers=_auth(token))
    body = res.json()
    ids = {c["user_id"] for c in body["candidates"]}
    assert str(primary_dar["_id"]) in ids
    assert str(secondary_dar["_id"]) not in ids  # mwalimu wa sekondari HAYUPO kwa msingi


async def test_board_subject_match_education(app, db, client):
    """MAHITAJI MAPYA (user): mtu aone WOTE wa idara yake hata kama masomo
    hayakufanana (default). Akiweka subject_match=true → waonekane tu wenye
    ANGALAU somo moja linalofanana."""
    await db.cadres.insert_one({"code": "TEACHER_PRIMARY", "category": "education", "level": "Primary"})

    me = _user("+255711000001", region_id=4, region_name="Dodoma", dest_region_ids=[3],
               cadre="TEACHER_PRIMARY", cadre_display="Mwalimu wa Elimu ya Msingi",
               category="education", subjects=["MATH", "KISWAHILI"])
    # Dar: ana MATH → anafanana
    same_math = _user("+255711000002", region_id=3, region_name="Dar Es Salaam", dest_region_ids=[4],
                      cadre="TEACHER_PRIMARY", cadre_display="Mwalimu wa Elimu ya Msingi",
                      category="education", subjects=["MATH"])
    # Dar: ana SCIENCE pekee → masomo tofauti (bado AONEKANE default!)
    diff_science = _user("+255711000003", region_id=3, region_name="Dar Es Salaam", dest_region_ids=[4],
                         cadre="TEACHER_PRIMARY", cadre_display="Mwalimu wa Elimu ya Msingi",
                         category="education", subjects=["SCIENCE"])
    # Pwani: ana KISWAHILI → anafanana
    same_sw = _user("+255711000004", region_id=19, region_name="Pwani", dest_region_ids=[4],
                    cadre="TEACHER_PRIMARY", cadre_display="Mwalimu wa Elimu ya Msingi",
                    category="education", subjects=["KISWAHILI"])
    # Hana masomo → hachujwi → AONEKANE kila wakati
    no_subjects = _user("+255711000005", region_id=3, region_name="Dar Es Salaam", dest_region_ids=[4],
                        cadre="TEACHER_PRIMARY", cadre_display="Mwalimu wa Elimu ya Msingi",
                        category="education", subjects=[])
    for u in (me, same_math, diff_science, same_sw, no_subjects):
        await db.users.insert_one(u)
    token = create_access_token(str(me["_id"]))

    # DEFAULT (no subject_match): wote wa idara yake wanaonekana — hata masomo tofauti
    res = await client.get("/matches/board?scope=incoming", headers=_auth(token))
    assert res.status_code == 200
    body = res.json()
    ids = {c["user_id"] for c in body["candidates"]}
    assert str(same_math["_id"]) in ids
    assert str(same_sw["_id"]) in ids
    assert str(no_subjects["_id"]) in ids
    assert str(diff_science["_id"]) in ids  # masomo tofauti BADO anaonekana (idara yake)

    # subject_match=true: diff_science aondoke, waliofanana wabaki
    res2 = await client.get("/matches/board?scope=incoming&subject_match=true", headers=_auth(token))
    body2 = res2.json()
    ids2 = {c["user_id"] for c in body2["candidates"]}
    assert str(same_math["_id"]) in ids2
    assert str(same_sw["_id"]) in ids2
    assert str(no_subjects["_id"]) in ids2
    assert str(diff_science["_id"]) not in ids2  # subject_match=ON → masomo tofauti HAYUPO
    # candidates wanarudisha subjects (kwa frontend kuonyesha/highlight)
    math_card = next(c for c in body2["candidates"] if c["user_id"] == str(same_math["_id"]))
    assert math_card["subjects"] == ["MATH"]


async def test_board_incoming_district_destination_not_blocked(app, db, client):
    """TATIZO HALISI (production): mwalimu wa Dar (Ilala) anataka kwenda Dodoma
    (Chamwino) na mwalimu wa Dodoma (Chamwino) anataka kuja Dar (Kigamboni).
    Destination za wilaya (Kigamboni) hazifai kuficha mtu kwa mwenyeji wa
    wilaya nyingine ya mkoa ule ule (Ilala) — anafaa kuonekana kwa MKOA."""
    await db.cadres.insert_one({"code": "TEACHER_PRIMARY", "category": "education", "level": "Primary"})

    me = _user("+255711000001", region_id=3, region_name="Dar Es Salaam", district_id=17,
               district_name="Ilala Mc", cadre="TEACHER_PRIMARY",
               cadre_display="Mwalimu wa Elimu ya Msingi", category="education",
               subjects=["CHEM", "BIO"], dest_region_ids=[4])  # nataka kwenda Dodoma
    me["desired_destinations"] = [{"region_id": 4, "region_name": "Dodoma",
                                   "district_id": 23, "district_name": "Chamwino Dc",
                                   "facility_id": None, "facility_name": None}]

    dodoma_teacher = _user("+255711000002", region_id=4, region_name="Dodoma", district_id=23,
                           district_name="Chamwino Dc", cadre="TEACHER_PRIMARY",
                           cadre_display="Mwalimu wa Elimu ya Msingi", category="education",
                           subjects=["BIO", "CHEM"], dest_region_ids=[3])  # anataka kuja Dar
    dodoma_teacher["desired_destinations"] = [{"region_id": 3, "region_name": "Dar Es Salaam",
                                                "district_id": 18, "district_name": "Kigamboni Mc",
                                                "facility_id": None, "facility_name": None}]
    # Mwengine wa Dodoma hataki Dar → asionekane
    other_dodoma = _user("+255711000003", region_id=4, region_name="Dodoma", district_id=23,
                         district_name="Chamwino Dc", cadre="TEACHER_PRIMARY",
                         cadre_display="Mwalimu wa Elimu ya Msingi", category="education",
                         dest_region_ids=[19])  # anataka kwenda Pwani tu
    for u in (me, dodoma_teacher, other_dodoma):
        await db.users.insert_one(u)
    token = create_access_token(str(me["_id"]))

    res = await client.get("/matches/board?scope=incoming&region_ids=4", headers=_auth(token))
    assert res.status_code == 200
    body = res.json()
    ids = {c["user_id"] for c in body["candidates"]}
    # Mwalimu wa Dodoma anayetaka kuja Dar (Kigamboni) AONEKANE kwa mwenyeji wa Dar
    assert str(dodoma_teacher["_id"]) in ids
    # Asiyetaka kuja Dar asionekane
    assert str(other_dodoma["_id"]) not in ids
    # Score ≥ region-level (0.5) — Kigamboni siyo Ilala lakini mkoa ni Dar
    card = next(c for c in body["candidates"] if c["user_id"] == str(dodoma_teacher["_id"]))
    assert (card.get("score") or 0) >= 0.5


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
