import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Literal
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from ...config import settings
from ...db import get_db
from ...events.publisher import publish
from ...events.topics import (
    TOPIC_USER_UPDATED_BY_ADMIN, TOPIC_USER_DELETED, TOPIC_USER_ADMIN_CHANGED, TOPIC_PAGE_VIEWED,
)
from ...security import current_admin, current_user, _is_valid_object_id

router = APIRouter(prefix="/admin", tags=["admin"])


def _escape_regex(q: str) -> str:
    return re.escape(q)


def _as_object_id(user_id: str) -> ObjectId:
    if not _is_valid_object_id(user_id):
        raise HTTPException(400, "Invalid user_id")
    return ObjectId(user_id)


@router.get("/stats")
async def stats(_=Depends(current_admin)):
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

    return {
        "totals": {"users": users_total, "users_health": users_health, "users_education": users_edu,
                   "users_verified": users_verified, "users_active_7d": users_active_7d,
                   "matches": matches_total, "matches_24h": matches_24h,
                   "events": events_total, "events_24h": events_24h,
                   "messages": msgs_total, "calls": calls_total},
        "by_cadre": by_cadre, "by_region": by_region, "events_by_type": events_by_type,
    }


@router.get("/users")
async def list_users(_=Depends(current_admin),
                     category: Optional[Literal["health", "education"]] = None,
                     cadre_code: Optional[str] = None, region_id: Optional[int] = None,
                     q: Optional[str] = None, limit: int = Query(100, le=500), skip: int = Query(0, ge=0)):
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
    return {"total": total, "skip": skip, "limit": limit, "users": users}


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
async def list_events(_=Depends(current_admin), event_type: Optional[str] = None, limit: int = Query(100, le=500)):
    db = get_db(); q = {"event_type": event_type} if event_type else {}
    total = await db.event_log.count_documents(q)
    cur = db.event_log.find(q).sort("occurred_at", -1).limit(limit)
    events = []
    async for e in cur:
        e["_id"] = str(e["_id"]); events.append(e)
    return {"total": total, "events": events}


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


from pydantic import BaseModel, Field
from ...security import hash_password


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
    publish(TOPIC_USER_DELETED, {
        "event": "user.deleted", "user_id": user_id,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"ok": True, "deleted_user_id": user_id}


@router.post("/users/{user_id}/grant-admin")
async def grant_admin(user_id: str, _=Depends(current_admin)):
    r = await get_db().users.update_one({"_id": _as_object_id(user_id)}, {"$set": {"is_admin": True}})
    if not r.matched_count: raise HTTPException(404, "User not found")
    publish(TOPIC_USER_ADMIN_CHANGED, {
        "event": "user.admin_changed", "user_id": user_id, "is_admin": True,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"ok": True}


@router.post("/users/{user_id}/revoke-admin")
async def revoke_admin(user_id: str, _=Depends(current_admin)):
    r = await get_db().users.update_one({"_id": _as_object_id(user_id)}, {"$set": {"is_admin": False}})
    if not r.matched_count: raise HTTPException(404, "User not found")
    publish(TOPIC_USER_ADMIN_CHANGED, {
        "event": "user.admin_changed", "user_id": user_id, "is_admin": False,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"ok": True}


@router.get("/reports")
async def reports(_=Depends(current_admin), days: int = Query(30)):
    """Aggregated reports for admin: revenue, users trend, matches trend, top events."""
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

    return {
        "period_days": days, "since": since.isoformat(),
        "revenue": {"total_tzs": total_revenue, "paid_count": paid_count, "per_purpose": per_purpose},
        "users_per_day": users_trend,
        "matches_per_day": matches_trend,
        "top_pages": top_pages,
    }


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
