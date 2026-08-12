import asyncio
import csv
import io
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Literal
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from ...config import settings
from ...db import get_db
from ...events.publisher import publish
from ...events.topics import (
    TOPIC_USER_UPDATED_BY_ADMIN, TOPIC_USER_DELETED, TOPIC_USER_ADMIN_CHANGED, TOPIC_PAGE_VIEWED,
)
from ...security import current_admin, current_user, _is_valid_object_id, hash_password
from ...cache import get_redis
from ...emailer import get_email_config, send_email

router = APIRouter(prefix="/admin", tags=["admin"])


# ─── Redis cache kwa admin data ───────────────────────────────────────────
# /admin/stats, /admin/reports, /admin/events na /admin/users wanafanya
# aggregations + queries nzito kwenye DB. Tunacache matokeo kwa sekunde
# chache (Redis) — kurudi kwenye admin pages hakupigi DB kila mara. Cache
# inafutwa (bust) kila mutation ya admin (update/delete/grant/clear) ili
# data ionekane FRESH mara moja. Ikiwa Redis haipo → tunaendelea bila cache
# (kama cached() kwenye cache.py) — hakuna kuvunjika.


async def _cache_get(key: str) -> dict | list | None:
    try:
        r = get_redis()
        v = await r.get(key)
        return json.loads(v) if v is not None else None
    except Exception:
        return None


async def _cache_set(key: str, value, ttl: int) -> None:
    try:
        r = get_redis()
        await r.setex(key, ttl, json.dumps(value, default=str))
    except Exception:
        pass


async def _bust_admin_caches() -> None:
    try:
        r = get_redis()
        keys = [k async for k in r.scan_iter("admin:*")]
        if keys:
            await r.delete(*keys)
    except Exception:
        pass


def _escape_regex(q: str) -> str:
    return re.escape(q)


def _as_object_id(user_id: str) -> ObjectId:
    if not _is_valid_object_id(user_id):
        raise HTTPException(400, "Invalid user_id")
    return ObjectId(user_id)


