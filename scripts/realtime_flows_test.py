"""REAL-TIME flows verification (payments + data CRUD + announcements).

Proves, against a LIVE backend:
  1. Payment submit → admin gets WS 'notification' (payment.submitted) LIVE
  2. Admin payment approve → user gets WS 'notification' (payment.approved) LIVE
  3. Data CRUD (subject add) → SSE /admin/live-events shows data.subject_added LIVE
  4. Announcement send → user gets WS 'announcement' event LIVE (banner inabadilika)

Usage:
    cd backend && .venv/bin/python scripts/realtime_flows_test.py
    (MONGO_URI env inahitajika kama SMTP inatuma OTP kwa email)
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


async def login_admin():
    step1 = rest("POST", "/auth/login", {"phone": ADMIN_EMAIL, "password": ADMIN_PASS})
    if "error" in step1 or not step1.get("two_factor_required"):
        print(f"❌ Admin login step1: {step1.get('detail', step1)}")
        sys.exit(1)
    code = step1.get("dev_code") or os.environ.get("ADMIN_OTP", "")
    if not code:
        from datetime import datetime, timedelta, timezone
        from pymongo import MongoClient
        from app.security import hash_password
        uri = os.environ.get("MONGO_URI")
        if not uri:
            print("❌ MONGO_URI inahitajika")
            sys.exit(1)
        code = "123456"
        c = MongoClient(uri, serverSelectionTimeoutMS=5000)
        db = c[os.environ.get("MONGO_DB", "kubadilishana_vituo")]
        admin_doc = db.users.find_one({"email": ADMIN_EMAIL}, {"_id": 1})
        uid = admin_doc["_id"]
        db.login_otps.update_one(
            {"user_id": uid, "purpose": "2fa"},
            {"$set": {"user_id": uid, "email": ADMIN_EMAIL, "purpose": "2fa",
                      "code_hash": hash_password(code),
                      "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
                      "created_at": datetime.now(timezone.utc), "used": False, "attempts": 0}},
            upsert=True)
        c.close()
    step2 = rest("POST", "/auth/login/2fa", {"email": ADMIN_EMAIL, "code": code})
    if "error" in step2 or not step2.get("access_token"):
        print(f"❌ Admin login step2: {step2.get('detail', step2)}")
        sys.exit(1)
    return step2["access_token"]


async def wait_ws_event(ws, event_name, timeout_s, predicate=None):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        if ev.get("event") == event_name and (predicate is None or predicate(ev)):
            return ev
    return None


async def main():
    results = {}
    token = await login_admin()
    print("✅ Admin logged in")

    import websockets
    ws_admin = await websockets.connect(BASE.replace("http", "ws") + f"/ws?token={token}", open_timeout=10)

    # ── TEST A: Payment submit → admin receives notification LIVE ──
    ts = str(int(time.time()))
    user_reg = rest("POST", "/auth/register", {
        "full_name": f"Pay User {ts}", "phone_primary": f"0763{ts[-6:]}",
        "password": "secret123", "category": "health", "cadre_code": "CO",
        "current_station": {"region_id": 17, "region_name": "Mwanza", "district_id": 1701,
                            "district_name": "Nyamagana DC", "facility_id": None, "facility_name": None},
        "desired_destinations": [{"region_id": 1, "region_name": "Arusha", "district_id": 101,
                                  "district_name": "Arusha DC", "facility_id": None, "facility_name": None}]})
    if "error" in user_reg:
        print(f"❌ user register: {user_reg.get('detail')}")
        sys.exit(1)
    user_tok = user_reg["access_token"]
    # submit payment (manual donation via SMS)
    pay = rest("POST", "/payments/donate", {"amount": 5000, "sms_text": f"CONFIRMED test {ts}"}, user_tok)
    if "error" in pay:
        print(f"⚠️ payment submit: {pay.get('detail', pay)}")
        results["payment_admin_notify"] = False
    else:
        ev = await wait_ws_event(ws_admin, "notification", 8,
                                 lambda e: e.get("type") == "payment.submitted")
        if ev:
            print(f"✅ Payment submit → admin alipata notification LIVE: '{ev.get('title')}'")
            results["payment_admin_notify"] = True
        else:
            print("❌ Payment submit → admin hakupata notification within 8s")
            results["payment_admin_notify"] = False

    # ── TEST B: Data CRUD (subject add) → SSE live-events ──
    import subprocess
    import threading
    sse_out = {}

    def sse_reader():
        out = subprocess.run(["curl", "-s", "-N", "--max-time", "10",
                              "-H", f"Authorization: Bearer {token}",
                              f"{BASE}/admin/live-events"],
                             capture_output=True, text=True)
        sse_out["out"] = out.stdout

    st = threading.Thread(target=sse_reader)
    st.start()
    await asyncio.sleep(2)
    subj_code = f"RT{ts[-4:]}"
    add = rest("POST", "/admin/data/subjects",
               {"code": subj_code, "name": f"RealTime Subject {ts}", "level": "Secondary"}, token)
    if "error" in add:
        print(f"⚠️ subject add: {add.get('detail', add)}")
        results["data_sse"] = False
    else:
        st.join(timeout=15)
        out = sse_out.get("out", "")
        got = any(f'"event_type": "data.subject_added"' in out for _ in [0])
        # more precise: parse data lines
        parsed = [json.loads(l[6:]) for l in out.split("\n") if l.startswith("data: ")]
        got = any(e.get("event_type") == "data.subject_added" for e in parsed)
        if got:
            print("✅ Data CRUD (subject add) → SSE live-events iliona data.subject_added LIVE")
            results["data_sse"] = True
        else:
            print("❌ Data CRUD → SSE haikuona data.subject_added")
            results["data_sse"] = False

    # ── TEST C: Admin payment approve → user gets notification LIVE ──
    if not (pay and "error" not in pay):
        results["payment_user_approve"] = False
    else:
        order_id = pay.get("order_id")
        # WS ya user lazima iwe wazi KABLA approve (kinga ya race)
        ws_user = await websockets.connect(BASE.replace("http", "ws") + f"/ws?token={user_tok}", open_timeout=10)
        appr = rest("POST", f"/payments/admin/{order_id}/approve", {}, token)
        if "error" in appr:
            print(f"⚠️ approve: {appr.get('detail', appr)}")
            results["payment_user_approve"] = False
        else:
            ev = await wait_ws_event(ws_user, "notification", 8,
                                     lambda e: e.get("type") == "payment.approved")
            if ev:
                print(f"✅ Admin approve → user alipata notification LIVE: '{ev.get('title')}'")
                results["payment_user_approve"] = True
            else:
                print("❌ Admin approve → user hakupata notification within 8s")
                results["payment_user_approve"] = False
        await ws_user.close()

    # ── TEST D: Announcement send → user gets WS 'announcement' LIVE ──
    # WS ya user iunganishwe KABLA ya kutuma tangazo (kinga ya race).
    ws_user2 = await websockets.connect(BASE.replace("http", "ws") + f"/ws?token={user_tok}", open_timeout=10)
    ann = rest("POST", "/admin/announcements", {
        "title": f"Tangazo Live {ts}", "message": "Hii ni announcement ya real-time test.",
        "audience": "user", "target_user_id": user_reg["user_id"]}, token)
    if "error" in ann:
        print(f"⚠️ announcement: {ann.get('detail', ann)}")
        results["announcement_live"] = False
    else:
        ev = await wait_ws_event(ws_user2, "announcement", 8)
        if ev:
            print(f"✅ Announcement → user alipata WS event LIVE: '{ev.get('title')}'")
            results["announcement_live"] = True
        else:
            print("❌ Announcement → user hakupata WS event within 8s")
            results["announcement_live"] = False
    await ws_user2.close()

    await ws_admin.close()
    ok = all(results.values())
    print(f"\nFLOWS RESULT: {'ALL REAL-TIME PASS ✅✅✅' if ok else 'SOME FAILED ❌'}")
    print(json.dumps(results, indent=2))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
