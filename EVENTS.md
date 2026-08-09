# Events, Broker & Pub/Sub Guide

## 🔌 MQTT Broker

Backend inatumia MQTT broker moja kwa events zote: **Mosquitto self-hosted**,
inayorun ndani ya docker-compose yako (EC2 au local). Hakuna cloud broker —
kila kitu kinakaa kwenye backend yako mwenyewe.

### Configure via env vars

| Env var | Local/EC2 (docker-compose) |
|---|---|
| `MQTT_HOST` | `mosquitto` |
| `MQTT_PORT` | `1883` |
| `MQTT_USERNAME` | `kvuser` (docker-compose ina-generate passwd yenyewe) |
| `MQTT_PASSWORD` | `KvChangeMe2026` (badilisha kwenye .env!) |
| `MQTT_USE_TLS` | `false` |

> Browser haitumi MQTT kabisa — live events zote (chats, arifa, matangazo,
> matches, presence) zinasafiri kupitia **backend WebSocket (`/ws`)** iliyolindwa
> na JWT ya kila user. MQTT (Mosquitto) ni **backend-internal tu**.

Broker inasapoti MQTT 3.1.1 na 5.0. QoS 1 default (at-least-once) — safi kwa events zetu.

---

## 📡 Topics — kila event, publisher, subscriber

| Topic | Publisher (module) | Subscribers | Payload keys |
|---|---|---|---|
| `kv/user/registered` | **auth** — POST /auth/register | matching (recompute), analytics (log), frontend (live dashboard) | `user_id`, `category`, `cadre_code`, `subjects`, `current_station`, `desired_destinations`, `occurred_at` |
| `kv/user/profile_updated` | **users** — PATCH /users/me | analytics | `user_id`, `changed_fields`, `occurred_at` |
| `kv/user/destination_changed` | **users** — PUT /users/me/destinations | matching (recompute), analytics, frontend | `user_id`, `desired_destinations`, `occurred_at` |
| `kv/user/station_changed` | **users** — PUT /users/me/station | matching (recompute), analytics | `user_id`, `current_station`, `occurred_at` |
| `kv/user/prefs_updated` | **users** — PUT /users/me/notification-prefs | analytics | `user_id`, `notification_prefs`, `occurred_at` |
| `kv/user/updated_by_admin` | **admin** — PATCH /admin/users/{id} | **matching (recompute)** — admin edits now trigger re-matching, analytics | `user_id`, `changed_fields`, `occurred_at` |
| `kv/user/deleted` | **admin** — DELETE /admin/users/{id} | analytics | `user_id`, `occurred_at` |
| `kv/user/admin_changed` | **admin** — POST /admin/users/{id}/grant-admin, /revoke-admin | analytics | `user_id`, `is_admin`, `occurred_at` |
| `kv/user/presence` | **messaging** — WS connect/disconnect | analytics, frontend (online badge) | `user_id`, `is_online`, `occurred_at` |
| `kv/user/password_reset_requested` | **auth** — POST /auth/forgot-password | analytics (security audit) | `user_id`, `occurred_at` |
| `kv/user/password_reset_completed` | **auth** — POST /auth/reset-password | analytics (security audit) | `user_id`, `occurred_at` |
| `kv/match/found` | **matching** subscriber (background) | analytics (log), frontend (dashboard live badge, notifications) | `user_a_id`, `user_b_id`, `score`, `occurred_at` |
| `kv/message/sent/{recipient_user_id}` | **messaging** — POST /messages | analytics, frontend (recipient auto-refresh chat list) | `message_id`, `conversation_id`, `from_user_id`, `from_full_name`, `to_user_id`, `text`, `created_at` |
| `kv/call/initiated/{recipient_user_id}` | **messaging** — POST /messages/call | analytics, frontend (recipient notification) | `call_id`, `from_user_id`, `from_full_name`, `to_user_id`, `initiated_at` |
| `kv/payment/submitted/{user_id}` | **payments** — POST /payments/donate | analytics, frontend (toast) | `event`, `order_id`, `amount`, `currency`, `status: verifying`, `occurred_at` |
| `kv/payment/approved/{user_id}` | **payments** — POST /payments/admin/{id}/approve | analytics, frontend (live toast) | `event`, `order_id`, `amount`, `currency`, `status: approved`, `occurred_at` |
| `kv/payment/rejected/{user_id}` | **payments** — POST /payments/admin/{id}/reject | analytics, frontend (live toast) | `event`, `order_id`, `amount`, `currency`, `status: rejected`, `occurred_at` |
| `kv/page/viewed` | **admin** — POST /admin/page-view | analytics (log/audit), live consumers | `user_id`, `user_name`, `path`, `referrer`, `occurred_at` |

> **Reliability note:** `page.viewed` is persisted **directly** by the route into `page_views` (the admin `top_pages` report reads it) and *also* published for the audit stream — telemetry stays reliable even if the broker is down. Similarly, presence events (`kv/user/presence`) and page views are **high-frequency** — they appear in `event_log`/CSV and inflate `events_24h`; filter by topic when analysing stats.

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
                                        Mosquitto (broker, self-hosted)
                                                 ↓
                        ┌────────────────────────┼────────────────────────┐
                        ↓                        ↓                        ↓
              matching subscriber      analytics subscriber      backend WS fanout
              (same process,           (same process,            (browser inasikia via /ws)
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
