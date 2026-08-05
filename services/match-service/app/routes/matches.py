from bson import ObjectId
from fastapi import APIRouter, Depends, Query
from ..core.auth import current_user
from ..core.db import get_db
from ..matching import find_matches_for_user

router = APIRouter(prefix="/matches", tags=["matches"])


@router.get("/me")
async def my_matches(user=Depends(current_user), limit: int = Query(50, le=200)):
    """Fresh recomputation of matches for the current user."""
    matches = await find_matches_for_user(get_db(), user)
    return {"count": len(matches), "matches": matches[:limit]}


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
