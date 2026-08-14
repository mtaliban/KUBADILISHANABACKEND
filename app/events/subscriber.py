"""Background MQTT subscribers running inside the same backend process.

Two responsibilities:
1) Analytics: log every event to Mongo `event_log` and append to daily CSV.
2) Matching: on user.registered / destination_changed / station_changed /
   updated_by_admin, recompute matches (stale matches dropped first) and
   publish kv/match/found.
"""
import csv
import json
import logging
import ssl
from datetime import datetime, timezone
from pathlib import Path
from bson import ObjectId
from paho.mqtt import client as mqtt
from ..config import settings
from ..db import get_sync_db
from ..modules.matches.matching import match_score
from .topics import (
    TOPIC_ALL, TOPIC_USER_REGISTERED, TOPIC_USER_PROFILE_UPDATED,
    TOPIC_USER_DESTINATION_CHANGED, TOPIC_USER_STATION_CHANGED,
    TOPIC_USER_UPDATED_BY_ADMIN,
    TOPIC_MATCH_FOUND,
    TOPIC_MESSAGE_SENT, TOPIC_CALL_INITIATED,
    TOPIC_PAYMENT_SUBMITTED, TOPIC_PAYMENT_APPROVED, TOPIC_PAYMENT_REJECTED,
    TOPIC_ANNOUNCEMENT,
)

logger = logging.getLogger(__name__)
_sub: mqtt.Client | None = None


def _csv_path_for_today() -> Path:
    d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    Path(settings.csv_output_dir).mkdir(parents=True, exist_ok=True)
    return Path(settings.csv_output_dir) / f"events_{d}.csv"


def _append_csv(row: dict) -> None:
    path = _csv_path_for_today()
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["occurred_at", "topic", "event_type", "actor_user_id", "payload_json"], extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerow(row)


def _log_event(msg) -> None:
    db = get_sync_db()
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception as e:
        logger.exception(f"bad payload on {msg.topic}: {e}")
        return
    now = datetime.now(timezone.utc)
    event_type = payload.get("event", "unknown")
    actor = payload.get("user_id") or payload.get("user_a_id") or payload.get("from_user_id")
    db.event_log.insert_one({
        "event_type": event_type, "topic": msg.topic,
        "actor_user_id": actor, "payload": payload, "occurred_at": now,
    })
    _append_csv({
        "occurred_at": now.isoformat(), "topic": msg.topic,
        "event_type": event_type, "actor_user_id": actor or "",
        "payload_json": json.dumps(payload, default=str),
    })


def _recompute_matches(user_id: str, client: mqtt.Client) -> int:
    db = get_sync_db()
    user = db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        return 0
    # Drop stale matches first so a cadre/category/station change never leaves
    # obsolete pairs behind.
    db.matches.delete_many({"$or": [{"user_a_id": user_id}, {"user_b_id": user_id}]})
    q = {"_id": {"$ne": user["_id"]}, "category": user["category"], "cadre_code": user["cadre_code"], "status": "active"}
    now = datetime.now(timezone.utc)
    saved = 0
    for cand in db.users.find(q):
        score = match_score(user, cand)
        if score <= 0:
            continue
        pair = tuple(sorted([str(user["_id"]), str(cand["_id"])]))
        db.matches.update_one(
            {"user_a_id": pair[0], "user_b_id": pair[1]},
            {"$set": {"user_a_id": pair[0], "user_b_id": pair[1], "score": score, "matched_at": now, "status": "new"}},
            upsert=True,
        )
        saved += 1
        client.publish(TOPIC_MATCH_FOUND, json.dumps({
            "event": "match.found",
            "user_a_id": pair[0], "user_b_id": pair[1],
            "score": score, "occurred_at": now.isoformat(),
        }), qos=1)
    logger.info(f"recomputed matches for {user_id}: {saved}")
    return saved


def _push_to_users(payload: dict, user_ids: list[str]) -> None:
    """Push a WS message to a list of user ids (from a background thread)."""
    from ..modules.messaging.ws_manager import manager
    import asyncio

    async def push():
        for uid in user_ids:
            if uid:
                await manager.send_to_user(uid, payload)

    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(push())
        loop.close()
    except Exception as e:
        logger.exception(f"WS push failed: {e}")


def _push_batch_to_users(batch: list[tuple[dict, str]]) -> None:
    """Push many WS payloads in ONE event loop (from a background thread)."""
    from ..modules.messaging.ws_manager import manager
    import asyncio

    async def push():
        for payload, uid in batch:
            if uid:
                await manager.send_to_user(uid, payload)

    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(push())
        loop.close()
    except Exception as e:
        logger.exception(f"WS push failed: {e}")


