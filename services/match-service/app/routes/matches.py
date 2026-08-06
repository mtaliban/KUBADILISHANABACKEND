from typing import Optional
from bson import ObjectId
from fastapi import APIRouter, Depends, Query
from ..core.auth import current_user
from ..core.db import get_db
from ..matching import find_matches_for_user

router = APIRouter(prefix="/matches", tags=["matches"])


def _filter_matches(matches, region_id=None, district_id=None, facility_id=None):
    out = []
    for m in matches:
        st = m["candidate"]["current_station"]
        if facility_id and st.get("facility_id") != facility_id:
            continue
        if district_id and st.get("district_id") != district_id:
            continue
        if region_id and st.get("region_id") != region_id:
            continue
        out.append(m)
    return out


@router.get("/me")
async def my_matches(
    user=Depends(current_user),
    region_id: Optional[int] = Query(None),
    district_id: Optional[int] = Query(None),
    facility_id: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
):
    """People wanting to swap with me (reverse-matched), optionally filtered by their current location."""
    matches = await find_matches_for_user(get_db(), user)
    filtered = _filter_matches(matches, region_id, district_id, facility_id)
    return {"total": len(matches), "filtered": len(filtered), "matches": filtered[:limit]}


@router.get("/stats")
async def matches_stats(user=Depends(current_user)):
    """Aggregate counts of potential swap-mates grouped by Region, District, Facility."""
    matches = await find_matches_for_user(get_db(), user)
    per_region: dict = {}
    per_district: dict = {}
    per_facility: dict = {}
    for m in matches:
        st = m["candidate"]["current_station"]
        r_key = (st["region_id"], st["region_name"])
        per_region[r_key] = per_region.get(r_key, 0) + 1
        if st.get("district_id"):
            d_key = (st["district_id"], st["district_name"], st["region_name"])
            per_district[d_key] = per_district.get(d_key, 0) + 1
        if st.get("facility_id"):
            f_key = (st["facility_id"], st.get("facility_name"), st.get("district_name"))
            per_facility[f_key] = per_facility.get(f_key, 0) + 1
    return {
        "total_matches": len(matches),
        "by_region": [
            {"region_id": k[0], "region_name": k[1], "count": v}
            for k, v in sorted(per_region.items(), key=lambda x: -x[1])
        ],
        "by_district": [
            {"district_id": k[0], "district_name": k[1], "region_name": k[2], "count": v}
            for k, v in sorted(per_district.items(), key=lambda x: -x[1])
        ],
        "by_facility": [
            {"facility_id": k[0], "facility_name": k[1], "district_name": k[2], "count": v}
            for k, v in sorted(per_facility.items(), key=lambda x: -x[1])
        ],
    }


@router.get("/me/cached")
async def my_cached_matches(user=Depends(current_user), limit: int = Query(50, le=200)):
    """Cached matches (from `matches` collection populated by MQTT subscriber)."""
    db = get_db()
    uid = str(user["_id"])
    cursor = db.matches.find({"$or": [{"user_a_id": uid}, {"user_b_id": uid}]}).sort("matched_at", -1).limit(limit)
    results = []
    async for m in cursor:
        other_id = m["user_b_id"] if m["user_a_id"] == uid else m["user_a_id"]
        other = await db.users.find_one({"_id": ObjectId(other_id)})
        if not other:
            continue
        results.append({
            "score": m["score"],
            "matched_at": m["matched_at"],
            "status": m.get("status", "new"),
            "candidate": {
                "user_id": str(other["_id"]),
                "full_name": other["full_name"],
                "phone_primary": other["phone_primary"],
                "cadre_display": other.get("cadre_display"),
                "current_station": other["current_station"],
                "desired_destinations": other.get("desired_destinations", []),
            },
        })
    return {"count": len(results), "matches": results}
