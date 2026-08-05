# Backend — Kubadilishana Vituo

Event-driven microservices (FastAPI + MongoDB + MQTT + Redis).

## Services

| Service | Port | Role | Publishes | Subscribes |
|---|---|---|---|---|
| auth-service | 8001 | Register, Login, JWT | `kv/user/registered` | — |
| user-service | 8002 | Profile, station, destinations | `kv/user/profile_updated` `destination_changed` `station_changed` | — |
| location-service | 8003 | Cascading location + cadre lookup | — | — |
| match-service | 8004 | Compute matches | `kv/match/found` | `kv/user/#` |
| analytics-service | 8005 | Log all events (Mongo + CSV) | — | `kv/user/#`, `kv/match/#` |

## Run backend only

```bash
cp ../.env.example .env
docker compose up -d --build
```

Then load reference data (regions/districts/schools/facilities/cadres/subjects):

```bash
python scripts/seed_data.py
```

## Tests

```bash
cd services/auth-service
pip install -r requirements.txt -r tests/requirements-test.txt
pytest -v
```

## Watch events live

```bash
docker exec -it kv_mosquitto mosquitto_sub -t 'kv/#' -v
```

Register a user (curl to `/auth/register`) and events flash live in the subscriber terminal.