def _admin_user_ids(db) -> list[str]:
    return [str(u["_id"]) for u in db.users.find({"is_admin": True}, {"_id": 1})]


def _match_partners(db, uid: str) -> list[str]:
    partners: set[str] = set()
    for m in db.matches.find({"$or": [{"user_a_id": uid}, {"user_b_id": uid}]}, {"user_a_id": 1, "user_b_id": 1}):
        if m["user_a_id"] == uid:
            partners.add(m["user_b_id"])
        else:
            partners.add(m["user_a_id"])
    return list(partners)


def _relevant_registration_recipients(db, payload: dict) -> list[str]:
    """Who should hear about a new registration.

    Relevant = people whose region the newcomer wants to come to (target)
    AND the newcomer's CURRENT region is a source they watch:
      - followed_regions (ikiwa imewekwa) — mikoa waliyoichagua
      - vinginevyo (default) — mikoa wanayotaka kwenda (desired_destinations)
      - kama hana destinations wala follows → hupata wale wanaokuja mkoa wake
    """
    new_station = payload.get("current_station") or {}
    new_region_id = new_station.get("region_id")
    new_category = payload.get("category")
    new_dest_ids = {d.get("region_id") for d in (payload.get("desired_destinations") or []) if d.get("region_id")}
    if not new_dest_ids:
        return []
    q: dict = {"status": "active", "is_admin": {"$ne": True}}
    if new_category:
        q["category"] = new_category  # walimu waone walimu tu, afya waone afya tu
    # Kadha: mwalimu wa MSINGI asikaribiwe na usajili wa mwalimu wa SEKONDARI
    # (na kinyume chake) — elimu inachujwa kwa LEVEL ya kada, sawa na board.
    # Idara nyingine zinaona kada zote za idara yake. Dynamic: kada yenye
    # `level` (Primary/Secondary) = ya ualimu → inachuja kwa level.
    new_cadre = db.cadres.find_one({"code": payload.get("cadre_code"), "category": new_category})
    lvl = (new_cadre or {}).get("level")
    if lvl:
        level_codes = [c["code"] for c in db.cadres.find({"category": new_category, "level": lvl})]
        if level_codes:
            q["cadre_code"] = {"$in": level_codes}
    uid = payload.get("user_id")
    if uid:
        try:
            q["_id"] = {"$ne": ObjectId(uid)}
        except Exception:
            pass
    out: list[str] = []
    for u in db.users.find(q, {"_id": 1, "current_station": 1,
                                "desired_destinations": 1, "followed_regions": 1}):
        st = u.get("current_station") or {}
        if st.get("region_id") not in new_dest_ids:
            continue  # mtu mpya hataki kuja mkoa wao
        watched = u.get("followed_regions") or [
            d.get("region_id") for d in (u.get("desired_destinations") or []) if d.get("region_id")
        ]
        # Hakuna sources zilizowekwa → anapokea wale wanaokuja mkoa wake.
        # Akiwa na sources (follows/destinations) → chanzo lazima kiwe kati yake.
        if not watched or (new_region_id in watched):
            out.append(str(u["_id"]))
    return out


