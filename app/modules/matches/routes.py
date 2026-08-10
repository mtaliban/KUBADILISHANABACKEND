from typing import Literal, Optional
from bson import ObjectId
from fastapi import APIRouter, Depends, Query
from ...db import get_db
from ...security import current_user
from .matching import find_matches_for_user
from ..messaging.ws_manager import manager as ws_manager

router = APIRouter(prefix="/matches", tags=["matches"])


def _filter(matches, region_id=None, district_id=None, facility_id=None):
    out = []
    for m in matches:
        st = m["candidate"]["current_station"]
        if facility_id and st.get("facility_id") != facility_id: continue
        if district_id and st.get("district_id") != district_id: continue
        if region_id and st.get("region_id") != region_id: continue
        out.append(m)
    return out


@router.get("/me")
async def my_matches(user=Depends(current_user),
                     region_id: Optional[int] = None, district_id: Optional[int] = None,
                     facility_id: Optional[str] = None, limit: int = Query(100, le=500)):
    matches = await find_matches_for_user(get_db(), user)
    f = _filter(matches, region_id, district_id, facility_id)
    return {"total": len(matches), "filtered": len(f), "matches": f[:limit]}


@router.get("/stats")
async def stats(user=Depends(current_user)):
    matches = await find_matches_for_user(get_db(), user)
    per_r, per_d, per_f = {}, {}, {}
    for m in matches:
        st = m["candidate"]["current_station"]
        rk = (st["region_id"], st["region_name"])
        per_r[rk] = per_r.get(rk, 0) + 1
        if st.get("district_id"):
            dk = (st["district_id"], st["district_name"], st["region_name"])
            per_d[dk] = per_d.get(dk, 0) + 1
        if st.get("facility_id"):
            fk = (st["facility_id"], st.get("facility_name"), st.get("district_name"))
            per_f[fk] = per_f.get(fk, 0) + 1
    return {
        "total_matches": len(matches),
        "by_region": [{"region_id": k[0], "region_name": k[1], "count": v}
                      for k, v in sorted(per_r.items(), key=lambda x: -x[1])],
        "by_district": [{"district_id": k[0], "district_name": k[1], "region_name": k[2], "count": v}
                        for k, v in sorted(per_d.items(), key=lambda x: -x[1])],
        "by_facility": [{"facility_id": k[0], "facility_name": k[1], "district_name": k[2], "count": v}
                        for k, v in sorted(per_f.items(), key=lambda x: -x[1])],
    }


def _candidate_out(u: dict, score: float | None = None) -> dict:
    """Grid card payload for the dashboard board."""
    return {
        "user_id": str(u["_id"]),
        "full_name": u["full_name"],
        "phone_primary": u.get("phone_primary"),
        "cadre_display": u.get("cadre_display"),
        "cadre_code": u.get("cadre_code"),
        "category": u.get("category"),
        "score": score,
        "current_station": u.get("current_station"),
        "desired_destinations": u.get("desired_destinations", []),
        "online": ws_manager.is_online(str(u["_id"])),
        "created_at": u.get("created_at"),
    }


