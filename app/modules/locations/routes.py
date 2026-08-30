import re
from typing import Literal, Optional
from fastapi import APIRouter, HTTPException, Query
from ...db import get_db
from ...cache import cached

router = APIRouter(tags=["locations"])


def _escape_regex(q: str) -> str:
    """Escape user input used inside a MongoDB $regex (prevents ReDoS/injection)."""
    return re.escape(q)


@router.get("/locations/regions")
async def list_regions():
    async def _load():
        cursor = get_db().regions.find({}, {"_id": 0}).sort("name", 1)
        return [d async for d in cursor]
    return await cached("locations:regions", _load)


@router.get("/locations/regions/{region_id}/districts")
async def list_districts(region_id: int):
    async def _load():
        cursor = get_db().districts.find({"region_id": region_id}, {"_id": 0}).sort("name", 1)
        return [d async for d in cursor]
    result = await cached(f"locations:districts:{region_id}", _load)
    if not result:
        raise HTTPException(404, "Region not found or has no districts")
    return result


@router.get("/locations/districts/{district_id}/facilities")
async def list_facilities_in_district(
    district_id: int,
    category: Literal["health", "education"] = Query(...),
    level: Optional[Literal["Primary", "Secondary"]] = Query(None),
    q: Optional[str] = Query(None),
    limit: int = Query(200, le=1000),
):
    db = get_db()
    if category == "education":
        query = {"district_id": district_id}
        if level: query["level"] = level
        if q: query["name"] = {"$regex": _escape_regex(q), "$options": "i"}
        cursor = db.schools.find(query, {"_id": 0, "id": 1, "name": 1, "school_code": 1, "level": 1, "ownership": 1}).sort("name", 1).limit(limit)
        return [d async for d in cursor]

    district = await db.districts.find_one({"id": district_id}, {"_id": 0, "name": 1})
    if not district: raise HTTPException(404, "District not found")
    query = {"district": district["name"]}
    if q: query["name"] = {"$regex": _escape_regex(q), "$options": "i"}
    cursor = db.health_facilities.find(query, {"_id": 0, "code": 1, "name": 1, "type": 1, "type_category": 1, "ownership_category": 1, "status": 1}).sort("name", 1).limit(limit)
    return [d async for d in cursor]


@router.get("/locations/regions/{region_id}/facilities")
async def list_facilities_in_region(
    region_id: int,
    category: Literal["health", "education"] = Query(...),
    level: Optional[Literal["Primary", "Secondary"]] = Query(None),
    sector: Optional[Literal["wizara_afya", "tamisemi"]] = Query(None),
    q: Optional[str] = Query(None),
    limit: int = Query(500, le=2000),
):
    db = get_db()
    if category == "education":
        query = {"region_id": region_id}
        if level: query["level"] = level
        if q: query["name"] = {"$regex": _escape_regex(q), "$options": "i"}
        cursor = db.schools.find(query, {"_id": 0, "id": 1, "name": 1, "school_code": 1, "district_name": 1, "level": 1}).sort("name", 1).limit(limit)
        return [d async for d in cursor]
    region = await db.regions.find_one({"id": region_id}, {"_id": 0, "name": 1})
    if not region: raise HTTPException(404, "Region not found")
    query = {"region": region["name"]}
    if q: query["name"] = {"$regex": _escape_regex(q), "$options": "i"}
    # Sector filter: wizara_afya → referral hospitals only; tamisemi → non-referral
    if sector == "wizara_afya":
        query["type_category"] = {"$in": ["Hospitali ya Taifa (Rufaa)", "Hospitali ya Mkoa (Rufaa)"]}
    elif sector == "tamisemi":
        query["type_category"] = {"$in": ["Hospitali ya Wilaya", "Kituo cha Afya", "Zahanati (Dispensary)"]}
    cursor = db.health_facilities.find(query, {"_id": 0, "code": 1, "name": 1, "district": 1, "type": 1, "type_category": 1}).sort("name", 1).limit(limit)
    return [d async for d in cursor]


@router.get("/locations/departments")
async def list_departments():
    """Idara zote ACTIVE (kwa usajili na dropdown) — idara zilizositishwa
    hazionekani kwa watumiaji (suspend ya idara)."""
    # Hakikisha idara za msingi (Afya + Elimu) zipo hata kabla admin hajaingia.
    db = get_db()
    if not await db.departments.count_documents({}):
        defaults = [
            {"code": "health", "name": "Afya", "status": "active", "icon": "🏥"},
            {"code": "education", "name": "Elimu", "status": "active", "icon": "🏫"},
        ]
        for d in defaults:
            if not await db.departments.find_one({"code": d["code"]}):
                await db.departments.insert_one(dict(d))
    key = "locations:departments"
    async def _load():
        q = {"status": "active"}
        return [d async for d in get_db().departments.find(q, {"_id": 0}).sort("name", 1)]
    return await cached(key, _load)


@router.get("/cadres")
async def list_cadres(
    category: Optional[str] = Query(None),
    sector: Optional[Literal["wizara_afya", "tamisemi"]] = Query(None),
):
    """Cadres — zinaload kutoka DB first time, kisha kutoka cache.
    `sector` inatumika kwa afya tu: wizara_afya vs tamisemi.
    Kwa sasa zote zina kada sawa, lakini sector inahifadhiwa kwa matumizi ya baadaye."""
    key = f"cadres:{category or 'all'}:{sector or 'all'}"
    async def _load():
        q = {}
        if category:
            q["category"] = category
        if sector:
            q["sector"] = {"$in": [sector, None]}  # sector-specific au generic
        return [d async for d in get_db().cadres.find(q, {"_id": 0}).sort("display_name", 1)]
    return await cached(key, _load)


@router.get("/cadres/subjects")
async def list_subjects(level: Optional[str] = Query(None)):
    """Masomo — bila `level` inarudisha yote (Primary + Secondary); ukibainisha
    `level=Primary` au `level=Secondary` unapata kiwango hicho tu."""
    key = f"subjects:{level or 'all'}"
    async def _load():
        q = {"level": level} if level else {}
        return [d async for d in get_db().subjects.find(q, {"_id": 0}).sort("name", 1)]
    return await cached(key, _load)
