from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from ..core.auth import current_user
from ..core.db import get_db
from ..events.publisher import publish, TOPIC_MESSAGE_SENT, TOPIC_CALL_INITIATED
from ..models.message import SendMessageRequest, CallLogRequest
from ..ws_manager import manager

router = APIRouter(prefix="/messages", tags=["messages"])


def _conv_id(a: str, b: str) -> str:
    return "_".join(sorted([a, b]))


@router.post("")
async def send_message(body: SendMessageRequest, user=Depends(current_user)):
    db = get_db()
    from_id = str(user["_id"])
    to_id = body.to_user_id

    recipient = await db.users.find_one({"_id": ObjectId(to_id)}, {"_id": 1, "full_name": 1})
    if not recipient:
        raise HTTPException(404, "Recipient not found")

    now = datetime.now(timezone.utc)
    conv_id = _conv_id(from_id, to_id)
    doc = {
        "conversation_id": conv_id,
        "from_user_id": from_id,
        "to_user_id": to_id,
        "text": body.text.strip(),
        "is_read": False,
        "created_at": now,
        "delivered_at": None,
        "read_at": None,
    }
    result = await db.messages.insert_one(doc)
    doc["_id"] = str(result.inserted_id)

    # bump conversation summary
    await db.conversations.update_one(
        {"_id": conv_id},
        {"$set": {
            "_id": conv_id,
            "participants": sorted([from_id, to_id]),
            "last_message_at": now,
            "last_message_text": body.text[:120],
            "last_message_from": from_id,
        }, "$inc": {f"unread_count.{to_id}": 1}},
        upsert=True,
    )

    payload = {
        "event": "message.sent",
        "message_id": doc["_id"],
        "conversation_id": conv_id,
        "from_user_id": from_id,
        "from_full_name": user["full_name"],
        "to_user_id": to_id,
        "text": doc["text"],
        "created_at": now.isoformat(),
    }

    # 1) WebSocket delivery
    delivered = await manager.send_to_user(to_id, payload)
    if delivered:
        await db.messages.update_one({"_id": result.inserted_id}, {"$set": {"delivered_at": now}})
        payload["delivered_at"] = now.isoformat()

    # 2) MQTT event (analytics + fanout)
    publish(f"{TOPIC_MESSAGE_SENT}/{to_id}", payload)

    return payload


@router.get("/conversations")
async def list_conversations(user=Depends(current_user), limit: int = Query(100, le=500)):
    """WhatsApp-like inbox list."""
    db = get_db()
    uid = str(user["_id"])
    cursor = db.conversations.find({"participants": uid}).sort("last_message_at", -1).limit(limit)
    convs = []
    async for c in cursor:
        other_id = next((p for p in c["participants"] if p != uid), None)
        if not other_id:
            continue
        other = await db.users.find_one({"_id": ObjectId(other_id)}, {"full_name": 1, "phone_primary": 1, "cadre_display": 1, "current_station": 1})
        convs.append({
            "conversation_id": c["_id"],
            "with_user_id": other_id,
            "with_full_name": other["full_name"] if other else "Mtumiaji",
            "with_phone": other["phone_primary"] if other else None,
            "with_cadre": (other or {}).get("cadre_display"),
            "with_station": (other or {}).get("current_station"),
            "last_message_at": c.get("last_message_at"),
            "last_message_text": c.get("last_message_text"),
            "last_message_from": c.get("last_message_from"),
            "unread": (c.get("unread_count") or {}).get(uid, 0),
        })
    return convs


@router.get("/with/{other_user_id}")
async def chat_history(other_user_id: str, user=Depends(current_user), limit: int = Query(100, le=500)):
    db = get_db()
    uid = str(user["_id"])
    conv = _conv_id(uid, other_user_id)
    cursor = db.messages.find({"conversation_id": conv}).sort("created_at", 1).limit(limit)
    msgs = []
    async for m in cursor:
        msgs.append({
            "message_id": str(m["_id"]),
            "from_user_id": m["from_user_id"],
            "to_user_id": m["to_user_id"],
            "text": m["text"],
            "created_at": m["created_at"],
            "is_read": m.get("is_read", False),
        })
    # mark as read for me
    await db.messages.update_many(
        {"conversation_id": conv, "to_user_id": uid, "is_read": False},
        {"$set": {"is_read": True, "read_at": datetime.now(timezone.utc)}},
    )
    await db.conversations.update_one({"_id": conv}, {"$set": {f"unread_count.{uid}": 0}})
    return {"conversation_id": conv, "messages": msgs}