@router.get("/board")
async def board(
    user=Depends(current_user),
    scope: Literal["incoming", "all"] = Query("incoming", description="incoming = wanaokuja mkoa wako; all = watumiaji wote"),
    region_id: Optional[int] = None,
    district_id: Optional[int] = None,
    facility_id: Optional[str] = None,
    limit: int = Query(200, le=1000),
):
    """Dashboard stats board (ad-board juu ya dashboard).

    - `scope=incoming` (default): watu wanaokuja mkoa wako — matches halisi
      (wanaotaka kuja kwako, kutoka mikoa unayotaka kwenda).
    - `scope=all`: watumiaji wote wa mfumo (stats za mikoa yao).

    Filters (region_id / district_id / facility_id) zinachuja kwenye
    **kituo cha sasa** cha mtumiaji (yaani wapi yupo sasa) — kwa hiyo
    default ya frontend ni kufilter kwa mkoa wa destination yako ya kwanza.
    """
    db = get_db()
    my_station = user.get("current_station") or {}

    # ── Gather candidates (full set for stats; limited for grid) ──
    if scope == "all":
        q = {"status": "active", "is_admin": {"$ne": True}, "_id": {"$ne": user["_id"]}}
        if region_id is not None:
            q["current_station.region_id"] = region_id
        if district_id is not None:
            q["current_station.district_id"] = district_id
        if facility_id is not None:
            q["current_station.facility_id"] = facility_id
        cursor = db.users.find(q, {"password_hash": 0}).sort("created_at", -1)
        raw = [u async for u in cursor]
        stat_rows = raw                     # full set → stats kamili
        candidates = [_candidate_out(u) for u in raw[:limit]]
        stat_total = len(raw)
    else:
        matches = await find_matches_for_user(db, user)
        if region_id is not None or district_id is not None or facility_id is not None:
            matches = _filter(matches, region_id, district_id, facility_id)
        stat_rows = [m["candidate"] for m in matches]   # full set → stats kamili
        stat_total = len(matches)
        candidates = []
        for m in matches[:limit]:
            c = m["candidate"]
            candidates.append({
                "user_id": c["user_id"], "full_name": c["full_name"],
                "phone_primary": c.get("phone_primary"),
                "cadre_display": c.get("cadre_display"), "cadre_code": c.get("cadre_code"),
                "category": c.get("category"), "score": m["score"],
                "current_station": c.get("current_station"),
                "desired_destinations": c.get("desired_destinations", []),
                "online": ws_manager.is_online(c["user_id"]),
            })

    # ── Stats by region / district / facility (kituo cha sasa cha candidate) ──
    per_r, per_d, per_f = {}, {}, {}
    for c in stat_rows:
        st = c.get("current_station") or {}
        rk = (st.get("region_id"), st.get("region_name"))
        per_r[rk] = per_r.get(rk, 0) + 1
        if st.get("district_id"):
            dk = (st.get("district_id"), st.get("district_name"), st.get("region_name"))
            per_d[dk] = per_d.get(dk, 0) + 1
        if st.get("facility_id"):
            fk = (st.get("facility_id"), st.get("facility_name"), st.get("district_name"), st.get("district_id"))
            per_f[fk] = per_f.get(fk, 0) + 1

    return {
        "scope": scope,
        "total": stat_total,
        "candidates": candidates,
        "by_region": [{"region_id": k[0], "region_name": k[1], "count": v}
                       for k, v in sorted(per_r.items(), key=lambda x: -x[1])],
        "by_district": [{"district_id": k[0], "district_name": k[1], "region_name": k[2], "count": v}
                         for k, v in sorted(per_d.items(), key=lambda x: -x[1])],
        "by_facility": [{"facility_id": k[0], "facility_name": k[1], "district_name": k[2], "district_id": k[3], "count": v}
                         for k, v in sorted(per_f.items(), key=lambda x: -x[1])],
    }


@router.get("/me/cached")
async def cached_matches(user=Depends(current_user), limit: int = Query(50, le=200)):
    db = get_db()
    uid = str(user["_id"])
    cur = db.matches.find({"$or": [{"user_a_id": uid}, {"user_b_id": uid}]}).sort("matched_at", -1).limit(limit)
    out = []
    async for m in cur:
        other_id = m["user_b_id"] if m["user_a_id"] == uid else m["user_a_id"]
        other = await db.users.find_one({"_id": ObjectId(other_id)})
        if not other: continue
        out.append({
            "score": m["score"], "matched_at": m["matched_at"], "status": m.get("status", "new"),
            "candidate": {
                "user_id": str(other["_id"]), "full_name": other["full_name"],
                "phone_primary": other["phone_primary"], "cadre_display": other.get("cadre_display"),
                "current_station": other["current_station"],
                "desired_destinations": other.get("desired_destinations", []),
            },
        })
    return {"count": len(out), "matches": out}
