"""REAL-TIME verification: malalamiko yanafika kwa admin PAPO HAPO (WS).

Proves, against a LIVE backend (real Mongo + Mosquitto + Redis):
  1. Admin connects to /ws?token= (WS channel)
  2. A regular user submits feedback (POST /feedback)
  3. Admin's WS receives event 'notification' with type 'feedback.new'
     WITHOUT refresh.

Usage:
    cd backend && .venv/bin/python scripts/feedback_realtime_test.py
"""
import asyncio
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.security import hash_password

BASE = os.environ.get("KV_BASE", "http://localhost:8080")
WS_BASE = BASE.replace("http", "ws")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "testadmin@kv.test")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "TestAdmin!123")

try:
    import websockets
except ImportError:
    print("websockets haipo — install: .venv/bin/pip install websockets")
    sys.exit(1)

from pymongo import MongoClient
from bson import ObjectId


def rest(method, path, body=None, token=None, timeout=20):
    req = urllib.request.Request(f"{BASE}{path}", method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
            raw = r.read()
            try:
                return json.loads(raw.decode())
            except Exception:
                return {"raw": raw.decode()[:200]}
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": e.read().decode()[:300]}


def mongo_db():
    env = {}
    for p in ["../.env", ".env"]:
        try:
            for line in open(p):
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k] = v
        except FileNotFoundError:
            pass
    uri = f"mongodb://{env.get('MONGO_ROOT_USER', 'admin')}:{env.get('MONGO_ROOT_PASSWORD', 'changeme')}@localhost:27017/kubadilishana_vituo?authSource=admin"
    return MongoClient(uri, serverSelectionTimeoutMS=4000)["kubadilishana_vituo"]


async def admin_token():
    db = mongo_db()
    r = rest("POST", "/auth/login", {"phone": ADMIN_EMAIL, "password": ADMIN_PASS})
    if r.get("error"):
        print("login fail:", r.get("detail"))
        return None
    # Login hairejeshi user_id tena — tafuta kwa email kwenye DB.
    user = db.users.find_one({"email": ADMIN_EMAIL}, {"_id": 1})
    if not user:
        print("admin haipo kwenye DB")
        return None
    user_id = str(user["_id"])
    otp = "123456"
    # Ondoa records zote za zamani (zinaweza kuwa stale/expired) kisha andika
    # OTP mpya SAFI — hakikisha `purpose` ipo (login_2fa inaitafuta kwa hiyo).
    db.login_otps.delete_many({"user_id": ObjectId(user_id)})
    db.login_otps.insert_one({
        "user_id": ObjectId(user_id),
        "email": ADMIN_EMAIL,
        "purpose": "2fa",
        "code_hash": hash_password(otp),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        "created_at": datetime.now(timezone.utc),
        "used": False,
        "attempts": 0,
    })
    r2 = rest("POST", "/auth/login/2fa", {"email": ADMIN_EMAIL, "code": otp})
    token = r2.get("access_token")
    if not token:
        print("2fa fail:", r2.get("detail"))
        return None
    return token


async def user_token():
    db = mongo_db()
    # Create a fresh test user
    phone = "071" + "".join(random.choices("0123456789", k=7))
    payload = {
        "full_name": "Feedback Test User",
        "phone_primary": phone,
        "password": "changeme123",
        "category": "health",
        "cadre_code": "RN",
        "subjects": [],
        "current_station": {"region_id": 1, "region_name": "Arusha",
                            "district_id": 1, "district_name": "Arusha"},
        "desired_destinations": [{"region_id": 2, "region_name": "Dodoma"}],
    }
    r = rest("POST", "/auth/register", payload)
    if r.get("error"):
        print("register fail:", r.get("detail"))
        return None, phone
    return r.get("access_token"), phone


async def main():
    token = await admin_token()
    if not token:
        return
    print("✓ Admin token OK")

    # 1) Connect admin WS FIRST (before the action — avoids race)
    ws_url = f"{WS_BASE}/ws?token={token}"
    received = []
    try:
        ws = await asyncio.wait_for(websockets.connect(ws_url, open_timeout=10), timeout=12)
    except Exception as e:
        print("✗ WS connect fail:", e)
        return
    print("✓ Admin WS connected:", ws_url[:60] + "...")

    async def collect():
        try:
            async for msg in ws:
                try:
                    data = json.loads(msg)
                except Exception:
                    continue
                received.append(data)
                if data.get("type") == "feedback.new":
                    break
        except Exception:
            pass

    collector = asyncio.create_task(collect())

    # 2) User submits feedback
    utoken, phone = await user_token()
    if not utoken:
        print("✗ user register fail")
        return
    subj = f"Test Malalamiko {random.randint(1000, 9999)}"
    fb = rest("POST", "/feedback", {"subject": subj, "message": "Hii ni jaribio la real-time."}, utoken)
    print("✓ Feedback submitted:", fb.get("id") or fb.get("error") or fb.get("detail"))

    # 3) Wait for the WS event (max 8s)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not any(d.get("type") == "feedback.new" for d in received):
        await asyncio.sleep(0.3)

    hit = [d for d in received if d.get("type") == "feedback.new"]
    if hit:
        print("✅ REAL-TIME PASS — admin alipata feedback.new live:")
        print("   title:", hit[0].get("title"))
        print("   body:", hit[0].get("body"))
        print("   feedback.subject:", (hit[0].get("feedback") or {}).get("subject"))
    else:
        print("✗ REAL-TIME FAIL — hakuna feedback.new kwenye WS ya admin")
        print("   events zilizofika:", [d.get("event") or d.get("type") for d in received][:10])

    await ws.close()
    collector.cancel()

    # cleanup test data
    db = mongo_db()
    db.feedback.delete_many({"user_name": "Feedback Test User"})
    db.users.delete_many({"full_name": "Feedback Test User"})
    print("✓ cleanup done")


asyncio.run(main())
