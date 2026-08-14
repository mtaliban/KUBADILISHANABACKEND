from datetime import datetime, timezone
from typing import Optional
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from ...db import get_db
from ...security import current_user, user_id_from_token
from ...events.publisher import publish
from ...events.topics import TOPIC_CALL_INITIATED, TOPIC_USER_PRESENCE
from .ws_manager import manager

router = APIRouter(prefix="/messages", tags=["messages"])
ws_router = APIRouter()


def _safe_oid(value: str):
    """Parse a Mongo ObjectId from user input, else 400 (no unhandled 500)."""
    try:
        return ObjectId(value)
    except Exception:
        raise HTTPException(400, "Invalid ID")


def _try_oid(value: str):
    """Parse an ObjectId from DB-derived data; None if malformed (skip entry)."""
    try:
        return ObjectId(value)
    except Exception:
        return None


class CallLogRequest(BaseModel):
    to_user_id: str
    outcome: str = Field("initiated", pattern="^(initiated|answered|missed)$")


@router.post("/call")
async def log_call(body: CallLogRequest, user=Depends(current_user)):
    db = get_db(); from_id = str(user["_id"]); now = datetime.now(timezone.utc)
    doc = {"from_user_id": from_id, "to_user_id": body.to_user_id, "initiated_at": now, "status": body.outcome}
    res = await db.call_logs.insert_one(doc)
    publish(f"{TOPIC_CALL_INITIATED}/{body.to_user_id}", {
        "event": "call.initiated", "call_id": str(res.inserted_id),
        "from_user_id": from_id, "from_full_name": user["full_name"],
        "to_user_id": body.to_user_id, "initiated_at": now.isoformat(),
    })
    return {"call_id": str(res.inserted_id), "initiated_at": now.isoformat()}


@router.get("/calls")
async def my_call_history(user=Depends(current_user), limit: int = Query(50, le=200)):
    db = get_db(); uid = str(user["_id"])
    cur = db.call_logs.find({"$or": [{"from_user_id": uid}, {"to_user_id": uid}]}).sort("initiated_at", -1).limit(limit)
    calls = []
    async for c in cur:
        other_id = c["to_user_id"] if c["from_user_id"] == uid else c["from_user_id"]
        other = await db.users.find_one({"_id": _try_oid(other_id)}, {"full_name": 1, "phone_primary": 1})
        calls.append({"call_id": str(c["_id"]), "direction": "out" if c["from_user_id"] == uid else "in",
                      "with_user_id": other_id,
                      "with_full_name": other["full_name"] if other else "Mtumiaji",
                      "with_phone": other["phone_primary"] if other else None,
                      "status": c.get("status"), "initiated_at": c.get("initiated_at")})
    return calls


@router.get("/presence")
async def presence(user=Depends(current_user)):
    """Who is currently online? (WebSocket-connected right now)"""
    return {"online_user_ids": manager.online_users(), "count": len(manager.online_users())}


@router.get("/presence/{user_id}")
async def check_presence(user_id: str, user=Depends(current_user)):
    """Is a specific user online? Returns online + last_seen_at."""
    db = get_db()
    u = await db.users.find_one({"_id": _safe_oid(user_id)}, {"last_seen_at": 1})
    return {"user_id": user_id, "online": manager.is_online(user_id),
            "last_seen_at": (u or {}).get("last_seen_at")}


@ws_router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = Query(...)):
    """
    Client message types:
      - {type: 'ping'} → server replies {type: 'pong'}
      - {type: 'typing', to: userId, on: true|false} → server relays to recipient
      - {type: 'presence_ping'}   → server updates last_seen_at
    Server may push at any time:
      - {event: 'message.sent', ...}     (chat)
      - {event: 'match.found', ...}      (live matches, Uber-style)
      - {event: 'typing', from: id, on: true|false}
      - {event: 'presence', user_id, online: true|false}
      - {event: 'call.initiated', ...}
    """
    uid = user_id_from_token(token)
    if not uid:
        await ws.close(code=4401); return
    await manager.connect(uid, ws)

    # Announce online + update DB last_seen
    now = datetime.now(timezone.utc)
    await get_db().users.update_one({"_id": _safe_oid(uid)}, {"$set": {"last_seen_at": now, "is_online": True}})
    publish(TOPIC_USER_PRESENCE, {
        "event": "user.presence", "user_id": uid, "is_online": True,
        "occurred_at": now.isoformat(),
    })
    # Fanout presence to every other connected user so online status is genuinely live
    await manager.broadcast({"event": "presence", "user_id": uid, "online": True})

    try:
        while True:
            data = await ws.receive_json()
            t = data.get("type")

            if t == "ping":
                await ws.send_json({"type": "pong"})

            elif t == "typing":
                to_id = data.get("to")
                on = bool(data.get("on"))
                if to_id:
                    await manager.send_to_user(to_id, {
                        "event": "typing", "from_user_id": uid, "on": on,
                    })

            elif t == "presence_ping":
                await get_db().users.update_one(
                    {"_id": _safe_oid(uid)},
                    {"$set": {"last_seen_at": datetime.now(timezone.utc)}},
                )
    except WebSocketDisconnect:
        manager.disconnect(uid, ws)
        # Only mark offline if no other connection (e.g. another tab) remains
        still_online = manager.is_online(uid)
        if not still_online:
            await get_db().users.update_one(
                {"_id": _safe_oid(uid)},
                {"$set": {"last_seen_at": datetime.now(timezone.utc), "is_online": False}},
            )
            publish(TOPIC_USER_PRESENCE, {
                "event": "user.presence", "user_id": uid, "is_online": False,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
            })
            # Fanout offline to every connected user too
            await manager.broadcast({"event": "presence", "user_id": uid, "online": False})
