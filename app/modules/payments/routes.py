"""Payments module — Selcom mobile-money integration.

Modes:
  - selcom_mode=mock  → no Selcom API calls; order auto-completes after 5s (dev/demo)
  - selcom_mode=live  → real Selcom Ecommerce API (requires vendor+key+secret env vars)
"""
import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from ...config import settings
from ...db import get_db
from ...events.publisher import publish
from ...security import current_user, current_admin
from .schemas import InitiatePaymentRequest, InitiatePaymentResponse
from .selcom_client import create_order, wallet_push, order_status, verify_webhook_signature

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/payments", tags=["payments"])

MOCK_AUTO_PAY_SECONDS = 5  # in mock mode, "customer" confirms after this


def _new_order_id() -> str:
    return f"kv_{int(datetime.now(timezone.utc).timestamp())}_{secrets.token_hex(4)}"


def _publish_status(order: dict) -> None:
    """Emit MQTT event so frontend gets a live toast."""
    uid = order["user_id"]
    ev = f"payment.{order['status']}"
    publish(f"kv/payment/{order['status']}/{uid}", {
        "event": ev, "order_id": order["_id"], "amount": order["amount"],
        "currency": order["currency"], "method": order["method"],
        "status": order["status"], "occurred_at": datetime.now(timezone.utc).isoformat(),
    })


async def _mock_auto_complete(order_id: str) -> None:
    """Simulate customer confirming payment after MOCK_AUTO_PAY_SECONDS."""
    await asyncio.sleep(MOCK_AUTO_PAY_SECONDS)
    db = get_db()
    now = datetime.now(timezone.utc)
    r = await db.payments.update_one(
        {"_id": order_id, "status": "pending"},
        {"$set": {"status": "paid", "paid_at": now, "mock_completed": True}},
    )
    if r.modified_count:
        order = await db.payments.find_one({"_id": order_id})
        _publish_status(order)
        # Update user payment_status
        from bson import ObjectId
        await db.users.update_one({"_id": ObjectId(order["user_id"])},
                                  {"$set": {"payment_status": "paid", "paid_at": now}})
        logger.info(f"[MOCK] Payment {order_id} auto-completed (paid)")


@router.post("/initiate", response_model=InitiatePaymentResponse)
async def initiate_payment(
    body: InitiatePaymentRequest,
    bg: BackgroundTasks,
    user=Depends(current_user),
):
    db = get_db()
    order_id = _new_order_id()
    now = datetime.now(timezone.utc)
    buyer_phone = body.phone or user["phone_primary"]

    doc = {
        "_id": order_id,
        "user_id": str(user["_id"]),
        "amount": body.amount,
        "currency": settings.payment_currency,
        "method": body.method,
        "purpose": body.purpose,
        "buyer_phone": buyer_phone,
        "status": "pending",
        "mode": settings.selcom_mode,
        "created_at": now,
        "paid_at": None,
        "selcom_response": None,
        "callback_payload": None,
    }
    await db.payments.insert_one(doc)

    publish(f"kv/payment/initiated/{user['_id']}", {
        "event": "payment.initiated", "order_id": order_id,
        "amount": body.amount, "currency": settings.payment_currency,
        "method": body.method, "occurred_at": now.isoformat(),
    })

    if settings.selcom_mode == "mock":
        # simulate the customer confirming after N seconds
        bg.add_task(_mock_auto_complete, order_id)
        return InitiatePaymentResponse(
            order_id=order_id, status="pending", amount=body.amount,
            method=body.method, checkout_url=None,
            message=f"[DEMO] Angalia simu yako — malipo yatakamilika baada ya {MOCK_AUTO_PAY_SECONDS}s (mock)",
        )

    # LIVE Selcom
    try:
        callback_url = f"{settings.public_base_url.rstrip('/')}/payments/webhook"
        sel_order = await create_order(
            order_id=order_id, amount=body.amount,
            buyer_email=f"{buyer_phone}@kv.local", buyer_name=user["full_name"],
            buyer_phone=buyer_phone, callback_url=callback_url,
        )
        await db.payments.update_one({"_id": order_id}, {"$set": {"selcom_response": sel_order}})
        checkout_url = None
        # Wallet push for mobile money methods
        if body.method in {"mixx", "airtel", "mpesa", "halopesa"}:
            push_resp = await wallet_push(order_id, buyer_phone)
            await db.payments.update_one({"_id": order_id}, {"$set": {"selcom_push": push_resp}})
            msg = "Angalia simu yako — kubali USSD prompt kumaliza malipo"
        else:
            # Card / hosted checkout — Selcom returns payment_gateway_url in create_order
            checkout_url = (sel_order.get("data") or [{}])[0].get("payment_gateway_url")
            msg = "Bofya kiungo cha malipo kumaliza"
        return InitiatePaymentResponse(
            order_id=order_id, status="pending", amount=body.amount,
            method=body.method, checkout_url=checkout_url, message=msg,
        )
    except Exception as e:
        logger.exception(f"Selcom initiate failed: {e}")
        await db.payments.update_one({"_id": order_id}, {"$set": {"status": "failed", "error": str(e)}})
        raise HTTPException(502, f"Payment provider error: {e}")