@router.get("/stats")
async def stats(_=Depends(current_admin)):
    cached_res = await _cache_get("admin:stats")
    if cached_res is not None:
        return cached_res
    db = get_db()
    now = datetime.now(timezone.utc)
    last24 = now - timedelta(hours=24); last7 = now - timedelta(days=7)
    users_total = await db.users.count_documents({})
    users_health = await db.users.count_documents({"category": "health"})
    users_edu = await db.users.count_documents({"category": "education"})
    users_verified = await db.users.count_documents({"is_verified": True})
    users_active_7d = await db.users.count_documents({"last_seen_at": {"$gte": last7}})
    matches_total = await db.matches.count_documents({})
    matches_24h = await db.matches.count_documents({"matched_at": {"$gte": last24}})
    events_total = await db.event_log.count_documents({})
    events_24h = await db.event_log.count_documents({"occurred_at": {"$gte": last24}})
    msgs_total = await db.messages.count_documents({})
    calls_total = await db.call_logs.count_documents({})

    async def _agg(pipeline):
        return [x async for x in db.users.aggregate(pipeline)]

    by_cadre_raw = await _agg([
        {"$group": {"_id": {"cat": "$category", "cadre": "$cadre_code"}, "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ])
    by_cadre = [{"category": r["_id"]["cat"], "cadre": r["_id"]["cadre"], "count": r["n"]} for r in by_cadre_raw]

    by_region_raw = await _agg([
        {"$group": {"_id": "$current_station.region_name", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ])
    by_region = [{"region": r["_id"], "count": r["n"]} for r in by_region_raw]

    events_by_type = []
    async for r in db.event_log.aggregate([
        {"$group": {"_id": "$event_type", "n": {"$sum": 1}}}, {"$sort": {"n": -1}},
    ]):
        events_by_type.append({"event_type": r["_id"], "count": r["n"]})

    result = {
        "totals": {"users": users_total, "users_health": users_health, "users_education": users_edu,
                   "users_verified": users_verified, "users_active_7d": users_active_7d,
                   "matches": matches_total, "matches_24h": matches_24h,
                   "events": events_total, "events_24h": events_24h,
                   "messages": msgs_total, "calls": calls_total},
        "by_cadre": by_cadre, "by_region": by_region, "events_by_type": events_by_type,
    }
    await _cache_set("admin:stats", result, 15)
    return result


@router.get("/users")
async def list_users(_=Depends(current_admin),
                     category: Optional[Literal["health", "education"]] = None,
                     cadre_code: Optional[str] = None, region_id: Optional[int] = None,
                     q: Optional[str] = None, limit: int = Query(100, le=500), skip: int = Query(0, ge=0)):
    cache_key = f"admin:users:{category or '-'}:{cadre_code or '-'}:{region_id or '-'}:{q or '-'}:{limit}:{skip}"
    cached_res = await _cache_get(cache_key)
    if cached_res is not None:
        return cached_res
    db = get_db(); qd = {}
    if category: qd["category"] = category
    if cadre_code: qd["cadre_code"] = cadre_code
    if region_id: qd["current_station.region_id"] = region_id
    if q: qd["$or"] = [{"full_name": {"$regex": _escape_regex(q), "$options": "i"}}, {"phone_primary": {"$regex": _escape_regex(q)}}]
    total = await db.users.count_documents(qd)
    cur = db.users.find(qd, {"password_hash": 0}).sort("created_at", -1).skip(skip).limit(limit)
    users = []
    async for u in cur:
        u["_id"] = str(u["_id"]); users.append(u)
    result = {"total": total, "skip": skip, "limit": limit, "users": users}
    await _cache_set(cache_key, result, 10)
    return result


@router.get("/matches")
async def list_matches(_=Depends(current_admin), limit: int = Query(100, le=500)):
    db = get_db(); total = await db.matches.count_documents({})
    cur = db.matches.find().sort("matched_at", -1).limit(limit)
    out = []
    async for m in cur:
        m["_id"] = str(m["_id"])
        a = await db.users.find_one({"_id": ObjectId(m["user_a_id"])}, {"full_name": 1, "phone_primary": 1, "cadre_code": 1, "current_station": 1})
        b = await db.users.find_one({"_id": ObjectId(m["user_b_id"])}, {"full_name": 1, "phone_primary": 1, "cadre_code": 1, "current_station": 1})
        if a and b:
            m["user_a"] = {"full_name": a["full_name"], "phone": a["phone_primary"], "cadre": a["cadre_code"], "region": a["current_station"]["region_name"]}
            m["user_b"] = {"full_name": b["full_name"], "phone": b["phone_primary"], "cadre": b["cadre_code"], "region": b["current_station"]["region_name"]}
        out.append(m)
    return {"total": total, "matches": out}


@router.get("/events")
async def list_events(_=Depends(current_admin), event_type: Optional[str] = None,
                      limit: int = Query(100, le=500), skip: int = Query(0, ge=0)):
    cache_key = f"admin:events:{event_type or '-'}:{limit}:{skip}"
    cached_res = await _cache_get(cache_key)
    if cached_res is not None:
        return cached_res
    db = get_db(); q = {"event_type": event_type} if event_type else {}
    total = await db.event_log.count_documents(q)
    cur = db.event_log.find(q).sort("occurred_at", -1).skip(skip).limit(limit)
    events = []
    async for e in cur:
        e["_id"] = str(e["_id"]); events.append(e)
    result = {"total": total, "skip": skip, "limit": limit, "events": events}
    await _cache_set(cache_key, result, 10)
    return result


@router.get("/csv/list")
async def csv_list(_=Depends(current_admin)):
    p = Path(settings.csv_output_dir)
    if not p.exists(): return {"files": []}
    files = []
    for f in sorted(p.glob("events_*.csv")):
        files.append({"name": f.name, "size_bytes": f.stat().st_size,
                      "modified_at": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat()})
    return {"files": files}


@router.get("/csv/download/{name}")
async def csv_download(name: str, _=Depends(current_admin)):
    if not name.startswith("events_") or not name.endswith(".csv") or "/" in name:
        raise HTTPException(400, "Invalid name")
    p = Path(settings.csv_output_dir) / name
    if not p.exists(): raise HTTPException(404)
    return FileResponse(str(p), media_type="text/csv", filename=name)


@router.get("/live-events")
async def live_events(_=Depends(current_admin)):
    """Server-Sent Events feed of newest event_log docs (polling every 2s)."""
    db = get_db()
    async def gen():
        last_seen = datetime.now(timezone.utc) - timedelta(seconds=5)
        while True:
            cur = db.event_log.find({"occurred_at": {"$gt": last_seen}}).sort("occurred_at", 1).limit(50)
            async for e in cur:
                e["_id"] = str(e["_id"])
                last_seen = e["occurred_at"]
                yield f"data: {json.dumps(e, default=str)}\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(gen(), media_type="text/event-stream")


class AdminUpdateUser(BaseModel):
    full_name: str | None = Field(None, min_length=3, max_length=100)
    phone_alt: str | None = None
    category: str | None = None
    cadre_code: str | None = None
    subjects: list[str] | None = None
    current_station: dict | None = None
    desired_destinations: list[dict] | None = None
    status: str | None = None
    is_verified: bool | None = None
    is_admin: bool | None = None
    new_password: str | None = Field(None, min_length=6)


@router.patch("/users/{user_id}")
async def admin_update_user(user_id: str, body: AdminUpdateUser, _=Depends(current_admin)):
    updates = body.model_dump(exclude_none=True)
    if "new_password" in updates:
        updates["password_hash"] = hash_password(updates.pop("new_password"))
    # Kada inabadilishwa → sasisha pia cadre_display (jina linaloonekana) ili
    # wasifu na cards zioneshe kada sahihi, sio ya zamani.
    if "cadre_code" in updates:
        cadre = await get_db().cadres.find_one({"code": updates["cadre_code"]}, {"_id": 0, "display_name": 1})
        if cadre:
            updates["cadre_display"] = cadre["display_name"]
    if not updates:
        raise HTTPException(400, "No changes")
    updates["updated_at"] = datetime.now(timezone.utc)
    oid = _as_object_id(user_id)
    r = await get_db().users.update_one({"_id": oid}, {"$set": updates})
    if not r.matched_count:
        raise HTTPException(404, "User not found")
    # Emit event so the subscriber recomputes matches when station/destinations/
    # cadre changed by an admin (previously admin edits silently skipped matching).
    publish(TOPIC_USER_UPDATED_BY_ADMIN, {
        "event": "user.updated_by_admin", "user_id": user_id,
        "changed_fields": [k for k in updates if k not in ("updated_at", "password_hash")],
        "occurred_at": updates["updated_at"].isoformat(),
    })
    fresh = await get_db().users.find_one({"_id": oid}, {"password_hash": 0})
    fresh["_id"] = str(fresh["_id"])
    await _bust_admin_caches()
    return fresh


@router.delete("/users/{user_id}")
async def admin_delete_user(user_id: str, _=Depends(current_admin)):
    db = get_db()
    oid = _as_object_id(user_id)
    r = await db.users.delete_one({"_id": oid})
    if not r.deleted_count:
        raise HTTPException(404, "User not found")
    await db.matches.delete_many({"$or": [{"user_a_id": user_id}, {"user_b_id": user_id}]})
    await db.messages.delete_many({"$or": [{"from_user_id": user_id}, {"to_user_id": user_id}]})
    await _bust_admin_caches()
    publish(TOPIC_USER_DELETED, {
        "event": "user.deleted", "user_id": user_id,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"ok": True, "deleted_user_id": user_id}


@router.post("/users/{user_id}/grant-admin")
async def grant_admin(user_id: str, _=Depends(current_admin)):
    r = await get_db().users.update_one({"_id": _as_object_id(user_id)}, {"$set": {"is_admin": True}})
    if not r.matched_count: raise HTTPException(404, "User not found")
    await _bust_admin_caches()
    publish(TOPIC_USER_ADMIN_CHANGED, {
        "event": "user.admin_changed", "user_id": user_id, "is_admin": True,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"ok": True}


@router.post("/users/{user_id}/revoke-admin")
async def revoke_admin(user_id: str, _=Depends(current_admin)):
    r = await get_db().users.update_one({"_id": _as_object_id(user_id)}, {"$set": {"is_admin": False}})
    if not r.matched_count: raise HTTPException(404, "User not found")
    await _bust_admin_caches()
    publish(TOPIC_USER_ADMIN_CHANGED, {
        "event": "user.admin_changed", "user_id": user_id, "is_admin": False,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"ok": True}


@router.get("/reports")
async def reports(_=Depends(current_admin), days: int = Query(30)):
    """Aggregated reports for admin: revenue, users trend, matches trend, top events."""
    cache_key = f"admin:reports:{days}"
    cached_res = await _cache_get(cache_key)
    if cached_res is not None:
        return cached_res
    db = get_db()
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    # Total revenue (approved donations only)
    rev_agg = await db.payments.aggregate([
        {"$match": {"status": "approved"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}, "count": {"$sum": 1}}},
    ]).to_list(1)
    total_revenue = rev_agg[0]["total"] if rev_agg else 0
    paid_count = rev_agg[0]["count"] if rev_agg else 0

    # Revenue per donation purpose
    per_purpose = []
    async for r in db.payments.aggregate([
        {"$match": {"status": "approved"}},
        {"$group": {"_id": "$purpose", "total": {"$sum": "$amount"}, "n": {"$sum": 1}}},
        {"$sort": {"total": -1}},
    ]):
        per_purpose.append({"purpose": r["_id"], "total": r["total"], "count": r["n"]})

    # Users per day (registrations)
    users_trend = []
    async for r in db.users.aggregate([
        {"$match": {"created_at": {"$gte": since}}},
        {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}}, "n": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]):
        users_trend.append({"date": r["_id"], "count": r["n"]})

    # Matches per day
    matches_trend = []
    async for r in db.matches.aggregate([
        {"$match": {"matched_at": {"$gte": since}}},
        {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$matched_at"}}, "n": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]):
        matches_trend.append({"date": r["_id"], "count": r["n"]})

    # Top pages (from page_views collection)
    top_pages = []
    async for r in db.page_views.aggregate([
        {"$match": {"visited_at": {"$gte": since}}},
        {"$group": {"_id": "$path", "n": {"$sum": 1}, "unique_users": {"$addToSet": "$user_id"}}},
        {"$sort": {"n": -1}},
        {"$limit": 20},
    ]):
        top_pages.append({"path": r["_id"], "views": r["n"], "unique_users": len(r["unique_users"])})

    # ── NUMBERS (namba halisi, siyo michoro tu) ──
    # Users kwa mkoa (kituo cha sasa) — kila mkoa iko na hesabu yake.
    users_by_region = []
    async for r in db.users.aggregate([
        {"$group": {"_id": {"region_id": "$current_station.region_id", "name": "$current_station.region_name"}, "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ]):
        users_by_region.append({"region_id": r["_id"]["region_id"], "region": r["_id"]["name"], "count": r["n"]})

    # Users kwa wilaya
    users_by_district = []
    async for r in db.users.aggregate([
        {"$group": {"_id": {"region": "$current_station.region_name", "district": "$current_station.district_name"},
                     "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ]):
        users_by_district.append({"region": r["_id"]["region"], "district": r["_id"]["district"], "count": r["n"]})

    # Users kwa kada
    users_by_cadre = []
    async for r in db.users.aggregate([
        {"$group": {"_id": {"cat": "$category", "cadre": "$cadre_code"}, "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ]):
        users_by_cadre.append({"category": r["_id"]["cat"], "cadre": r["_id"]["cadre"], "count": r["n"]})

    # Users kwa status
    users_by_status = []
    async for r in db.users.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}]):
        users_by_status.append({"status": r["_id"] or "unknown", "count": r["n"]})

    # Users kwa idara
    users_by_category = [{"category": "health", "count": await db.users.count_documents({"category": "health"})},
                         {"category": "education", "count": await db.users.count_documents({"category": "education"})}]

    result = {
        "period_days": days, "since": since.isoformat(),
        "revenue": {"total_tzs": total_revenue, "paid_count": paid_count, "per_purpose": per_purpose},
        "users_per_day": users_trend,
        "matches_per_day": matches_trend,
        "top_pages": top_pages,
        "users_by_region": users_by_region,
        "users_by_district": users_by_district,
        "users_by_cadre": users_by_cadre,
        "users_by_status": users_by_status,
        "users_by_category": users_by_category,
    }
    await _cache_set(cache_key, result, 30)
    return result


# ─── Data management (reference data: mikoa/wilaya/masomo/kada) ───────────

async def _bust_location_caches() -> None:
    """Futa Redis cache za locations (regions/districts/cadres/subjects) —
    vinginevyo mabadiliko hayaonekani kwa watumiaji hadi cache itakapoisha."""
    try:
        r = get_redis()
        keys = [k async for k in r.scan_iter("locations:*")]
        keys += [k async for k in r.scan_iter("cadres:*")]
        keys += [k async for k in r.scan_iter("subjects:*")]
        keys += [k async for k in r.scan_iter("admin:*")]
        if keys:
            await r.delete(*keys)
    except Exception:
        pass


class SubjectIn(BaseModel):
    code: str = Field(..., min_length=2, max_length=30)
    name: str = Field(..., min_length=2, max_length=120)
    level: str = Field("Secondary", pattern="^(Primary|Secondary)$")


class CadreIn(BaseModel):
    code: str = Field(..., min_length=2, max_length=30)
    category: Literal["health", "education"]
    display_name: str = Field(..., min_length=2, max_length=120)
    requires_subjects: bool = False
    level: str | None = Field(None, pattern="^(Primary|Secondary)$")


class RegionIn(BaseModel):
    id: int
    name: str = Field(..., min_length=2, max_length=80)


class DistrictIn(BaseModel):
    id: int
    region_id: int
    name: str = Field(..., min_length=2, max_length=80)


@router.get("/data/subjects")
async def data_subjects(_=Depends(current_admin), level: Optional[str] = None):
    q = {"level": level} if level else {}
    return [d async for d in get_db().subjects.find(q, {"_id": 0}).sort("name", 1)]


@router.post("/data/subjects")
async def data_subjects_add(body: SubjectIn, _=Depends(current_admin)):
    db = get_db()
    if await db.subjects.find_one({"code": body.code}):
        raise HTTPException(409, f"Somo '{body.code}' tayari lipo")
    await db.subjects.insert_one(body.model_dump())
    await _bust_location_caches()
    return {"ok": True, "subject": body.model_dump()}


@router.patch("/data/subjects/{code}")
async def data_subjects_update(code: str, body: SubjectIn, _=Depends(current_admin)):
    r = await get_db().subjects.update_one({"code": code}, {"$set": body.model_dump()})
    if not r.matched_count:
        raise HTTPException(404, "Somo halipo")
    await _bust_location_caches()
    return {"ok": True}


@router.delete("/data/subjects/{code}")
async def data_subjects_delete(code: str, _=Depends(current_admin)):
    r = await get_db().subjects.delete_one({"code": code})
    if not r.deleted_count:
        raise HTTPException(404, "Somo halipo")
    await _bust_location_caches()
    return {"ok": True, "deleted": code}


@router.get("/data/cadres")
async def data_cadres(_=Depends(current_admin), category: Optional[Literal["health", "education"]] = None):
    q = {"category": category} if category else {}
    return [d async for d in get_db().cadres.find(q, {"_id": 0}).sort("display_name", 1)]


@router.post("/data/cadres")
async def data_cadres_add(body: CadreIn, _=Depends(current_admin)):
    db = get_db()
    if await db.cadres.find_one({"code": body.code}):
        raise HTTPException(409, f"Kada '{body.code}' tayari ipo")
    await db.cadres.insert_one(body.model_dump())
    await _bust_location_caches()
    return {"ok": True, "cadre": body.model_dump()}


@router.patch("/data/cadres/{code}")
async def data_cadres_update(code: str, body: CadreIn, _=Depends(current_admin)):
    r = await get_db().cadres.update_one({"code": code}, {"$set": body.model_dump()})
    if not r.matched_count:
        raise HTTPException(404, "Kada haipo")
    await _bust_location_caches()
    return {"ok": True}


@router.delete("/data/cadres/{code}")
async def data_cadres_delete(code: str, _=Depends(current_admin)):
    r = await get_db().cadres.delete_one({"code": code})
    if not r.deleted_count:
        raise HTTPException(404, "Kada haipo")
    await _bust_location_caches()
    return {"ok": True, "deleted": code}


@router.get("/data/regions")
async def data_regions(_=Depends(current_admin)):
    return [d async for d in get_db().regions.find({}, {"_id": 0}).sort("name", 1)]


@router.post("/data/regions")
async def data_regions_add(body: RegionIn, _=Depends(current_admin)):
    db = get_db()
    if await db.regions.find_one({"id": body.id}):
        raise HTTPException(409, "Mkoa upo tayari (id inatumiwa)")
    await db.regions.insert_one(body.model_dump())
    await _bust_location_caches()
    return {"ok": True, "region": body.model_dump()}


@router.patch("/data/regions/{region_id}")
async def data_regions_update(region_id: int, body: RegionIn, _=Depends(current_admin)):
    r = await get_db().regions.update_one({"id": region_id}, {"$set": body.model_dump()})
    if not r.matched_count:
        raise HTTPException(404, "Mkoa haupo")
    await _bust_location_caches()
    return {"ok": True}


@router.delete("/data/regions/{region_id}")
async def data_regions_delete(region_id: int, _=Depends(current_admin)):
    r = await get_db().regions.delete_one({"id": region_id})
    if not r.deleted_count:
        raise HTTPException(404, "Mkoa haupo")
    await _bust_location_caches()
    return {"ok": True, "deleted": region_id}


@router.get("/data/districts")
async def data_districts(_=Depends(current_admin), region_id: Optional[int] = None):
    q = {"region_id": region_id} if region_id else {}
    return [d async for d in get_db().districts.find(q, {"_id": 0}).sort("name", 1)]


@router.post("/data/districts")
async def data_districts_add(body: DistrictIn, _=Depends(current_admin)):
    db = get_db()
    if await db.districts.find_one({"id": body.id}):
        raise HTTPException(409, "Wilaya ipo tayari (id inatumiwa)")
    await db.districts.insert_one(body.model_dump())
    await _bust_location_caches()
    return {"ok": True, "district": body.model_dump()}


@router.patch("/data/districts/{district_id}")
async def data_districts_update(district_id: int, body: DistrictIn, _=Depends(current_admin)):
    r = await get_db().districts.update_one({"id": district_id}, {"$set": body.model_dump()})
    if not r.matched_count:
        raise HTTPException(404, "Wilaya haipo")
    await _bust_location_caches()
    return {"ok": True}


@router.delete("/data/districts/{district_id}")
async def data_districts_delete(district_id: int, _=Depends(current_admin)):
    r = await get_db().districts.delete_one({"id": district_id})
    if not r.deleted_count:
        raise HTTPException(404, "Wilaya haipo")
    await _bust_location_caches()
    return {"ok": True, "deleted": district_id}


@router.get("/reports/export")
async def reports_export(_=Depends(current_admin), fmt: Literal["csv", "xlsx"] = "csv"):
    """Ripoti kamili ya NAMBA (users kwa mkoa/wilaya/kada/idara/status +
    michango) kama CSV/XLSX — inafunguka kwenye Excel moja kwa moja."""
    data = await reports(days=365)
    rows: list[list] = []
    rows.append(["RIPOTI — KUBADILISHANA VITUO (NAMBA HALISI)"])
    rows.append(["Siku", str(data["period_days"])])
    rows.append([])
    rows.append(["=== WATUMIAJI KWA MKOA ==="])
    rows.append(["Mkoa", "Hesabu"])
    for r in data["users_by_region"]:
        rows.append([r["region"] or "(bila mkoa)", r["count"]])
    rows.append([])
    rows.append(["=== WATUMIAJI KWA WILAYA ==="])
    rows.append(["Mkoa", "Wilaya", "Hesabu"])
    for r in data["users_by_district"]:
        rows.append([r["region"] or "", r["district"] or "", r["count"]])
    rows.append([])
    rows.append(["=== WATUMIAJI KWA KADA ==="])
    rows.append(["Idara", "Kada", "Hesabu"])
    for r in data["users_by_cadre"]:
        rows.append([r["category"], r["cadre"], r["count"]])
    rows.append([])
    rows.append(["=== WATUMIAJI KWA IDARA ==="])
    rows.append(["Idara", "Hesabu"])
    for r in data["users_by_category"]:
        rows.append([r["category"], r["count"]])
    rows.append([])
    rows.append(["=== WATUMIAJI KWA STATUS ==="])
    rows.append(["Status", "Hesabu"])
    for r in data["users_by_status"]:
        rows.append([r["status"], r["count"]])
    rows.append([])
    rows.append(["=== MICHANGO (REVENUE) ==="])
    rows.append(["Jumla (TZS)", data["revenue"]["total_tzs"]])
    rows.append(["Michango iliyokubaliwa", data["revenue"]["paid_count"]])
    rows.append(["Aina", "Kiasi (TZS)", "Idadi"]) if data["revenue"]["per_purpose"] else None
    for r in data["revenue"]["per_purpose"]:
        rows.append([r["purpose"], r["total"], r["count"]])
    rows.append([])
    rows.append(["=== WAJIBU WA KUJA KWAKO (matches) ==="])
    rows.append(["Siku", "Matches"])
    for r in data["matches_per_day"]:
        rows.append([r["date"], r["count"]])
    return _export_response(rows, fmt, "ripoti_na_hesabu")


# ─── Exports: events + reports (CSV opens in Excel; XLSX native) ────────────


def _csv_response(rows: list[list], filename: str) -> StreamingResponse:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerows(rows)
    return StreamingResponse(
        iter([buf.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _xlsx_bytes(rows: list[list]) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    for col in ws.columns:
        letter = col[0].column_letter
        ws.column_dimensions[letter].width = max(14, min(48, max((len(str(c.value or "")) for c in col), default=14) + 2))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _xlsx_response(rows: list[list], filename: str) -> StreamingResponse:
    return StreamingResponse(
        iter([_xlsx_bytes(rows)]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _export_response(rows: list[list], fmt: str, name: str):
    if fmt == "xlsx":
        return _xlsx_response(rows, f"{name}.xlsx")
    return _csv_response(rows, f"{name}.csv")


@router.get("/events/export")
async def events_export(_=Depends(current_admin),
                        event_type: Optional[str] = None, fmt: Literal["csv", "xlsx"] = "csv",
                        limit: int = Query(5000, le=20000)):
    """Download all events (filtered by type) as CSV/XLSX — logs za mfumo."""
    q = {"event_type": event_type} if event_type else {}
    rows = [["#", "Saa (UTC)", "Aina ya tukio", "Topic", "Aktor (user)", "Data (JSON)"]]
    async for e in get_db().event_log.find(q).sort("occurred_at", -1).limit(limit):
        actor = str(e.get("actor_user_id") or "") + " " + (e.get("actor_name") or "")
        rows.append([len(rows), e.get("occurred_at", ""), e.get("event_type", ""),
                     e.get("topic", ""), actor.strip(), json.dumps(e.get("payload"), ensure_ascii=False, default=str)])
    return _export_response(rows, fmt, "events_log")


@router.post("/events/clear")
async def events_clear(_=Depends(current_admin)):
    """Futa event_log yote (logs za mfumo) — data ya live haigusiwi."""
    r = await get_db().event_log.delete_many({})
    await _bust_admin_caches()
    return {"ok": True, "deleted": r.deleted_count}


@router.post("/cleanup-test-data")
async def cleanup_test_data(_=Depends(current_admin), max_delete: int = Query(500, le=2000)):
    """Futa akaunti za MAJARIBIO (data ya uwongo): majina yanayofanana na
    patterns za test/demo/import (#), namba za test, pamoja na event_log na
    page_views zao. ADMINI HAWAGUSIWI kamwe.

    Huu ni msaada wa haraka kwa admin — siyo script ya kawaida; kila akaunti
    inakaguliwa dhidi ya patterns kabla ya kufutwa."""
    db = get_db()
    # MAKINI: ni patterns za majaribio ZILIZOJULIKANA tu (jina linaloanza na
    # kitu hiki au lenye neno kamili) — usifute mtu halisi kwa kosa. `\b` pande
    # zote mbili: "Protest" HAIENDANI (test si neno kamili), "Test Debug" ndiyo.
    test_re = re.compile(
        r"\b(test|demo|debug|dummy|sample|zzz)\b|\b(CO|Mwalimu|RN|EN|ANO|NO|MA|ACO|CA|AMO|MD|LAB|PHARM|HA) — ",
        re.I,
    )
    test_phone = re.compile(r"^(\+?255)?0{2,}|^(\+?255)?70000")
    q = {"is_admin": {"$ne": True},
         "$or": [{"full_name": test_re}, {"phone_primary": test_phone}]}
    cur = db.users.find(q, {"full_name": 1, "phone_primary": 1, "created_at": 1}).sort("created_at", 1).limit(max_delete)
    ids = []
    async for u in cur:
        ids.append(str(u["_id"]))
    if not ids:
        return {"ok": True, "deleted_users": 0, "deleted_events": 0, "deleted_views": 0}
    from bson import ObjectId as _OID
    oids = [_OID(i) for i in ids]
    del_users = await db.users.delete_many({"_id": {"$in": oids}})
    del_events = await db.event_log.delete_many({"actor_user_id": {"$in": ids}})
    del_views = await db.page_views.delete_many({"user_id": {"$in": ids}})
    await db.matches.delete_many({"$or": [{"user_a_id": {"$in": ids}}, {"user_b_id": {"$in": ids}}]})
    await db.notifications.delete_many({"user_id": {"$in": ids}})
    await _bust_admin_caches()
    return {"ok": True, "deleted_users": del_users.deleted_count,
            "deleted_events": del_events.deleted_count, "deleted_views": del_views.deleted_count}


# ─── Email settings (admin-configurable — hakuna SSH/git inahitajika) ──────
# Mipangilio ya SMTP/MailerSend inahifadhiwa kwenye MongoDB (settings collection)
# na kusomwa na emailer.get_email_config(). Admin anaweza kuweka Gmail App
# Password yake kwenye /admin/settings na kuthibitisha kwa "Tuma Code ya Majaribio".


class EmailSettingsIn(BaseModel):
    smtp_host: str = ""
    smtp_port: int = Field(587, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_tls: bool = True
    mailersend_api_key: str = ""
    mailersend_from: str = ""
    enabled: bool = True


@router.get("/settings/email")
async def get_email_settings(_=Depends(current_admin)):
    doc = await get_db().settings.find_one({"key": "email"})
    base = {
        "configured": bool(doc and (doc.get("smtp_host") or doc.get("mailersend_api_key"))),
        "smtp_host": (doc or {}).get("smtp_host", ""),
        "smtp_port": (doc or {}).get("smtp_port", 587),
        "smtp_username": (doc or {}).get("smtp_username", ""),
        "smtp_password": "********" if (doc or {}).get("smtp_password") else "",
        "smtp_from": (doc or {}).get("smtp_from", settings.smtp_from),
        "smtp_use_tls": bool((doc or {}).get("smtp_use_tls", True)),
        "mailersend_api_key": "********" if (doc or {}).get("mailersend_api_key") else "",
        "mailersend_from": (doc or {}).get("mailersend_from", settings.mailersend_from),
        "enabled": bool((doc or {}).get("enabled", True)),
    }
    return base


@router.post("/settings/email")
async def save_email_settings(body: EmailSettingsIn, _=Depends(current_admin)):
    db = get_db()
    existing = await db.settings.find_one({"key": "email"}) or {}
    data = body.model_dump()
    # "********" = admin hakuandika upya — weka ile ya zamani (usifute kwa kosa).
    if data.get("smtp_password") == "********":
        data["smtp_password"] = existing.get("smtp_password", "")
    if data.get("mailersend_api_key") == "********":
        data["mailersend_api_key"] = existing.get("mailersend_api_key", "")
    data["updated_at"] = datetime.now(timezone.utc)
    await db.settings.update_one({"key": "email"}, {"$set": data}, upsert=True)
    await _bust_admin_caches()
    configured = bool(data.get("smtp_host") or data.get("mailersend_api_key"))
    return {"ok": True, "configured": configured,
            "message": "Mipangilio ya email imehifadhiwa ✓"}


@router.post("/settings/email/test")
async def test_email_settings(admin=Depends(current_admin)):
    """Tuma code ya majaribio kwa email ya admin mwenyewe — uthibitisho wa
    mipangilio end-to-end (kwa njia ya UI, hakuna SSH)."""
    admin_email = admin.get("email")
    if not admin_email:
        raise HTTPException(400, "Akaunti hii haina email — wasiliana na admin mwenzako.")
    cfg = await get_email_config()
    code = f"{secrets.randbelow(1_000_000):06d}"
    ok = await send_email(cfg, admin_email, "Code ya Majaribio — Kubadilishana Vituo",
                          "Ujumbe wa Majaribio ✅",
                          f"Habari {admin.get('full_name')}, email iko sawa! Weka code hii kuthibitisha: {code}.",
                          code)
    if not ok:
        raise HTTPException(400, "Email haikutumwa — kagua mipangilio (SMTP host, port, password). Code iko kwenye backend logs.")
    return {"ok": True, "message": f"Email ya majaribio imetumwa kwa {admin_email} — angalia Inbox na Spam."}


class PageViewIn(BaseModel):
    path: str
    referrer: str | None = None


@router.post("/page-view")
async def log_page_view(body: PageViewIn, user=Depends(current_user)):
    """Frontend calls this on route change. Persisted directly (reliable — the
    admin reports read `page_views`) AND published as an event for the audit
    stream / live consumers."""
    now = datetime.now(timezone.utc)
    await get_db().page_views.insert_one({
        "user_id": str(user["_id"]),
        "user_name": user["full_name"],
        "path": body.path,
        "referrer": body.referrer,
        "visited_at": now,
    })
    publish(TOPIC_PAGE_VIEWED, {
        "event": "page.viewed", "user_id": str(user["_id"]),
        "user_name": user["full_name"], "path": body.path,
        "referrer": body.referrer, "occurred_at": now.isoformat(),
    })
    return {"ok": True}


@router.get("/monitoring")
async def monitoring(_=Depends(current_admin)):
    """Snapshot of key backend metrics + broker config (redacted)."""
    db = get_db()
    return {
        "process_started_at": os.environ.get("_KV_STARTED_AT"),
        "mongo": {"uri_host": settings.mongo_uri.split("@")[-1].split("/")[0]},
        "mqtt": {"host": settings.mqtt_host, "port": settings.mqtt_port, "tls": settings.mqtt_use_tls,
                 "username_set": bool(settings.mqtt_username)},
        "redis_url": settings.redis_url,
        "collections": {
            "users": await db.users.count_documents({}),
            "matches": await db.matches.count_documents({}),
            "messages": await db.messages.count_documents({}),
            "call_logs": await db.call_logs.count_documents({}),
            "event_log": await db.event_log.count_documents({}),
        },
        "csv_dir": settings.csv_output_dir,
    }
