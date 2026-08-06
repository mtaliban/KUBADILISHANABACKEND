from datetime import datetime, timezone
from typing import Optional
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from ...db import get_db
from ...security import current_user, user_id_from_token
from ...events.publisher import publish
from ...events.topics import TOPIC_MESSAGE_SENT, TOPIC_CALL_INITIATED
from .ws_manager import manager

router = APIRouter(prefix="/messages", tags=["messages"])
ws_router = APIRouter()


class SendMessageRequest(BaseModel):
    to_user_id: str
    text: str = Field(..., min_length=1, max_length=2000)


class CallLogRequest(BaseModel):
    to_user_id: str
    outcome: str = Field("initiated", pattern="^(initiated|answered|missed)$")


def _conv_id(a, b): return "_".join(sorted([a, b]))


@router.post("")
async def send_message(body: SendMessageRequest, user=Depends(current_user)):
    db = get_db()
    from_id = str(user["_id"]); to_id = body.to_user_id
    recipient = await db.users.find_one({"_id": ObjectId(to_id)}, {"_id": 1, "full_name": 1})
    if not recipient: raise HTTPException(404, "Recipient not found")
    now = datetime.now(timezone.utc); conv = _conv_id(from_id, to_id)
    doc = {"conversation_id": conv, "from_user_id": from_id, "to_user_id": to_id,
           "text": body.text.strip(), "is_read": False, "created_at": now,
           "delivered_at": None, "read_at": None}
    res = await db.messages.insert_one(doc); doc["_id"] = str(res.inserted_id)
    await db.conversations.update_one(
        {"_id": conv},
        {"$set": {"_id": conv, "participants": sorted([from_id, to_id]),
                  "last_message_at": now, "last_message_text": body.text[:120],
                  "last_message_from": from_id},
         "$inc": {f"unread_count.{to_id}": 1}}, upsert=True,
    )
    payload = {"event": "message.sent", "message_id": doc["_id"], "conversation_id": conv,
               "from_user_id": from_id, "from_full_name": user["full_name"],
               "to_user_id": to_id, "text": doc["text"], "created_at": now.isoformat()}
    delivered = await manager.send_to_user(to_id, payload)
    if delivered:
        await db.messages.update_one({"_id": res.inserted_id}, {"$set": {"delivered_at": now}})
        payload["delivered_at"] = now.isoformat()
    publish(f"{TOPIC_MESSAGE_SENT}/{to_id}", payload)
    return payload


@router.get("/conversations")
async def list_conversations(user=Depends(current_user), limit: int = Query(100, le=500)):
    db = get_db(); uid = str(user["_id"])
    cur = db.conversations.find({"participants": uid}).sort("last_message_at", -1).limit(limit)
    out = []
    async for c in cur:
        other_id = next((p for p in c["participants"] if p != uid), None)
        if not other_id: continue
        other = await db.users.find_one({"_id": ObjectId(other_id)}, {"full_name": 1, "phone_primary": 1, "cadre_display": 1, "current_station": 1})
        out.append({
            "conversation_id": c["_id"], "with_user_id": other_id,
            "with_full_name": other["full_name"] if other else "Mtumiaji",
            "with_phone": other["phone_primary"] if other else None,
            "with_cadre": (other or {}).get("cadre_display"),
            "with_station": (other or {}).get("current_station"),
            "last_message_at": c.get("last_message_at"),
            "last_message_text": c.get("last_message_text"),
            "last_message_from": c.get("last_message_from"),
            "unread": (c.get("unread_count") or {}).get(uid, 0),
        })
    return out


