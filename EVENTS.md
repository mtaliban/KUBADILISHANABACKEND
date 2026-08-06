# Events, Broker & Pub/Sub Guide

## 🔌 MQTT Broker

Backend inatumia MQTT broker moja kwa events zote. **Local dev** — Mosquitto ndani ya docker. **Production/online** — pendekezo langu ni **HiveMQ Cloud (Serverless)** au **EMQX Cloud** (zote zina free tier).

### Configure via env vars

| Env var | Local (Mosquitto) | HiveMQ Cloud | EMQX Cloud |
|---|---|---|---|
| `MQTT_HOST` | `mosquitto` | `xxxxx.s2.eu.hivemq.cloud` | `xxxxx.emqx.cloud` |
| `MQTT_PORT` | `1883` | `8883` | `8883` |
| `MQTT_USERNAME` | *(blank)* | your username | your username |
| `MQTT_PASSWORD` | *(blank)* | your password | your password |
| `MQTT_USE_TLS` | `false` | `true` | `true` |
| `NEXT_PUBLIC_MQTT_WS` | `ws://localhost:9001` | `wss://xxxxx.s2.eu.hivemq.cloud:8884/mqtt` | `wss://xxxxx.emqx.cloud:8084/mqtt` |

### How to get HiveMQ Cloud (dakika 5)
1. https://console.hivemq.cloud/ → Sign up (bure)
2. Create cluster (Serverless / Starter)
3. Access management → create credentials
4. Copy connection strings hapo juu → paste kwenye `.env`
5. Restart backend: `docker compose restart backend`

Broker inasapoti MQTT 3.1.1 na 5.0. QoS 1 default (at-least-once) — safi kwa events zetu.

---

## 📡 Topics — kila event, publisher, subscriber

| Topic | Publisher (module) | Subscribers | Payload keys |
|---|---|---|---|
| `kv/user/registered` | **auth** — POST /auth/register | matching (recompute), analytics (log), frontend (live dashboard) | `user_id`, `category`, `cadre_code`, `subjects`, `current_station`, `desired_destinations`, `occurred_at` |
| `kv/user/profile_updated` | **users** — PATCH /users/me | analytics | `user_id`, `changed_fields`, `occurred_at` |
| `kv/user/destination_changed` | **users** — PUT /users/me/destinations | matching (recompute), analytics, frontend | `user_id`, `desired_destinations`, `occurred_at` |
| `kv/user/station_changed` | **users** — PUT /users/me/station | matching (recompute), analytics | `user_id`, `current_station`, `occurred_at` |
| `kv/match/found` | **matching** subscriber (background) | analytics (log), frontend (dashboard live badge, notifications) | `user_a_id`, `user_b_id`, `score`, `occurred_at` |
| `kv/message/sent/{recipient_user_id}` | **messaging** — POST /messages | analytics, frontend (recipient auto-refresh chat list) | `message_id`, `conversation_id`, `from_user_id`, `from_full_name`, `to_user_id`, `text`, `created_at` |
| `kv/call/initiated/{recipient_user_id}` | **messaging** — POST /messages/call | analytics, frontend (recipient notification) | `call_id`, `from_user_id`, `from_full_name`, `to_user_id`, `initiated_at` |

**Wildcards** unaziweza subscribe:
- `kv/user/#` — user lifecycle zote
- `kv/match/#` — matching events zote
- `kv/message/#` — messages zote (zote za watumiaji)
- `kv/call/#` — calls zote
- `kv/#` — kila event

---

## 🔄 Event flow — mtu anajisajili

```
POST /auth/register
   ↓
auth module: insert user in Mongo + publish "kv/user/registered"
                                                 ↓
                                        Mosquitto (broker)
                                                 ↓
                        ┌────────────────────────┼────────────────────────┐
                        ↓                        ↓                        ↓
              matching subscriber      analytics subscriber      frontend (MQTT.js browser)
              (same process,           (same process,            (browser subscribed via ws:9001)
               background thread)      background thread)
                        ↓                        ↓                        ↓
              recompute matches         insert event_log +        live badge, dashboard reload
              publish "kv/match/found"   append CSV daily
                        ↓
              analytics subscriber picks up match.found → logs it too
```

**Note:** matching + analytics run **inside the same backend process** (single monolith) — hakuna network overhead, na MQTT ndio inatumika kwa loose coupling tu ambayo baadae itakuruhusu kutoa modules kama services tofauti.

---

## 📊 Where events are stored

1. **`event_log` Mongo collection** — every event, forever
2. **CSV daily rotating** — `backend/csv_output/events_YYYY-MM-DD.csv`
3. **Admin API** — `GET /admin/events`, `GET /admin/live-events` (SSE stream), `GET /admin/csv/list`, `GET /admin/csv/download/{name}`

---

## 🧪 Watch events live from terminal

```bash
docker exec -it kv_mosquitto mosquitto_sub -t 'kv/#' -v
```

Register a user in another shell:
```bash
curl -X POST http://localhost:8080/auth/register -H 'Content-Type: application/json' -d '{...}'
```

You'll see the event flash in your subscriber shell instantly.
