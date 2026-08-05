# MQTT Topics — Kubadilishana Vituo

All events use JSON payloads. Timestamps are ISO 8601 UTC.

## User lifecycle
| Topic | Publisher | Payload keys |
|---|---|---|
| `kv/user/registered` | auth-service | user_id, category, cadre_code, current_station, desired_destinations, occurred_at |
| `kv/user/profile_updated` | user-service | user_id, changed_fields, occurred_at |
| `kv/user/destination_changed` | user-service | user_id, desired_destinations, occurred_at |
| `kv/user/station_changed` | user-service | user_id, current_station, occurred_at |
| `kv/user/deleted` | user-service | user_id, occurred_at |

## Matching
| Topic | Publisher | Payload keys |
|---|---|---|
| `kv/match/found` | match-service | user_a_id, user_b_id, score, occurred_at |

## QoS
- All user lifecycle events: QoS 1 (at least once)
- Match events: QoS 1
