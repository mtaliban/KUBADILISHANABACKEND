from datetime import datetime, timedelta, timezone
from typing import Optional
from bson import ObjectId
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, Query
from ...db import get_db
from ...security import current_user, verify_password, hash_password, normalize_phone
from ...events.publisher import publish
from ...events.topics import (
    TOPIC_USER_PROFILE_UPDATED, TOPIC_USER_STATION_CHANGED, TOPIC_USER_DESTINATION_CHANGED,
    TOPIC_USER_PREFS_UPDATED,
)
from ..auth.schemas import StationInput, DestinationInput
from ..messaging.ws_manager import manager as ws_manager

router = APIRouter(prefix="/users", tags=["users"])


class UpdateProfileRequest(BaseModel):
    full_name: Optional[str] = Field(None, min_length=3, max_length=100)
    phone_alt: Optional[str] = None
    phone_primary: Optional[str] = None
    subjects: Optional[list[str]] = None
    current_station: Optional[StationInput] = None
    desired_destinations: Optional[list[DestinationInput]] = None


class UpdateStationRequest(BaseModel):
    current_station: StationInput


class UpdateDestinationsRequest(BaseModel):
    desired_destinations: list[DestinationInput] = Field(..., min_length=1, max_length=15)


class NotificationPrefs(BaseModel):
    new_matches: bool = True
    messages: bool = True


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=6)
    new_password: str = Field(..., min_length=6, max_length=128)


class FollowedRegionsRequest(BaseModel):
    """Mikoa ya chanzo ya kufuata kwa notifications/board (default: destination yako)."""
    region_ids: list[int] = Field(default_factory=list, max_length=31)


def _to_response(user: dict):
    return {
        "user_id": str(user["_id"]),
        "full_name": user["full_name"],
        "phone_primary": user["phone_primary"],
        "phone_alt": user.get("phone_alt"),
        "email": user.get("email"),
        "email_verified": user.get("email_verified", False),
        "category": user["category"],
        "cadre_code": user["cadre_code"],
        "cadre_display": user.get("cadre_display", user["cadre_code"]),
        "subjects": user.get("subjects", []),
        "current_station": user["current_station"],
        "desired_destinations": user.get("desired_destinations", []),
        "notification_prefs": user.get("notification_prefs", {"new_matches": True, "messages": True}),
        "followed_regions": user.get("followed_regions", []),
        "status": user.get("status", "active"),
        "is_verified": user.get("is_verified", False),
        "is_admin": user.get("is_admin", False),
    }


@router.get("/me")
async def get_me(user=Depends(current_user)):
    return _to_response(user)


@router.patch("/me")
async def update_me(body: UpdateProfileRequest, user=Depends(current_user)):
    """Update KAMILI ya wasifu: jina, simu, masomo, kituo, destinations —
    yote kwenye hatua moja (profile edit ya mtumiaji). Hakuna cache ya kale."""
    db = get_db()
    updates = {}
    if body.full_name is not None:
        updates["full_name"] = body.full_name.strip()
    if body.phone_alt is not None:
        updates["phone_alt"] = body.phone_alt
    if body.phone_primary is not None:
        try:
            phone = normalize_phone(body.phone_primary)
        except ValueError as e:
            raise HTTPException(422, str(e))
        # Ipo kwa mtu mwingine? (usiweze kuiba namba ya mtu)
        other = await db.users.find_one({
            "$or": [{"phone_primary": phone}, {"phone_alt": phone}],
            "_id": {"$ne": user["_id"]},
        }, {"_id": 1})
        if other:
            raise HTTPException(409, "Namba hii inatumiwa na akaunti nyingine")
        updates["phone_primary"] = phone
    if body.subjects is not None:
        updates["subjects"] = list(dict.fromkeys(body.subjects))
    if body.current_station is not None:
        updates["current_station"] = body.current_station.model_dump()
    if body.desired_destinations is not None:
        updates["desired_destinations"] = [d.model_dump() for d in body.desired_destinations]
    if updates:
        updates["updated_at"] = datetime.now(timezone.utc)
        await db.users.update_one({"_id": user["_id"]}, {"$set": updates})
        publish(TOPIC_USER_PROFILE_UPDATED, {
            "event": "user.profile_updated", "user_id": str(user["_id"]),
            "changed_fields": list(updates.keys()), "occurred_at": updates["updated_at"].isoformat(),
        })
    fresh = await db.users.find_one({"_id": user["_id"]})
    return _to_response(fresh)


