from typing import Literal, Optional
from fastapi import APIRouter, HTTPException, Query
from ..core.db import get_db
from ..core.cache import cached

router = APIRouter(prefix="/locations", tags=["locations"])


@router.get("/regions")
async def list_regions():
    async def _load():
        cursor = get_db().regions.find({}, {"_id": 0}).sort("name", 1)
        return [doc async for doc in cursor]
    return await cached("locations:regions", _load)


@router.get("/regions/{region_id}/districts")
async def list_districts(region_id: int):
    async def _load():
        cursor = get_db().districts.find({"region_id": region_id}, {"_id": 0}).sort("name", 1)
        return [doc async for doc in cursor]
    result = await cached(f"locations:districts:{region_id}", _load)
    if not result:
        raise HTTPException(404, "Region not found or has no districts")
    return result


@router.get("/districts/{district_id}/facilities")
async def list_facilities_in_district(
    district_id: int,
    category: Literal["health", "education"] = Query(..., description="health or education"),
    level: Optional[Literal["Primary", "Secondary"]] = Query(None, description="For education only"),
    q: Optional[str] = Query(None, description="Search by name"),
    limit: int = Query(200, le=1000),
):
    """
    Kwa education: inarudi schools za wilaya (filter kwa level: Primary au Secondary).
    Kwa health: inarudi health facilities za wilaya (district_name match).
    """
    db = get_db()

    if category == "education":
        query = {"district_id": district_id}
        if level:
            query["level"] = level
        if q:
            query["name"] = {"$regex": q, "$options": "i"}
        cursor = db.schools.find(
            query,
            {"_id": 0, "id": 1, "name": 1, "school_code": 1, "level": 1, "ownership": 1}
        ).sort("name", 1).limit(limit)
        return [doc async for doc in cursor]

    # health path — we look up district name to match facilities.district
    district = await db.districts.find_one({"id": district_id}, {"_id": 0, "name": 1})
    if not district:
        raise HTTPException(404, "District not found")
    query = {"district": district["name"]}
    if q:
        query["name"] = {"$regex": q, "$options": "i"}
    cursor = db.health_facilities.find(
        query,
        {"_id": 0, "code": 1, "name": 1, "type": 1, "type_category": 1, "ownership_category": 1, "status": 1}
    ).sort("name", 1).limit(limit)
    return [doc async for doc in cursor]


@router.get("/regions/{region_id}/facilities")
async def list_facilities_in_region(
    region_id: int,
    category: Literal["health", "education"] = Query(...),
    level: Optional[Literal["Primary", "Secondary"]] = Query(None),
    q: Optional[str] = Query(None),
    limit: int = Query(500, le=2000),
):
    """Grouped facilities kwa mkoa mzima (kwa facilities kubwa kama RRH, Zonal Hospitals)."""
    db = get_db()

    if category == "education":
        query = {"region_id": region_id}
        if level:
            query["level"] = level
        if q:
            query["name"] = {"$regex": q, "$options": "i"}
        cursor = db.schools.find(
            query,
            {"_id": 0, "id": 1, "name": 1, "school_code": 1, "district_name": 1, "level": 1}
        ).sort("name", 1).limit(limit)
        return [doc async for doc in cursor]

    region = await db.regions.find_one({"id": region_id}, {"_id": 0, "name": 1})
    if not region:
        raise HTTPException(404, "Region not found")
    query = {"region": region["name"]}
    if q:
        query["name"] = {"$regex": q, "$options": "i"}
    cursor = db.health_facilities.find(
        query,
        {"_id": 0, "code": 1, "name": 1, "district": 1, "type": 1, "type_category": 1}
    ).sort("name", 1).limit(limit)
    return [doc async for doc in cursor]