def _generate_notifications(msg, client: mqtt.Client) -> None:
    """Turn events into user-facing notifications (single funnel)."""
    db = get_sync_db()
    # Our own kv/notification/* events must not generate more notifications.
    if msg.topic.startswith("kv/notification/"):
        return
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception:
        return

    pending: list[tuple[str, str, str, str, dict]] = []  # (uid, type, title, body, data)
    # Live WS payloads (rich events separate from notifications) — one batch.
    ws_batch: list[tuple[dict, str]] = []

    def notify(user_ids: list[str], ntype: str, title: str, body: str, data: dict | None = None):
        for uid in user_ids:
            if uid:
                pending.append((uid, ntype, title, body, data or {}))

    topic = msg.topic
    if topic == TOPIC_USER_REGISTERED:
        uid = payload.get("user_id")
        u = db.users.find_one({"_id": ObjectId(uid)}, {"full_name": 1}) if uid else None
        name = (u or {}).get("full_name", "Mtumiaji mpya")
        notify(_admin_user_ids(db), "user.registered", f"{name} amejiunga 🎉",
               f"Kada: {payload.get('cadre_code')}", {"user_id": uid})
        # Relevant users tu: wale ambao mtu mpya anataka kuja mkoa wao, kutoka
        # mkoa wa chanzo wanaoufuatia (default = mikoa yao ya destination).
        relevant = _relevant_registration_recipients(db, payload)
        notify(relevant, "user.registered",
               f"{name} amejiunga na jukwaa 🎉",
               f"Kada: {payload.get('cadre_code')} — karibu!", {"user_id": uid})
        # Live WS fanout (rich payload) — request feed ya Uber inahitaji data
        # kamili bila MQTT kwenye browser (browser inasikia kupitia WS token).
        for other in relevant:
            ws_batch.append((dict(payload), other))
    elif topic == TOPIC_MATCH_FOUND:
        a_id, b_id = payload.get("user_a_id"), payload.get("user_b_id")
        score = round(float(payload.get("score") or 0) * 100)
        a = db.users.find_one({"_id": ObjectId(a_id)}, {"full_name": 1}) if a_id else None
        b = db.users.find_one({"_id": ObjectId(b_id)}, {"full_name": 1}) if b_id else None
        if a_id and b:
            notify([b_id], "match.found", f"{b['full_name']}, mtu mpya wa kubadilishana nawe 🎯",
                   f"{a['full_name']} — score {score}%", {"user_id": a_id, "score": payload.get("score")})
        if b_id and a:
            notify([a_id], "match.found", f"{a['full_name']}, mtu mpya wa kubadilishana nawe 🎯",
                   f"{b['full_name']} — score {score}%", {"user_id": b_id, "score": payload.get("score")})
    elif topic.startswith(TOPIC_MESSAGE_SENT + "/"):
        to_id = topic.rsplit("/", 1)[1]
        notify([to_id], "message.sent", f"Ujumbe kutoka {payload.get('from_full_name', 'mtumiaji')} 💬",
               (payload.get("text") or "")[:120], {"from_user_id": payload.get("from_user_id")})
    elif topic.startswith(TOPIC_CALL_INITIATED + "/"):
        to_id = topic.rsplit("/", 1)[1]
        # Weka namba ya mpigaji kwenye data — click ya notification inampigia
        # moja kwa moja (tel:), siyo kumpeleka tu kwenye chat.
        caller = None
        try:
            caller = db.users.find_one({"_id": ObjectId(payload.get("from_user_id"))},
                                       {"phone_primary": 1})
        except Exception:
            pass
        notify([to_id], "call.initiated", f"{payload.get('from_full_name', 'Mtu')} amekupigia 📞",
               "Angalia simu yako — mpigie tena",
               {"from_user_id": payload.get("from_user_id"),
                "from_phone": (caller or {}).get("phone_primary")})
    elif topic.startswith(TOPIC_PAYMENT_SUBMITTED + "/"):
        amount = payload.get("amount") or 0
        notify(_admin_user_ids(db), "payment.submitted", "Mchango mpya unahitaji uthibitisho 💰",
               f"TZS {amount:,} — angalia SMS na uthibitishe", {"order_id": payload.get("order_id")})
    elif topic.startswith(TOPIC_PAYMENT_APPROVED + "/"):
        uid = topic.rsplit("/", 1)[1]
        notify([uid], "payment.approved", "Mchango wako umekubaliwa ✓",
               f"TZS {payload.get('amount') or 0:,} — asante!", {"order_id": payload.get("order_id")})
    elif topic.startswith(TOPIC_PAYMENT_REJECTED + "/"):
        uid = topic.rsplit("/", 1)[1]
        notify([uid], "payment.rejected", "Mchango wako umekataliwa ✗",
               "Wasiliana na admin kwa maelezo", {"order_id": payload.get("order_id")})
    elif topic == TOPIC_USER_PROFILE_UPDATED:
        uid = payload.get("user_id")
        u = db.users.find_one({"_id": ObjectId(uid)}, {"full_name": 1}) if uid else None
        if u:
            notify(_match_partners(db, uid), "user.profile_updated",
                   f"{u['full_name']} amesasisha wasifu wake 👤",
                   "Angalia maelezo mapya kwenye dashboard", {"user_id": uid})
    elif topic.startswith(TOPIC_ANNOUNCEMENT + "/"):
        # Admin tangazo → notification kwa mtu aliyelengwa
        uid = topic.rsplit("/", 1)[1]
        notify([uid], "announcement", payload.get("title", "Tangazo la admin 📢"),
               payload.get("message", "")[:160],
               {"announcement_id": payload.get("announcement_id")})
        # Live WS event kwa recipient — megaphone/banner zinabadilika papo hapo.
        # Use event name "announcement" so the browser hook (useLiveEvents) matches.
        ws_payload = dict(payload)
        ws_payload["event"] = "announcement"
        ws_batch.append((ws_payload, uid))

    # Persist + publish MQTT for every queued notification, then push WS in ONE loop.
    if pending:
        for uid, ntype, title, body, data in pending:
            doc = {
                "user_id": uid, "type": ntype, "title": title, "body": body,
                "data": data, "read": False, "created_at": datetime.now(timezone.utc),
            }
            db.notifications.insert_one(doc)
            notif_payload = {
                "event": "notification", "notification_id": str(doc["_id"]), "type": ntype,
                "title": title, "body": body, "data": data,
                "occurred_at": doc["created_at"].isoformat(),
            }
            # NOTE: no more MQTT publish here — the browser listens on the
            # authenticated WebSocket (see useLiveEvents). Publishing these
            # to MQTT too would just duplicate traffic on our own broker.
            ws_batch.append((notif_payload, uid))
    if ws_batch:
        _push_batch_to_users(ws_batch)


