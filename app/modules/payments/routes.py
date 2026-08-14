"""Payments module — manual donation verification flow.

Flow:
1. Donor sees the admin's mobile-money number (`GET /payments/info`).
2. Donor pays that number from any network (M-Pesa, Tigo Pesa, Airtel Money,
   Halopesa, …) and receives an SMS confirmation.
3. Donor pastes the SMS text on the donate page and submits (`POST /payments/donate`).
4. Admin reviews the submission on the admin panel, confirms the money on their
   own phone, then approves or rejects (`POST /payments/admin/{id}/approve|reject`).
5. Donor's status flips from `verifying` → `approved` | `rejected`.

Each step publishes an MQTT event for the audit stream and live toasts.
"""
import logging
import secrets
from datetime import datetime, timezone
from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, Query

from ...config import settings
from ...db import get_db
from ...events.publisher import publish
from ...events.topics import (
    TOPIC_PAYMENT_SUBMITTED, TOPIC_PAYMENT_APPROVED, TOPIC_PAYMENT_REJECTED,
)
from ...security import current_user, current_admin, normalize_phone
from .schemas import DonateRequest, DonateResponse, AdminReviewRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/payments", tags=["payments"])


def _new_order_id() -> str:
    """Reference kama ya M-PESA: herufi kubwa 10 (k.m. C2H8MZ3JX1) —
    inaonekana kisomi kwa mchangiaji na admin (sio prefix ya mfumo)."""
    return secrets.token_hex(5).upper()


def _donation_out(doc: dict) -> dict:
    return {
        "order_id": doc["_id"],
        "user_id": doc.get("user_id"),
        "user_name": doc.get("user_name"),
        "amount": doc["amount"],
        "currency": doc.get("currency", "TZS"),
        "phone": doc.get("phone"),
        "sms_text": doc.get("sms_text"),
        "purpose": doc.get("purpose", "donation"),
        "status": doc["status"],
        "note": doc.get("note"),
        "created_at": doc["created_at"],
        "approved_at": doc.get("approved_at"),
        "rejected_at": doc.get("rejected_at"),
    }


@router.get("/info")
async def donation_info(_=Depends(current_user)):
    """Admin's mobile-money number that donors pay into (any network)."""
    return {"phone": settings.donation_phone, "currency": settings.payment_currency}


@router.post("/donate", response_model=DonateResponse)
async def submit_donation(body: DonateRequest, user=Depends(current_user)):
    db = get_db()
    order_id = _new_order_id()
    now = datetime.now(timezone.utc)
    try:
        buyer_phone = normalize_phone(body.phone) if body.phone else user["phone_primary"]
    except ValueError as e:
        raise HTTPException(422, f"phone: {e}")

    doc = {
        "_id": order_id,
        "user_id": str(user["_id"]),
        "user_name": user["full_name"],
        "amount": body.amount,
        "currency": settings.payment_currency,
        "phone": buyer_phone,
        "sms_text": body.sms_text.strip(),
        "purpose": body.purpose,
        "status": "verifying",
        "note": None,
        "created_at": now,
        "approved_at": None,
        "rejected_at": None,
    }
    await db.payments.insert_one(doc)

    publish(f"{TOPIC_PAYMENT_SUBMITTED}/{user['_id']}", {
        "event": "payment.submitted", "order_id": order_id,
        "amount": body.amount, "currency": settings.payment_currency,
        "status": "verifying", "occurred_at": now.isoformat(),
    })
    logger.info(f"Donation {order_id} submitted by {doc['user_id']} — awaiting verification")
    return DonateResponse(
        order_id=order_id, status="verifying", amount=body.amount,
        message="Tumepokea uthibitisho wako. Admin anathibitisha malipo — status itabadilika hivi karibuni.",
    )


@router.get("/status/{order_id}")
async def get_status(order_id: str, user=Depends(current_user)):
    order = await get_db().payments.find_one({"_id": order_id, "user_id": str(user["_id"])})
    if not order:
        raise HTTPException(404, "Order not found")
    return {"order_id": order_id, "status": order["status"], "amount": order["amount"],
            "note": order.get("note"), "created_at": order["created_at"],
            "approved_at": order.get("approved_at"), "rejected_at": order.get("rejected_at")}


@router.get("/my-history")
async def my_history(user=Depends(current_user), limit: int = Query(50, le=200)):
    cur = get_db().payments.find({"user_id": str(user["_id"])}).sort("created_at", -1).limit(limit)
    return [_donation_out(p) async for p in cur]


@router.get("/admin/all")
async def admin_list_payments(_=Depends(current_admin),
                              status: Optional[str] = Query(None),
                              limit: int = Query(200, le=1000)):
    q = {"status": status} if status else {}
    cur = get_db().payments.find(q).sort("created_at", -1).limit(limit)
    payments = [_donation_out(p) async for p in cur]
    total_approved = await get_db().payments.aggregate([
        {"$match": {"status": "approved"}}, {"$group": {"_id": None, "s": {"$sum": "$amount"}}}
    ]).to_list(1)
    return {"total_approved_tzs": (total_approved[0]["s"] if total_approved else 0),
            "count": len(payments), "payments": payments}


async def _review(order_id: str, new_status: Literal["approved", "rejected"],
                  note: str | None, _=Depends(current_admin)) -> dict:
    db = get_db()
    now = datetime.now(timezone.utc)
    r = await db.payments.update_one(
        {"_id": order_id, "status": "verifying"},
        {"$set": {**{"status": new_status, "note": note},
                   **({f"{new_status}_at": now} if new_status in ("approved", "rejected") else {})}},
    )
    if not r.matched_count:
        raise HTTPException(400, "Donation haiko kwenye status 'verifying' au haipo")
    order = await db.payments.find_one({"_id": order_id})
    topic = TOPIC_PAYMENT_APPROVED if new_status == "approved" else TOPIC_PAYMENT_REJECTED
    publish(f"{topic}/{order['user_id']}", {
        "event": f"payment.{new_status}", "order_id": order_id,
        "amount": order["amount"], "currency": order["currency"],
        "status": new_status, "occurred_at": now.isoformat(),
    })
    logger.info(f"Donation {order_id} → {new_status} (TZS {order['amount']})")
    return {"ok": True, "order_id": order_id, "status": new_status}


@router.post("/admin/{order_id}/approve")
async def approve_donation(order_id: str, body: AdminReviewRequest | None = None,
                          admin=Depends(current_admin)):
    return await _review(order_id, "approved", body.note if body else None, admin)


@router.post("/admin/{order_id}/reject")
async def reject_donation(order_id: str, body: AdminReviewRequest | None = None,
                          admin=Depends(current_admin)):
    return await _review(order_id, "rejected", body.note if body else None, admin)