@router.post("/mark-read/{other_user_id}")
async def mark_read(other_user_id: str, user=Depends(current_user)):
    db = get_db()
    uid = str(user["_id"])
    conv = _conv_id(uid, other_user_id)
    await db.messages.update_many(
        {"conversation_id": conv, "to_user_id": uid, "is_read": False},
        {"$set": {"is_read": True, "read_at": datetime.now(timezone.utc)}},
    )
    await db.conversations.update_one({"_id": conv}, {"$set": {f"unread_count.{uid}": 0}})
    return {"ok": True}


@router.post("/call")
async def log_call(body: CallLogRequest, user=Depends(current_user)):
    """Record that user tapped 'Call' — the actual call happens via `tel:` on the device."""
    db = get_db()
    from_id = str(user["_id"])
    now = datetime.now(timezone.utc)
    doc = {
        "from_user_id": from_id,
        "to_user_id": body.to_user_id,
        "initiated_at": now,
        "status": body.outcome,
    }
    result = await db.call_logs.insert_one(doc)
    publish(f"{TOPIC_CALL_INITIATED}/{body.to_user_id}", {
        "event": "call.initiated",
        "call_id": str(result.inserted_id),
        "from_user_id": from_id,
        "from_full_name": user["full_name"],
        "to_user_id": body.to_user_id,
        "initiated_at": now.isoformat(),
    })
    return {"call_id": str(result.inserted_id), "initiated_at": now.isoformat()}


@router.get("/calls")
async def my_call_history(user=Depends(current_user), limit: int = Query(50, le=200)):
    db = get_db()
    uid = str(user["_id"])
    cursor = db.call_logs.find({"$or": [{"from_user_id": uid}, {"to_user_id": uid}]}).sort("initiated_at", -1).limit(limit)
    calls = []
    async for c in cursor:
        other_id = c["to_user_id"] if c["from_user_id"] == uid else c["from_user_id"]
        other = await db.users.find_one({"_id": ObjectId(other_id)}, {"full_name": 1, "phone_primary": 1})
        calls.append({
            "call_id": str(c["_id"]),
            "direction": "out" if c["from_user_id"] == uid else "in",
            "with_user_id": other_id,
            "with_full_name": other["full_name"] if other else "Mtumiaji",
            "with_phone": other["phone_primary"] if other else None,
            "status": c.get("status"),
            "initiated_at": c.get("initiated_at"),
        })
    return calls


@router.get("/contacts")
async def my_contacts(user=Depends(current_user)):
    """
    Everyone I've ever chatted with or called — WhatsApp Contacts-style.
    Merges conversation participants + call log participants.
    """
    db = get_db()
    uid = str(user["_id"])
    others: dict = {}
    async for c in db.conversations.find({"participants": uid}):
        other_id = next((p for p in c["participants"] if p != uid), None)
        if other_id:
            others.setdefault(other_id, {})["last_message_at"] = c.get("last_message_at")
    async for c in db.call_logs.find({"$or": [{"from_user_id": uid}, {"to_user_id": uid}]}):
        other_id = c["to_user_id"] if c["from_user_id"] == uid else c["from_user_id"]
        others.setdefault(other_id, {})["last_call_at"] = c.get("initiated_at")

    contacts = []
    for other_id, meta in others.items():
        u = await db.users.find_one({"_id": ObjectId(other_id)}, {"full_name": 1, "phone_primary": 1, "cadre_display": 1, "current_station": 1})
        if not u:
            continue
        contacts.append({
            "user_id": other_id,
            "full_name": u["full_name"],
            "phone_primary": u["phone_primary"],
            "cadre_display": u.get("cadre_display"),
            "current_station": u.get("current_station"),
            "last_message_at": meta.get("last_message_at"),
            "last_call_at": meta.get("last_call_at"),
        })
    contacts.sort(key=lambda x: (x.get("last_message_at") or x.get("last_call_at") or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return contacts
