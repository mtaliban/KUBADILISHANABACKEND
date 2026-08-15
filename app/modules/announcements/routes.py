"""Admin announcements (matangazo).

- Admin sends a broadcast → stored + MQTT event `kv/announcement/{recipient}` for
  every recipient so the subscriber turns it into a notification for each user.
- Any user can list active announcements aimed at them and dismiss (cancel) them.
"""
from datetime import datetime, timezone
from typing import Optional
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ...db import get_db
from ...security import current_user, current_admin
from ...events.publisher import publish
from ...events.topics import TOPIC_ANNOUNCEMENT

router = APIRouter(tags=["announcements"])


class AnnouncementCreate(BaseModel):
    title: str = Field(..., min_length=3, max_length=120)
    message: str = Field(..., min_length=3, max_length=2000)
    audience: str = "all"  # 'all' | 'user' | code ya idara (k.m. health, education, au mpya)
    target_user_id: Optional[str] = None


async def _resolve_recipients(db, body: AnnouncementCreate) -> list[str]:
    if body.audience == "all":
        cur = db.users.find({"status": "active"}, {"_id": 1})
        return [str(u["_id"]) async for u in cur]
    if body.audience and body.audience not in ("all", "user"):
        # Idara yoyote (dynamic) — health, education, au idara mpya iliyoongezwa
        # na admin. Watu wa idara hiyo tu wanapokea tangazo.
        cur = db.users.find({"category": body.audience, "status": "active"}, {"_id": 1})
        return [str(u["_id"]) async for u in cur]
    if body.target_user_id:
        try:
            oid = ObjectId(body.target_user_id)
        except Exception:
            raise HTTPException(400, "Invalid target_user_id")
        u = await db.users.find_one({"_id": oid}, {"_id": 1})
        if not u:
            raise HTTPException(404, "Target user not found")
        return [str(u["_id"])]
    raise HTTPException(400, "target_user_id is required for audience=user")


@router.post("/admin/announcements", tags=["admin"])
async def send_announcement(body: AnnouncementCreate, admin=Depends(current_admin)):
    """Admin sends a tangazo to all users / a category / a single user."""
    db = get_db()
    recipients = await _resolve_recipients(db, body)
    now = datetime.now(timezone.utc)
    doc = {
        "title": body.title.strip(),
        "message": body.message.strip(),
        "audience": body.audience,
        "target_user_id": body.target_user_id,
        "recipient_ids": recipients,
        "recipient_count": len(recipients),
        "created_by": str(admin["_id"]),
        "created_by_name": admin["full_name"],
        "created_at": now,
        "active": True,
        "dismissed_by": [],
    }
    res = await db.announcements.insert_one(doc)
    ann_id = str(res.inserted_id)
    # Event-driven: one MQTT event per recipient → subscriber notifies each user
    base = {
        "event": "announcement.new",
        "announcement_id": ann_id,
        "title": doc["title"],
        "message": doc["message"],
        "audience": doc["audience"],
        "created_at": now.isoformat(),
    }
    for uid in recipients:
        publish(f"{TOPIC_ANNOUNCEMENT}/{uid}", {**base, "user_id": uid})
    return {
        "announcement_id": ann_id,
        "sent_to": len(recipients),
        "audience": body.audience,
        "created_at": now.isoformat(),
    }


@router.get("/announcements/active")
async def active_announcements(user=Depends(current_user), limit: int = Query(20, le=100)):
    """Tangazo zinazolenga mimi, ambazo sija-dismiss (cancel)."""
    db = get_db(); uid = str(user["_id"])
    cur = db.announcements.find(
        {"active": True, "recipient_ids": uid, "dismissed_by": {"$ne": uid}},
        {"title": 1, "message": 1, "audience": 1, "created_by_name": 1, "created_at": 1,
         "recipient_count": 1, "dismissed_by": 1},
    ).sort("created_at", -1).limit(limit)
    out = []
    async for a in cur:
        out.append({
            "announcement_id": str(a["_id"]),
            "title": a["title"],
            "message": a["message"],
            "audience": a["audience"],
            "created_by_name": a.get("created_by_name"),
            "created_at": a["created_at"],
            "recipient_count": a.get("recipient_count", 0),
            "dismissed": uid in (a.get("dismissed_by") or []),
        })
    return {"count": len(out), "announcements": out}