@router.post("/webhook")
async def selcom_webhook(request: Request):
    """
    Selcom → backend. Called after user confirms USSD or Selcom detects failure.
    Response must be 2xx quickly (Selcom retries otherwise).
    """
    raw = await request.body()
    sig = request.headers.get("Signature") or request.headers.get("X-Signature")
    if settings.selcom_mode == "live" and not verify_webhook_signature(raw, sig):
        logger.warning("Selcom webhook signature verification failed")
        raise HTTPException(401, "Invalid signature")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    order_id = data.get("order_id") or data.get("reference")
    result = (data.get("result") or data.get("status") or "").upper()
    if not order_id:
        raise HTTPException(400, "Missing order_id")

    db = get_db()
    order = await db.payments.find_one({"_id": order_id})
    if not order:
        return {"ok": True, "note": "unknown order — ignored"}

    now = datetime.now(timezone.utc)
    status_map = {"SUCCESS": "paid", "COMPLETED": "paid", "PAID": "paid",
                  "FAILED": "failed", "CANCELLED": "failed", "EXPIRED": "expired"}
    new_status = status_map.get(result, "failed")

    await db.payments.update_one(
        {"_id": order_id},
        {"$set": {"status": new_status, "paid_at": now if new_status == "paid" else None,
                  "callback_payload": data}},
    )
    fresh = await db.payments.find_one({"_id": order_id})
    _publish_status(fresh)

    if new_status == "paid":
        from bson import ObjectId
        await db.users.update_one({"_id": ObjectId(order["user_id"])},
                                  {"$set": {"payment_status": "paid", "paid_at": now}})
    logger.info(f"Webhook: order {order_id} → {new_status}")
    return {"ok": True, "order_id": order_id, "status": new_status}


@router.get("/status/{order_id}")
async def get_status(order_id: str, user=Depends(current_user)):
    order = await get_db().payments.find_one({"_id": order_id, "user_id": str(user["_id"])})
    if not order:
        raise HTTPException(404, "Order not found")
    return {"order_id": order_id, "status": order["status"], "amount": order["amount"],
            "method": order["method"], "created_at": order["created_at"],
            "paid_at": order.get("paid_at"), "mode": order.get("mode")}


@router.get("/my-history")
async def my_history(user=Depends(current_user), limit: int = Query(50, le=200)):
    cur = get_db().payments.find({"user_id": str(user["_id"])}).sort("created_at", -1).limit(limit)
    out = []
    async for p in cur:
        out.append({"order_id": p["_id"], "amount": p["amount"], "currency": p["currency"],
                    "method": p["method"], "status": p["status"], "created_at": p["created_at"],
                    "paid_at": p.get("paid_at"), "purpose": p.get("purpose"), "mode": p.get("mode")})
    return out


@router.get("/admin/all")
async def admin_list_payments(_=Depends(current_admin),
                              status: Optional[str] = None,
                              limit: int = Query(200, le=1000)):
    q = {"status": status} if status else {}
    cur = get_db().payments.find(q).sort("created_at", -1).limit(limit)
    out = []
    async for p in cur:
        out.append({"order_id": p["_id"], "user_id": p["user_id"], "amount": p["amount"],
                    "method": p["method"], "status": p["status"], "created_at": p["created_at"],
                    "paid_at": p.get("paid_at"), "mode": p.get("mode")})
    total_paid = await get_db().payments.aggregate([
        {"$match": {"status": "paid"}}, {"$group": {"_id": None, "s": {"$sum": "$amount"}}}
    ]).to_list(1)
    return {"total_paid_tzs": (total_paid[0]["s"] if total_paid else 0), "count": len(out), "payments": out}
