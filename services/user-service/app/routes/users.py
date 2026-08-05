from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from ..core.auth import current_user
from ..core.db import get_db
from ..events.publisher import (
    publish, TOPIC_PROFILE_UPDATED, TOPIC_DESTINATION_CHANGED, TOPIC_STATION_CHANGED
)
from ..models.user import (
    UpdateProfileRequest, UpdateStationRequest, UpdateDestinationsRequest,
    UserProfileResponse, NotificationPrefs,
)

router = APIRouter(prefix="/users", tags=["users"])


def _to_response(user: dict) -> UserProfileResponse:
    return UserProfileResponse(
        user_id=str(user["_id"]),
        full_name=user["full_name"],
        phone_primary=user["phone_primary"],
        phone_alt=user.get("phone_alt"),
        category=user["category"],
        cadre_code=user["cadre_code"],
        cadre_display=user.get("cadre_display", user["cadre_code"]),
        subjects=user.get("subjects", []),
        current_station=user["current_station"],
        desired_destinations=user.get("desired_destinations", []),
        notification_prefs=NotificationPrefs(**user.get("notification_prefs", {})),
        status=user.get("status", "active"),
        is_verified=user.get("is_verified", False),
    )


@router.get("/me", response_model=UserProfileResponse)
async def get_me(user=Depends(current_user)):
    return _to_response(user)


@router.patch("/me", response_model=UserProfileResponse)
async def update_me(body: UpdateProfileRequest, user=Depends(current_user)):
    updates: dict = {}
    if body.full_name is not None:
        updates["full_name"] = body.full_name.strip()
    if body.phone_alt is not None:
        updates["phone_alt"] = body.phone_alt
    if body.subjects is not None:
        updates["subjects"] = list(dict.fromkeys(body.subjects))

    if not updates:
        return _to_response(user)

    updates["updated_at"] = datetime.now(timezone.utc)
    await get_db().users.update_one({"_id": user["_id"]}, {"$set": updates})

    publish(TOPIC_PROFILE_UPDATED, {
        "event": "user.profile_updated",
        "user_id": str(user["_id"]),
        "changed_fields": list(updates.keys()),
        "occurred_at": updates["updated_at"].isoformat(),
    })

    fresh = await get_db().users.find_one({"_id": user["_id"]})
    return _to_response(fresh)


@router.put("/me/station", response_model=UserProfileResponse)
async def update_station(body: UpdateStationRequest, user=Depends(current_user)):
    now = datetime.now(timezone.utc)
    station = body.current_station.model_dump()
    await get_db().users.update_one(
        {"_id": user["_id"]},
        {"$set": {"current_station": station, "updated_at": now}},
    )
    publish(TOPIC_STATION_CHANGED, {
        "event": "user.station_changed",
        "user_id": str(user["_id"]),
        "current_station": station,
        "occurred_at": now.isoformat(),
    })
    fresh = await get_db().users.find_one({"_id": user["_id"]})
    return _to_response(fresh)


@router.put("/me/destinations", response_model=UserProfileResponse)
async def update_destinations(body: UpdateDestinationsRequest, user=Depends(current_user)):
    now = datetime.now(timezone.utc)
    dests = [d.model_dump() for d in body.desired_destinations]
    await get_db().users.update_one(
        {"_id": user["_id"]},
        {"$set": {"desired_destinations": dests, "updated_at": now}},
    )
    publish(TOPIC_DESTINATION_CHANGED, {
        "event": "user.destination_changed",
        "user_id": str(user["_id"]),
        "desired_destinations": dests,
        "occurred_at": now.isoformat(),
    })
    fresh = await get_db().users.find_one({"_id": user["_id"]})
    return _to_response(fresh)


@router.put("/me/notification-prefs", response_model=UserProfileResponse)
async def update_prefs(prefs: NotificationPrefs, user=Depends(current_user)):
    await get_db().users.update_one(
        {"_id": user["_id"]},
        {"$set": {"notification_prefs": prefs.model_dump(),
                  "updated_at": datetime.now(timezone.utc)}},
    )
    fresh = await get_db().users.find_one({"_id": user["_id"]})
    return _to_response(fresh)