@router.get("/announcements/unread-count")
async def announcement_unread_count(user=Depends(current_user)):
    """Tangazo ambazo bado hazija-dismissed — badge ya icon ya megaphone."""
    db = get_db(); uid = str(user["_id"])
    n = await db.announcements.count_documents(
        {"active": True, "recipient_ids": uid, "dismissed_by": {"$ne": uid}}
    )
    return {"unread": n}


@router.post("/announcements/{announcement_id}/dismiss")
async def dismiss_announcement(announcement_id: str, user=Depends(current_user)):
    """User akubali/kufuta tangazo — halionekani tena kwa ajili yake."""
    db = get_db(); uid = str(user["_id"])
    r = await db.announcements.update_one(
        {"_id": ObjectId(announcement_id), "recipient_ids": uid},
        {"$addToSet": {"dismissed_by": uid}},
    )
    if not r.matched_count:
        raise HTTPException(404, "Announcement not found")
    return {"ok": True}


@router.get("/admin/announcements", tags=["admin"])
async def admin_list_announcements(_=Depends(current_admin), limit: int = Query(50, le=200)):
    db = get_db()
    total = await db.announcements.count_documents({})
    cur = db.announcements.find().sort("created_at", -1).limit(limit)
    out = []
    async for a in cur:
        out.append({
            "announcement_id": str(a["_id"]),
            "title": a["title"], "message": a["message"], "audience": a["audience"],
            "target_user_id": a.get("target_user_id"),
            "recipient_count": a.get("recipient_count", 0),
            "dismissed_count": len(a.get("dismissed_by") or []),
            "created_by_name": a.get("created_by_name"),
            "created_at": a["created_at"], "active": a.get("active", True),
        })
    return {"total": total, "announcements": out}


@router.post("/admin/announcements/{announcement_id}/resend", tags=["admin"])
async def resend_announcement(announcement_id: str, admin=Depends(current_admin)):
    """Tuma tena tangazo lililotumwa — kwa walengwa WOTE wa sasa (recipients
    wanahesabiwa upya — watumiaji wapya wanaojiunga wanaingia pia)."""
    db = get_db()
    doc = await db.announcements.find_one({"_id": ObjectId(announcement_id)})
    if not doc:
        raise HTTPException(404, "Tangazo halipo")
    body = AnnouncementCreate(
        title=doc["title"], message=doc["message"],
        audience=doc.get("audience", "all"),
        target_user_id=doc.get("target_user_id"),
    )
    recipients = await _resolve_recipients(db, body)
    now = datetime.now(timezone.utc)
    new_doc = {
        "title": body.title,
        "message": body.message,
        "audience": body.audience,
        "target_user_id": body.target_user_id,
        "recipient_ids": recipients,
        "recipient_count": len(recipients),
        "created_by": str(admin["_id"]),
        "created_by_name": admin["full_name"],
        "created_at": now,
        "active": True,
        "dismissed_by": [],
        "resent_from": announcement_id,
    }
    res = await db.announcements.insert_one(new_doc)
    ann_id = str(res.inserted_id)
    base = {
        "event": "announcement.new",
        "announcement_id": ann_id,
        "title": new_doc["title"],
        "message": new_doc["message"],
        "audience": new_doc["audience"],
        "created_at": now.isoformat(),
    }
    for uid in recipients:
        publish(f"{TOPIC_ANNOUNCEMENT}/{uid}", {**base, "user_id": uid})
    return {
        "announcement_id": ann_id,
        "sent_to": len(recipients),
        "audience": body.audience,
        "created_at": now.isoformat(),
    }


@router.delete("/admin/announcements/{announcement_id}", tags=["admin"])
async def delete_announcement(announcement_id: str, _=Depends(current_admin)):
    """Futa tangazo (CRUD) — halionekani tena kwenye orodha ya admin wala
    kwa watumiaji (dismissed pia)."""
    db = get_db()
    r = await db.announcements.delete_one({"_id": ObjectId(announcement_id)})
    if not r.deleted_count:
        raise HTTPException(404, "Tangazo halipo")
    return {"ok": True, "deleted": announcement_id}
