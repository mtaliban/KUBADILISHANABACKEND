"""Selcom Ecommerce API client — HMAC-SHA256 signed requests.

Docs: https://developers.selcommobile.com/#/apis/reference/ecommerce

Wallet-Pull flow (kwa mobile money push):
1) POST /checkout/create-order-minimal → returns order_id
2) POST /checkout/wallet-payment → triggers USSD push on customer phone
3) Customer confirms on their handset
4) Selcom → webhook to callback_url with result
5) Poll GET /checkout/order-status for status
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
import httpx
from ...config import settings

logger = logging.getLogger(__name__)


def _b64(s: str | bytes) -> str:
    if isinstance(s, str):
        s = s.encode("utf-8")
    return base64.b64encode(s).decode("utf-8")


def _sign(payload: dict, timestamp: str, signed_fields: list[str]) -> dict:
    """Build Selcom auth headers per their spec."""
    digest_input = "&".join(f"{k}={payload[k]}" for k in signed_fields)
    signature = hmac.new(
        settings.selcom_api_secret.encode("utf-8"),
        digest_input.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Authorization": f"SELCOM {_b64(settings.selcom_api_key)}",
        "Digest-Method": "HS256",
        "Digest": _b64(signature),
        "Timestamp": timestamp,
        "Signed-Fields": ",".join(signed_fields),
        "Content-Type": "application/json",
    }


async def create_order(order_id: str, amount: int, buyer_email: str, buyer_name: str,
                       buyer_phone: str, callback_url: str) -> dict:
    """Create a minimal order at Selcom (returns Selcom's internal order_id)."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
    payload = {
        "vendor": settings.selcom_vendor_code,
        "order_id": order_id,
        "buyer_email": buyer_email or "unknown@kv.local",
        "buyer_name": buyer_name,
        "buyer_phone": buyer_phone,
        "amount": str(amount),
        "currency": settings.payment_currency,
        "webhook": _b64(callback_url),
        "no_of_items": "1",
    }
    signed = ["vendor", "order_id", "buyer_email", "buyer_name", "buyer_phone",
              "amount", "currency", "webhook", "no_of_items"]
    headers = _sign(payload, timestamp, signed)
    url = f"{settings.selcom_base_url}/checkout/create-order-minimal"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(url, json=payload, headers=headers)
        r.raise_for_status()
        return r.json()


async def wallet_push(order_id: str, buyer_phone: str) -> dict:
    """Trigger USSD push on buyer's mobile money account."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
    payload = {"transid": order_id, "order_id": order_id, "msisdn": buyer_phone}
    signed = ["transid", "order_id", "msisdn"]
    headers = _sign(payload, timestamp, signed)
    url = f"{settings.selcom_base_url}/checkout/wallet-payment"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(url, json=payload, headers=headers)
        r.raise_for_status()
        return r.json()


async def order_status(order_id: str) -> dict:
    """Query current order status."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
    payload = {"order_id": order_id}
    headers = _sign(payload, timestamp, ["order_id"])
    url = f"{settings.selcom_base_url}/checkout/order-status?order_id={order_id}"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url, headers=headers)
        r.raise_for_status()
        return r.json()


def verify_webhook_signature(raw_body: bytes, provided_sig: str | None) -> bool:
    """Verify Selcom webhook HMAC. In production, tighten this."""
    if not provided_sig:
        return False
    expected = hmac.new(
        settings.selcom_webhook_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, provided_sig)
