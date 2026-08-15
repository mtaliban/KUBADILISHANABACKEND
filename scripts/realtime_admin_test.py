"""REAL-TIME verification for the admin pipeline (event-driven).

Proves, against a LIVE backend (real Mongo + Mosquitto + Redis):
  1. SSE feed  /admin/live-events  → new events appear WITHOUT refresh (<6s)
  2. WS channel → admin receives 'user.registered' notification live
  3. /admin/events endpoint returns the new event (fresh, bypass cache)

Usage (backend at http://localhost:8080):
    cd backend && .venv/bin/python scripts/realtime_admin_test.py
    (weka MONGO_URI env kama SMTP inatuma OTP kwa email — script inaweka
     OTP inayojulikana kwenye DB kwa test admin pekee)
"""
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.environ.get("KV_BASE", "http://localhost:8080")

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "testadmin@kv.test")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "TestAdmin!123")


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


def read_sse(token, duration):
    """HTTP stream kama frontend inavyofanya (fetch + reader) — sio WS."""
    req = urllib.request.Request(f"{BASE}/admin/live-events")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "text/event-stream")
    events = []
    deadline = time.monotonic() + duration
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            buffer = b""
            while time.monotonic() < deadline:
                chunk = r.read(1024)
                if not chunk:
                    break
                buffer += chunk
                text = buffer.decode("utf-8", errors="replace")
                # process complete SSE blocks
                while "\n\n" in text:
                    block, text = text.split("\n\n", 1)
                    for line in block.split("\n"):
                        if line.startswith("data: "):
                            try:
                                events.append(json.loads(line[6:]))
                            except Exception:
                                pass
                buffer = text.encode("utf-8")
    except Exception as e:
        return {"error": str(e)}
    return {"events": events}


