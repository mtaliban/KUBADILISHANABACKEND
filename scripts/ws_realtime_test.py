"""Live WebSocket real-time verification for Kubadilishana Vituo.

Connects two real WebSocket clients (the same way the browser does via
frontend/src/lib/liveSocket.ts) and proves the chat is genuinely real-time:

  1. Presence fanout      — B connects → A sees B online (WS broadcast)
  2. Typing indicator     — A types   → B sees "anaandika..."  (relayed)
  3. Message delivery     — A sends   → B receives in <1s (latency measured)
  4. Delivered tick (✓✓)  — sender gets delivered_at when B is online
  5. Read receipt (blue ✓✓) — B marks read → A's message flips to blue live
  6. Presence offline     — B disconnects → A sees B offline (WS broadcast)

Usage (backend running at http://localhost:8080):
    cd backend && .venv/bin/python scripts/ws_realtime_test.py

Requires two non-admin test users. By default uses 0765123480/secret123 and
0760000002/secret123 — override with env: USER_A_PHONE, USER_A_PASS, etc.
"""
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("KV_BASE", "http://localhost:8080")
WS = os.environ.get("KV_WS", "ws://localhost:8080/ws")

A_PHONE = os.environ.get("USER_A_PHONE", "0765123480")
A_PASS = os.environ.get("USER_A_PASS", "secret123")
B_PHONE = os.environ.get("USER_B_PHONE", "0760000002")
B_PASS = os.environ.get("USER_B_PASS", "secret123")


def rest(method, path, body=None, token=None):
    req = urllib.request.Request(f"{BASE}{path}", method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": e.read().decode()[:200]}


async def connect_ws(token, name, events):
    import websockets
    ws = await websockets.connect(f"{WS}?token={token}")
    events[name] = []

    async def reader():
        async for raw in ws:
            try:
                events[name].append(json.loads(raw))
            except Exception:
                pass

    asyncio.create_task(reader())
    return ws


async def main():
    results = {}
    a = rest("POST", "/auth/login", {"phone": A_PHONE, "password": A_PASS})
    b = rest("POST", "/auth/login", {"phone": B_PHONE, "password": B_PASS})
    if "error" in a or "error" in b:
        print("LOGIN FAIL:", a.get("detail", a), b.get("detail", b))
        sys.exit(1)
    a_id, a_tok = a["user_id"], a["access_token"]
    b_id, b_tok = b["user_id"], b["access_token"]
    print(f"User A: {a['full_name']} ({a_id})")
    print(f"User B: {b['full_name']} ({b_id})")

    events = {}
    wsA = await connect_ws(a_tok, "A", events)
    wsB = await connect_ws(b_tok, "B", events)
    print("WS A + WS B connected ✅")

    # 1. Presence fanout
    await asyncio.sleep(1.5)
    pres = [e for e in events["A"] if e.get("event") == "presence"]
    results["presence_online"] = any(
        e.get("user_id") == b_id and e.get("online") for e in pres
    )
    print(f"Presence online:  A aliona B online → {'✅' if results['presence_online'] else '❌'}")

    # 2. Typing
    await wsA.send(json.dumps({"type": "typing", "to": b_id, "on": True}))
    await asyncio.sleep(1.0)
    results["typing"] = any(
        e.get("event") == "typing" and e.get("from_user_id") == a_id and e.get("on")
        for e in events["B"]
    )
    print(f"Typing:           B aliona 'anaandika...' → {'✅' if results['typing'] else '❌'}")

    # 3. Message delivery latency
    t0 = time.monotonic()
    sent = rest("POST", "/messages",
                {"to_user_id": b_id, "text": f"WS live test {time.time()}"}, a_tok)
    if "error" in sent:
        print("SEND FAIL:", sent)
        sys.exit(1)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        got_msgs = [
            e for e in events["B"]
            if e.get("event") == "message.sent" and e.get("message_id") == sent["message_id"]
        ]
        if got_msgs:
            break
        await asyncio.sleep(0.05)
    results["message_delivery"] = bool(got_msgs)
    latency = (time.monotonic() - t0) * 1000
    print(f"Message delivery: B alipokea kwa {latency:.0f}ms → {'✅' if results['message_delivery'] else '❌'}")

    # 4. Delivered tick
    results["delivered"] = bool(sent.get("delivered_at"))
    print(f"Delivered (✓✓):   sender alipata delivered_at → {'✅' if results['delivered'] else '❌'}")

    # 5. Read receipt (blue tick)
    await asyncio.sleep(0.5)
    t0 = time.monotonic()
    rest("POST", f"/messages/mark-read/{a_id}", {}, b_tok)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        rc = [
            e for e in events["A"]
            if e.get("event") == "read.receipt" and e.get("read_by") == b_id
        ]
        if rc:
            break
        await asyncio.sleep(0.05)
    results["read_receipt"] = bool(rc)
    latency2 = (time.monotonic() - t0) * 1000
    print(f"Read receipt:     A aliona blue tick kwa {latency2:.0f}ms → {'✅' if results['read_receipt'] else '❌'}")

    # 6. Presence offline
    await wsB.close()
    await asyncio.sleep(1.5)
    results["presence_offline"] = any(
        e.get("event") == "presence" and e.get("user_id") == b_id and not e.get("online")
        for e in events["A"]
    )
    print(f"Presence offline: A aliona B offline → {'✅' if results['presence_offline'] else '❌'}")

    await wsA.close()
    ok = all(results.values())
    print(f"\nRESULT: {'ALL REAL-TIME PASS ✅✅✅' if ok else 'SOME FAILED ❌'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