@router.post("/me/password")
async def change_my_password(body: ChangePasswordRequest, user=Depends(current_user)):
    """Badilisha password ya mtumiaji mwenyewe (anahitaji password ya sasa)."""
    if not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(400, "Password ya sasa si sahihi")
    now = datetime.now(timezone.utc)
    await get_db().users.update_one(
        {"_id": user["_id"]},
        {"$set": {"password_hash": hash_password(body.new_password), "updated_at": now}},
    )
    publish(TOPIC_USER_PROFILE_UPDATED, {
        "event": "user.profile_updated", "user_id": str(user["_id"]),
        "changed_fields": ["password_hash"], "occurred_at": now.isoformat(),
    })
    return {"ok": True, "message": "Password imebadilishwa ✓"}


@router.put("/me/station")
async def update_station(body: UpdateStationRequest, user=Depends(current_user)):
    now = datetime.now(timezone.utc)
    station = body.current_station.model_dump()
    await get_db().users.update_one({"_id": user["_id"]}, {"$set": {"current_station": station, "updated_at": now}})
    publish(TOPIC_USER_STATION_CHANGED, {
        "event": "user.station_changed", "user_id": str(user["_id"]),
        "current_station": station, "occurred_at": now.isoformat(),
    })
    fresh = await get_db().users.find_one({"_id": user["_id"]})
    return _to_response(fresh)


@router.put("/me/destinations")
async def update_destinations(body: UpdateDestinationsRequest, user=Depends(current_user)):
    now = datetime.now(timezone.utc)
    dests = [d.model_dump() for d in body.desired_destinations]
    await get_db().users.update_one({"_id": user["_id"]}, {"$set": {"desired_destinations": dests, "updated_at": now}})
    publish(TOPIC_USER_DESTINATION_CHANGED, {
        "event": "user.destination_changed", "user_id": str(user["_id"]),
        "desired_destinations": dests, "occurred_at": now.isoformat(),
    })
    fresh = await get_db().users.find_one({"_id": user["_id"]})
    return _to_response(fresh)


@router.get("/recent")
async def recent_users(_=Depends(current_user), limit: int = Query(15, le=50)):
    """Waliyosajiliwa hivi karibuni — kwa dashboard request feed (Uber-style)."""
    db = get_db()
    cur = db.users.find(
        {"status": "active"},
        {"full_name": 1, "phone_primary": 1, "cadre_display": 1, "category": 1,
         "cadre_code": 1, "current_station": 1, "desired_destinations": 1,
         "created_at": 1, "last_seen_at": 1, "is_admin": 1},
    ).sort("created_at", -1).limit(limit)
    out = []
    async for u in cur:
        if u.get("is_admin"):
            continue
        out.append({
            "user_id": str(u["_id"]), "full_name": u["full_name"],
            "phone_primary": u["phone_primary"], "cadre_display": u.get("cadre_display"),
            "cadre_code": u.get("cadre_code"), "category": u["category"],
            "current_station": u.get("current_station"),
            "desired_destinations": u.get("desired_destinations", []),
            "created_at": u.get("created_at"), "last_seen_at": u.get("last_seen_at"),
            "online": ws_manager.is_online(str(u["_id"])),
        })
    return {"count": len(out), "users": out}