async def main():
    results = {}

    # 1) Login as admin (2FA: password step, then OTP step)
    step1 = rest("POST", "/auth/login", {"phone": ADMIN_EMAIL, "password": ADMIN_PASS})
    if "error" in step1:
        print(f"❌ Admin LOGIN step1 failed: {step1.get('detail', step1)}")
        sys.exit(1)
    if not step1.get("two_factor_required"):
        print(f"❌ Expected 2FA challenge, got: {step1}")
        sys.exit(1)
    code = step1.get("dev_code") or os.environ.get("ADMIN_OTP", "")
    if not code:
        # Test tu: weka OTP inayojulikana moja kwa moja kwenye DB (kwa hii
        # test admin pekee) — code hashed kwa njia ile ile app inavyofanya.
        from datetime import datetime, timedelta, timezone
        from bson import ObjectId
        from pymongo import MongoClient
        from app.security import hash_password
        mongo_uri = os.environ.get("MONGO_URI")
        if not mongo_uri:
            print("❌ SMTP inatuma code kwa email na MONGO_URI haijawekwa.")
            sys.exit(1)
        code = "123456"
        c = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
        db = c[os.environ.get("MONGO_DB", "kubadilishana_vituo")]
        admin_doc = db.users.find_one({"email": ADMIN_EMAIL}, {"_id": 1})
        if not admin_doc:
            print("❌ Test admin hapatikani kwenye DB")
            sys.exit(1)
        uid = admin_doc["_id"]
        db.login_otps.update_one(
            {"user_id": uid, "purpose": "2fa"},
            {"$set": {"user_id": uid, "email": ADMIN_EMAIL,
                      "purpose": "2fa", "code_hash": hash_password(code),
                      "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
                      "created_at": datetime.now(timezone.utc), "used": False, "attempts": 0}},
            upsert=True,
        )
        c.close()
    step2 = rest("POST", "/auth/login/2fa", {"email": ADMIN_EMAIL, "code": code})
    if "error" in step2 or not step2.get("access_token"):
        print(f"❌ Admin LOGIN step2 failed: {step2.get('detail', step2)}")
        sys.exit(1)
    login = step2
    token = login["access_token"]
    admin_id = login.get("user_id")
    print(f"✅ Admin logged in: {login.get('full_name')} ({admin_id})")

    # 2) Register a fresh user → publishes user.registered
    ts = str(int(time.time()))
    phone = f"0763{ts[-6:]}"
    reg_body = {
        "full_name": f"RT Test {ts}",
        "phone_primary": phone,
        "password": "secret123",
        "category": "health",
        "cadre_code": "CO",
        "current_station": {"region_id": 17, "region_name": "Mwanza", "district_id": 1701,
                            "district_name": "Nyamagana DC", "facility_id": None, "facility_name": None},
        "desired_destinations": [{"region_id": 1, "region_name": "Arusha", "district_id": 101,
                                  "district_name": "Arusha DC", "facility_id": None, "facility_name": None}],
    }
    reg = rest("POST", "/auth/register", reg_body)
    if "error" in reg or not reg.get("user_id"):
        print(f"❌ Register failed: {reg.get('detail', reg)}")
        sys.exit(1)
    new_uid = reg["user_id"]
    print(f"✅ Registered new user: {reg.get('full_name')} ({new_uid})")

    # 3) Open SSE feed FIRST (background), THEN register → event must arrive
    #    without refresh. Order matters: SSE lazima iwe wazi kabla event inatokea.
    import threading
    import subprocess
    sse_result = {}

    def sse_reader():
        out = subprocess.run(
            ["curl", "-s", "-N", "--max-time", "10",
             "-H", f"Authorization: Bearer {token}",
             f"{BASE}/admin/live-events"],
            capture_output=True, text=True)
        sse_result["out"] = out.stdout
        sse_result["rc"] = out.returncode

    st = threading.Thread(target=sse_reader)
    st.start()
    await asyncio.sleep(2)  # SSE connection inaanza kabla ya event
    ts_reg = str(int(time.time()))
    reg_body2 = dict(reg_body)
    reg_body2["phone_primary"] = f"0763{ts_reg[-6:]}"
    reg_body2["full_name"] = f"RT SSE {ts_reg}"
    reg3 = rest("POST", "/auth/register", reg_body2)
    if "error" in reg3:
        print(f"❌ SSE-trigger register failed: {reg3.get('detail', reg3)}")
        results["sse_live"] = False
    else:
        st.join(timeout=15)
        out = sse_result.get("out", "")
        got = [e for line in out.split("\n") if line.startswith("data: ")
               for e in [json.loads(line[6:])] if e.get("event_type") == "user.registered"]
        print(f"✅ SSE feed connected — received {len(got)} user.registered events (live)")
        if got:
            print(f"✅ SSE: user.registered event arrived LIVE (no refresh) — {got[0].get('occurred_at')}")
            results["sse_live"] = True
        else:
            print(f"❌ SSE: no user.registered event within 10s. Raw: {out[:200]}")
            results["sse_live"] = False

    # 4) WS: admin should receive a 'notification' (user.registered) live
    try:
        import websockets
        ws_uri = BASE.replace("http", "ws") + f"/ws?token={token}"
        ws = await websockets.connect(ws_uri, open_timeout=10)
        # Register a SECOND user while WS is open — notification must arrive live
        ts2 = str(int(time.time()) + 1)
        reg_body["phone_primary"] = f"0763{ts2[-6:]}"
        reg_body["full_name"] = f"RT Test WS {ts2}"
        reg2 = rest("POST", "/auth/register", reg_body)
        if "error" in reg2:
            print(f"❌ 2nd register failed: {reg2.get('detail', reg2)}")
            results["ws_live"] = False
        else:
            found = False
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue
                if ev.get("event") == "notification" and ev.get("type") == "user.registered":
                    found = True
                    print(f"✅ WS: admin received user.registered notification LIVE: '{ev.get('title')}'")
                    break
            results["ws_live"] = found
            if not found:
                print("❌ WS: admin did NOT receive user.registered notification within 6s")
        await ws.close()
    except Exception as e:
        print(f"❌ WS test error: {e}")
        results["ws_live"] = False

    # 5) /admin/events must include the new registration (fresh)
    ev_res = rest("GET", "/admin/events?limit=5", token=token)
    if "error" in ev_res:
        print(f"❌ /admin/events: {ev_res.get('detail')}")
        results["events_api"] = False
    else:
        types = [e.get("event_type") for e in ev_res.get("events", [])]
        has_new = any(e.get("event_type") == "user.registered" and e.get("actor_user_id") == new_uid
                      for e in ev_res.get("events", []))
        print(f"✅ /admin/events returned {len(ev_res.get('events', []))} events, first types: {types[:4]}")
        if has_new:
            print("✅ /admin/events contains the NEW registration immediately")
            results["events_api"] = True
        else:
            print("⚠️  /admin/events top-5 doesn't show the new registration (check skip/pagination)")
            results["events_api"] = False

    ok = results.get("sse_live") and results.get("ws_live") and results.get("events_api")
    print(f"\nRESULT: {'ALL REAL-TIME PASS ✅✅✅' if ok else 'SOME FAILED ❌'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