@router.get("/with/{other_user_id}")
async def chat_history(other_user_id: str, user=Depends(current_user), limit: int = Query(100, le=500)):
    db = get_db(); uid = str(user["_id"]); conv = _conv_id(uid, other_user_id)
    cur = db.messages.find({"conversation_id": conv}).sort("created_at", 1).limit(limit)
    msgs = []
    async for m in cur:
        msgs.append({"message_id": str(m["_id"]), "from_user_id": m["from_user_id"],
                     "to_user_id": m["to_user_id"], "text": m["text"],
                     "created_at": m["created_at"], "is_read": m.get("is_read", False)})
    await db.messages.update_many({"conversation_id": conv, "to_user_id": uid, "is_read": False},
                                  {"$set": {"is_read": True, "read_at": datetime.now(timezone.utc)}})
    await db.conversations.update_one({"_id": conv}, {"$set": {f"unread_count.{uid}": 0}})
    return {"conversation_id": conv, "messages": msgs}


@router.post("/mark-read/{other_user_id}")
async def mark_read(other_user_id: str, user=Depends(current_user)):
    db = get_db(); uid = str(user["_id"]); conv = _conv_id(uid, other_user_id)
    await db.messages.update_many({"conversation_id": conv, "to_user_id": uid, "is_read": False},
                                  {"$set": {"is_read": True, "read_at": datetime.now(timezone.utc)}})
    await db.conversations.update_one({"_id": conv}, {"$set": {f"unread_count.{uid}": 0}})
    return {"ok": True}


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
        other = await db.users.find_one({"_id": ObjectId(other_id)}, {"full_name": 1, "phone_primary": 1})
        calls.append({"call_id": str(c["_id"]), "direction": "out" if c["from_user_id"] == uid else "in",
                      "with_user_id": other_id,
                      "with_full_name": other["full_name"] if other else "Mtumiaji",
                      "with_phone": other["phone_primary"] if other else None,
                      "status": c.get("status"), "initiated_at": c.get("initiated_at")})
    return calls


@router.get("/contacts")
async def my_contacts(user=Depends(current_user)):
    db = get_db(); uid = str(user["_id"]); others = {}
    async for c in db.conversations.find({"participants": uid}):
        oid = next((p for p in c["participants"] if p != uid), None)
        if oid: others.setdefault(oid, {})["last_message_at"] = c.get("last_message_at")
    async for c in db.call_logs.find({"$or": [{"from_user_id": uid}, {"to_user_id": uid}]}):
        oid = c["to_user_id"] if c["from_user_id"] == uid else c["from_user_id"]
        others.setdefault(oid, {})["last_call_at"] = c.get("initiated_at")
    out = []
    for oid, meta in others.items():
        u = await db.users.find_one({"_id": ObjectId(oid)}, {"full_name": 1, "phone_primary": 1, "cadre_display": 1, "current_station": 1})
        if not u: continue
        out.append({"user_id": oid, "full_name": u["full_name"], "phone_primary": u["phone_primary"],
                    "cadre_display": u.get("cadre_display"), "current_station": u.get("current_station"),
                    "last_message_at": meta.get("last_message_at"), "last_call_at": meta.get("last_call_at")})
    out.sort(key=lambda x: (x.get("last_message_at") or x.get("last_call_at") or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return out


@router.get("/presence")
async def presence(user=Depends(current_user)):
    """Who is currently online? (WebSocket-connected right now)"""
    return {"online_user_ids": manager.online_users(), "count": len(manager.online_users())}


@router.get("/presence/{user_id}")
async def check_presence(user_id: str, user=Depends(current_user)):
    """Is a specific user online? Returns online + last_seen_at."""
    db = get_db()
    u = await db.users.find_one({"_id": ObjectId(user_id)}, {"last_seen_at": 1})
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
    await get_db().users.update_one({"_id": ObjectId(uid)}, {"$set": {"last_seen_at": now, "is_online": True}})

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
                    {"_id": ObjectId(uid)},
                    {"$set": {"last_seen_at": datetime.now(timezone.utc)}},
                )
    except WebSocketDisconnect:
        manager.disconnect(uid, ws)
        await get_db().users.update_one(
            {"_id": ObjectId(uid)},
            {"$set": {"last_seen_at": datetime.now(timezone.utc), "is_online": False}},
        )