def _fanout_match_to_ws(payload: dict) -> None:
    """When kv/match/found fires, push it via WebSocket to both matched users."""
    from ..modules.messaging.ws_manager import manager
    import asyncio
    db = get_sync_db()
    a_id = payload.get("user_a_id"); b_id = payload.get("user_b_id")
    if not (a_id and b_id):
        return
    a = db.users.find_one({"_id": ObjectId(a_id)}, {"full_name": 1, "phone_primary": 1, "cadre_display": 1, "current_station": 1})
    b = db.users.find_one({"_id": ObjectId(b_id)}, {"full_name": 1, "phone_primary": 1, "cadre_display": 1, "current_station": 1})
    if not (a and b):
        return

    async def push_both():
        await manager.send_to_user(a_id, {
            "event": "match.found", "score": payload.get("score"),
            "occurred_at": payload.get("occurred_at"),
            "candidate": {"user_id": str(b["_id"]), "full_name": b["full_name"],
                          "phone_primary": b["phone_primary"], "cadre_display": b.get("cadre_display"),
                          "current_station": b.get("current_station")},
        })
        await manager.send_to_user(b_id, {
            "event": "match.found", "score": payload.get("score"),
            "occurred_at": payload.get("occurred_at"),
            "candidate": {"user_id": str(a["_id"]), "full_name": a["full_name"],
                          "phone_primary": a["phone_primary"], "cadre_display": a.get("cadre_display"),
                          "current_station": a.get("current_station")},
        })

    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(push_both())
        loop.close()
    except Exception as e:
        logger.exception(f"fanout match to WS failed: {e}")


def _on_connect(client, userdata, flags, rc, props):
    logger.info(f"backend MQTT subscriber connected rc={rc}")
    client.subscribe(TOPIC_ALL, qos=1)


def _on_message(client, userdata, msg):
    # 1) always log
    try:
        _log_event(msg)
    except Exception as e:
        logger.exception(f"log_event failed: {e}")

    # 2) matching triggers (incl. admin edits — fixes matches never recomputing
    #    when an admin changed station/destinations directly)
    if msg.topic in (TOPIC_USER_REGISTERED, TOPIC_USER_DESTINATION_CHANGED,
                     TOPIC_USER_STATION_CHANGED, TOPIC_USER_UPDATED_BY_ADMIN):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            uid = payload.get("user_id")
            if uid:
                _recompute_matches(uid, client)
        except Exception as e:
            logger.exception(f"match trigger failed: {e}")

    # 3) turn events into user-facing notifications
    try:
        _generate_notifications(msg, client)
    except Exception as e:
        logger.exception(f"notification generation failed: {e}")

    # 4) fanout match.found via WebSocket (Uber-style live notification)
    if msg.topic == TOPIC_MATCH_FOUND:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            _fanout_match_to_ws(payload)
        except Exception as e:
            logger.exception(f"WS fanout failed: {e}")


def start_subscriber() -> mqtt.Client:
    global _sub
    _sub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{settings.mqtt_client_prefix}-sub")
    if settings.mqtt_username:
        _sub.username_pw_set(settings.mqtt_username, settings.mqtt_password)
    if settings.mqtt_use_tls:
        _sub.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    _sub.on_connect = _on_connect
    _sub.on_message = _on_message
    _sub.connect(settings.mqtt_host, settings.mqtt_port, keepalive=60)
    _sub.loop_start()
    return _sub


def stop_subscriber():
    global _sub
    if _sub:
        _sub.loop_stop()
        _sub.disconnect()
        _sub = None
