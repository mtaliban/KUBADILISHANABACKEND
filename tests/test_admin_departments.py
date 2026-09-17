"""Admin: idara (departments) — regression za bug ya "nikiadd idara inaleta
error" na "usajili haionyeshi idara mpya".

Sababu:
1. DepartmentIn.code ilikuwa na pattern mkali `^[a-z0-9_-]+$` (max 30) —
   jina/code yenye nafasi au herufi kubwa (k.m. "Idara Ya Afya") ilikata 422.
2. `/locations/departments` inarudisha idara ACTIVE tu — sawa — ila wizard ya
   web ilificha idara isiyo na kada (imefanyiwa fix kwenye frontend).
"""

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

import app.modules.auth.routes as auth_routes
import app.modules.admin.routes as admin_routes
import app.security as security
from app.modules.auth.routes import router as auth_router
from app.modules.admin.routes import router as admin_router
from app.security import create_access_token, hash_password


@pytest.fixture
def app(db, monkeypatch):
    """App ndogo yenye admin router tu, DB ya kumbukumbu, bila Redis."""
    application = FastAPI()
    application.include_router(auth_router)
    application.include_router(admin_router)
    monkeypatch.setattr(auth_routes, "get_db", lambda: db)
    monkeypatch.setattr(admin_routes, "get_db", lambda: db)
    monkeypatch.setattr(security, "get_db", lambda: db)
    # Redis cache — identity (hakuna Redis kwenye tests)
    async def _bust_ok() -> None:
        return None
    monkeypatch.setattr(admin_routes, "_bust_location_caches", _bust_ok)
    monkeypatch.setattr(admin_routes, "_bust_admin_caches", _bust_ok)
    return application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seed_admin(db):
    from datetime import datetime, timezone
    from bson import ObjectId
    now = datetime.now(timezone.utc)
    admin = {
        "_id": ObjectId(),
        "full_name": "Admin",
        "phone_primary": "+255711000001",
        "password_hash": hash_password("secret123"),
        "category": "health",
        "cadre_code": "CO",
        "cadre_display": "Clinical Officer",
        "subjects": [],
        "current_station": {},
        "desired_destinations": [],
        "status": "active",
        "is_verified": True,
        "is_admin": True,
        "email_verified": True,
        "followed_regions": [],
        "created_at": now,
        "updated_at": now,
    }
    await db.users.insert_one(admin)
    return admin


async def test_add_department_with_spaces_and_caps_in_code(db, client):
    """Code yenye herufi kubwa + nafasi ("Idara Ya Afya") lazima isikatae 422 —
    inaslugiwa kuwa `idara_ya_afya`."""
    admin = await _seed_admin(db)
    token = create_access_token(str(admin["_id"]))
    r = await client.post(
        "/admin/data/departments",
        json={"code": "Idara Ya Afya", "name": "Afya ya Umma", "status": "active"},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["department"]["code"] == "idara_ya_afya"


async def test_add_department_code_from_name_when_empty(db, client):
    """Code ikiachwa wazi, inatengenezwa kutoka jina."""
    admin = await _seed_admin(db)
    token = create_access_token(str(admin["_id"]))
    r = await client.post(
        "/admin/data/departments",
        json={"code": "", "name": "Maji na Usafi", "status": "active"},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["department"]["code"] == "maji_na_usafi"


async def test_add_department_duplicate_returns_clear_409(db, client):
    """Idara iliyoipo inarudisha 409 na ujumbe mzuri (sio 422 ya kimya)."""
    admin = await _seed_admin(db)
    token = create_access_token(str(admin["_id"]))
    body = {"code": "afya_ya_umma", "name": "Afya ya Umma", "status": "active"}
    r1 = await client.post("/admin/data/departments", json=body, headers=_auth(token))
    assert r1.status_code == 200
    r2 = await client.post("/admin/data/departments", json=body, headers=_auth(token))
    assert r2.status_code == 409
    assert "tayari ipo" in r2.json()["detail"]


async def test_department_code_min_length_still_enforced(db, client):
    """Code fupi mno (chini ya herufi 2) bado inakataliwa."""
    admin = await _seed_admin(db)
    token = create_access_token(str(admin["_id"]))
    r = await client.post(
        "/admin/data/departments",
        json={"code": "a", "name": "Afya", "status": "active"},
        headers=_auth(token),
    )
    assert r.status_code == 422
