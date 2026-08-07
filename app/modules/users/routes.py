from datetime import datetime, timedelta, timezone
from typing import Optional
from bson import ObjectId
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, Query
from ...db import get_db
from ...security import current_user
from ...events.publisher import publish
from ...events.topics import (
    TOPIC_USER_PROFILE_UPDATED, TOPIC_USER_STATION_CHANGED, TOPIC_USER_DESTINATION_CHANGED,
)
from ..auth.schemas import StationInput, DestinationInput
from ..messaging.ws_manager import manager as ws_manager

router = APIRouter(prefix="/users", tags=["users"])


class UpdateProfileRequest(BaseModel):
    full_name: Optional[str] = Field(None, min_length=3, max_length=100)
    phone_alt: Optional[str] = None
    subjects: Optional[list[str]] = None


class UpdateStationRequest(BaseModel):
    current_station: StationInput


class UpdateDestinationsRequest(BaseModel):
    desired_destinations: list[DestinationInput] = Field(..., min_length=1, max_length=15)


class NotificationPrefs(BaseModel):
    new_matches: bool = True
    messages: bool = True


def _to_response(user: dict):
    return {
        "user_id": str(user["_id"]),
        "full_name": user["full_name"],
        "phone_primary": user["phone_primary"],
        "phone_alt": user.get("phone_alt"),
        "category": user["category"],
        "cadre_code": user["cadre_code"],
        "cadre_display": user.get("cadre_display", user["cadre_code"]),
        "subjects": user.get("subjects", []),
        "current_station": user["current_station"],
        "desired_destinations": user.get("desired_destinations", []),
        "notification_prefs": user.get("notification_prefs", {"new_matches": True, "messages": True}),
        "status": user.get("status", "active"),
        "is_verified": user.get("is_verified", False),
        "is_admin": user.get("is_admin", False),
    }


@router.get("/me")
async def get_me(user=Depends(current_user)):
    return _to_response(user)


@router.patch("/me")
async def update_me(body: UpdateProfileRequest, user=Depends(current_user)):
    updates = {}
    if body.full_name is not None:
        updates["full_name"] = body.full_name.strip()
    if body.phone_alt is not None:
        updates["phone_alt"] = body.phone_alt
    if body.subjects is not None:
        updates["subjects"] = list(dict.fromkeys(body.subjects))
    if updates:
        updates["updated_at"] = datetime.now(timezone.utc)
        await get_db().users.update_one({"_id": user["_id"]}, {"$set": updates})
        publish(TOPIC_USER_PROFILE_UPDATED, {
            "event": "user.profile_updated", "user_id": str(user["_id"]),
            "changed_fields": list(updates.keys()), "occurred_at": updates["updated_at"].isoformat(),
        })
    fresh = await get_db().users.find_one({"_id": user["_id"]})
    return _to_response(fresh)


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


@router.put("/me/notification-prefs")
async def update_prefs(prefs: NotificationPrefs, user=Depends(current_user)):
    await get_db().users.update_one(
        {"_id": user["_id"]},
        {"$set": {"notification_prefs": prefs.model_dump(), "updated_at": datetime.now(timezone.utc)}},
    )
    fresh = await get_db().users.find_one({"_id": user["_id"]})
    return _to_response(fresh)
