"""Maoni na Malalamiko (Comments & Complaints).

Mtumiaji anaweza kutuma maoni/malalamiko yake kwa admin moja kwa moja
(na kumuona jibu la admin). Admin ana sehemu ya kusoma maoni yote na
kumjibu mtumiaji (au kundi la watumiaji). Real-time: maoni mapya yanajulisha
admin kupitia WS (event `feedback.new`) — hakuna refresh ya page.
"""
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ...db import get_db
from ...security import current_user, current_admin
from ..messaging.ws_manager import manager

router = APIRouter(prefix="/feedback", tags=["feedback"])


def _out(f: dict) -> dict:
    return {
        "id": str(f["_id"]),
        "subject": f.get("subject", ""),
        "message": f.get("message", ""),
        "status": f.get("status", "open"),
        "admin_reply": f.get("admin_reply"),
        "admin_replied_at": f.get("admin_replied_at"),
        "created_at": f["created_at"],
        "user_name": f.get("user_name"),
        "user_phone": f.get("user_phone"),
    }


class FeedbackCreate(BaseModel):
    subject: str = Field(..., min_length=2, max_length=120)
    message: str = Field(..., min_length=3, max_length=2000)


@router.post("", status_code=201)
async def submit_feedback(body: FeedbackCreate, user=Depends(current_user)):
    """Mtumiaji anatuma maoni/malalamiko yake kwa admin."""
    db = get_db()
    now = datetime.now(timezone.utc)
    doc = {
        "user_id": str(user["_id"]),
        "user_name": user.get("full_name", ""),
        "user_phone": user.get("phone_primary"),
        "subject": body.subject.strip(),
        "message": body.message.strip(),
        "status": "open",
        "admin_reply": None,
        "admin_replied_at": None,
        "created_at": now,
    }
    r = await db.feedback.insert_one(doc)
    fid = str(r.inserted_id)
    # Real-time: admin anajulishwa PAPO HAPO (WS) — maoni mapya yanaonekana
    # kwenye page ya admin bila refresh. Same funnel kama notifications.
    admins = [str(u["_id"]) async for u in db.users.find({"is_admin": True}, {"_id": 1})]
    for aid in admins:
        await manager.send_to_user(aid, {
            "event": "feedback.new",
            "type": "feedback.new",
            "feedback": _out(doc),
            "occurred_at": now.isoformat(),
        })
    return _out(doc)


@router.get("/my")
async def my_feedback(user=Depends(current_user), limit: int = Query(50, le=200)):
    """Maoni yangu + majibu ya admin."""
    db = get_db()
    q = {"user_id": str(user["_id"])}
    cur = db.feedback.find(q).sort("created_at", -1).limit(limit)
    return {"total": await db.feedback.count_documents(q), "items": [_out(f) async for f in cur]}


# ─── Admin ────────────────────────────────────────────


class FeedbackReply(BaseModel):
    reply: str = Field(..., min_length=1, max_length=2000)


@router.get("/admin/all")
async def admin_list_feedback(_=Depends(current_admin), status: str = Query(""),
                              q: str = Query(""), limit: int = Query(100, le=500)):
    """Admin anaona maoni yote (open/replied) + anaweza kufuta.\n\n    Real-time: `feedback.new` WS event inamjulisha admin papo hapo bila refresh."""
    db = get_db()
    qd: dict = {}
    if status:
        qd["status"] = status
    if q:
        qd["$or"] = [
            {"subject": {"$regex": q, "$options": "i"}},
            {"message": {"$regex": q, "$options": "i"}},
            {"user_name": {"$regex": q, "$options": "i"}},
        ]
    total = await db.feedback.count_documents(qd)
    cur = db.feedback.find(qd).sort("created_at", -1).limit(limit)
    items = [_out(f) async for f in cur]
    counts = {}
    async for r in db.feedback.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        counts[r["_id"]] = r["n"]
    return {"total": total, "items": items,
            "counts": {"open": counts.get("open", 0), "replied": counts.get("replied", 0)}}


@router.post("/admin/{feedback_id}/reply")
async def admin_reply(feedback_id: str, body: FeedbackReply, _=Depends(current_admin)):
    """Admin anamjibu mtumiaji — jibu linaonekana kwenye maoni yake PAPO HAPO
    (WS `feedback.replied` inamfikia mtumiaji bila refresh)."""
    db = get_db()
    try:
        fid = ObjectId(feedback_id)
    except Exception:
        raise HTTPException(400, "Invalid feedback ID")
    now = datetime.now(timezone.utc)
    f = await db.feedback.find_one({"_id": fid})
    if not f:
        raise HTTPException(404, "Maoni hayapo")
    await db.feedback.update_one(
        {"_id": fid},
        {"$set": {"status": "replied", "admin_reply": body.reply.strip(),
                  "admin_replied_at": now, "replied_at": now}},
    )
    # Real-time kwa mtumiaji: jibu linaonekana PAPO HAPO (bila refresh).
    await manager.send_to_user(f["user_id"], {
        "event": "feedback.replied",
        "type": "feedback.replied",
        "feedback": _out({**f, "status": "replied", "admin_reply": body.reply.strip(),
                          "admin_replied_at": now}),
        "occurred_at": now.isoformat(),
    })
    return {"ok": True}


@router.delete("/admin/{feedback_id}")
async def admin_delete_feedback(feedback_id: str, _=Depends(current_admin)):
    try:
        fid = ObjectId(feedback_id)
    except Exception:
        raise HTTPException(400, "Invalid feedback ID")
    r = await get_db().feedback.delete_one({"_id": fid})
    if not r.deleted_count:
        raise HTTPException(404, "Maoni hayapo")
    return {"ok": True, "deleted": feedback_id}