@router.get("/online")
async def online_users(_=Depends(current_user), limit: int = Query(200, le=1000)):
    """List currently online users (WebSocket-connected) with profile summary."""
    db = get_db()
    ids = ws_manager.online_users()[:limit]
    if not ids:
        return {"count": 0, "users": []}
    obj_ids = [ObjectId(i) for i in ids]
    cursor = db.users.find(
        {"_id": {"$in": obj_ids}},
        {"full_name": 1, "phone_primary": 1, "cadre_display": 1, "category": 1,
         "current_station": 1, "last_seen_at": 1, "is_admin": 1},
    )
    out = []
    async for u in cursor:
        out.append({
            "user_id": str(u["_id"]), "full_name": u["full_name"],
            "phone_primary": u["phone_primary"], "cadre_display": u.get("cadre_display"),
            "category": u["category"], "current_station": u.get("current_station"),
            "last_seen_at": u.get("last_seen_at"), "is_admin": u.get("is_admin", False),
        })
    return {"count": len(out), "users": out}


@router.get("/recently-active")
async def recently_active(_=Depends(current_user), minutes: int = Query(60), limit: int = Query(100, le=500)):
    """Users active within last N minutes (based on last_seen_at)."""
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    cursor = get_db().users.find(
        {"last_seen_at": {"$gte": since}},
        {"full_name": 1, "phone_primary": 1, "cadre_display": 1, "category": 1,
         "current_station": 1, "last_seen_at": 1},
    ).sort("last_seen_at", -1).limit(limit)
    out = []
    async for u in cursor:
        out.append({
            "user_id": str(u["_id"]), "full_name": u["full_name"],
            "phone_primary": u["phone_primary"], "cadre_display": u.get("cadre_display"),
            "category": u["category"], "current_station": u.get("current_station"),
            "last_seen_at": u.get("last_seen_at"),
            "online": ws_manager.is_online(str(u["_id"])),
        })
    return {"count": len(out), "users": out}


@router.get("/{user_id}")
async def get_user_public(user_id: str, _=Depends(current_user)):
    """Public-safe profile of any user (used by chat page to fetch phone/name)."""
    try:
        oid = ObjectId(user_id)
    except Exception:
        raise HTTPException(400, "Invalid user_id")
    u = await get_db().users.find_one({"_id": oid}, {"password_hash": 0})
    if not u:
        raise HTTPException(404, "User not found")
    return {
        "user_id": str(u["_id"]), "full_name": u["full_name"],
        "phone_primary": u["phone_primary"], "phone_alt": u.get("phone_alt"),
        "category": u["category"], "cadre_code": u["cadre_code"],
        "cadre_display": u.get("cadre_display"), "subjects": u.get("subjects", []),
        "current_station": u["current_station"],
        "desired_destinations": u.get("desired_destinations", []),
        "last_seen_at": u.get("last_seen_at"),
        "online": ws_manager.is_online(user_id),
    }


@router.get("/me/followed-regions")
async def get_followed_regions(user=Depends(current_user)):
    """Mikoa ya chanzo user anayofuata (kwa board default + notifications)."""
    return {"region_ids": user.get("followed_regions", [])}


@router.put("/me/followed-regions")
async def set_followed_regions(body: FollowedRegionsRequest, user=Depends(current_user)):
    now = datetime.now(timezone.utc)
    ids = list(dict.fromkeys(body.region_ids))
    await get_db().users.update_one(
        {"_id": user["_id"]},
        {"$set": {"followed_regions": ids, "updated_at": now}},
    )
    publish(TOPIC_USER_PREFS_UPDATED, {
        "event": "user.prefs_updated", "user_id": str(user["_id"]),
        "followed_regions": ids, "occurred_at": now.isoformat(),
    })
    return {"region_ids": ids}


@router.put("/me/notification-prefs")
async def update_prefs(prefs: NotificationPrefs, user=Depends(current_user)):
    now = datetime.now(timezone.utc)
    prefs_dump = prefs.model_dump()
    await get_db().users.update_one(
        {"_id": user["_id"]},
        {"$set": {"notification_prefs": prefs_dump, "updated_at": now}},
    )
    publish(TOPIC_USER_PREFS_UPDATED, {
        "event": "user.prefs_updated", "user_id": str(user["_id"]),
        "notification_prefs": prefs_dump, "occurred_at": now.isoformat(),
    })
    fresh = await get_db().users.find_one({"_id": user["_id"]})
    return _to_response(fresh)
