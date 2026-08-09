# Backend — Kubadilishana Vituo

Single monolith backend (FastAPI + MongoDB + MQTT + Redis) — event-driven.
Kila kitu kinakaa kwenye backend yako mwenyewe: MongoDB, Mosquitto broker,
Redis, na FastAPI zote kwenye docker-compose moja.

## Run backend only

```bash
# 1) Weka .env (muhimu: JWT_SECRET, MONGO_ROOT_PASSWORD, MQTT_PASSWORD)
cp .env.example .env   # au uunde manually

# 2) Anza stack nzima (mongodb + mosquitto + redis + backend)
docker compose up -d --build

# 3) Load reference data (regions/districts/schools/facilities/cadres/subjects)
docker compose exec backend python scripts/seed_data.py
```

Angalia kila kitu kiko up:

```bash
docker compose ps
curl http://localhost:8000/health   # → {"status":"ok"}
```

> Backend inasikiliza kwenye **port 8000**. (Root docker-compose ina-expose
> backend kwenye 8080 kwa full stack — angalia root `docker-compose.yml`.)

## Architecture

```
FastAPI (app/) ──► MongoDB (mongodb:27017)     database self-hosted
      │        ──► Mosquitto (mosquitto:1883)  MQTT broker self-hosted
      │        ──► Redis (redis:6379)          cache self-hosted
      │
      └── publishes events (kv/user/registered, kv/match/found, ...)
          subscribers (matching + analytics + notifications) run in-process
```

## Tests

```bash
pip install -r requirements.txt -r tests/requirements-test.txt
pytest -v
```

## Watch events live

```bash
docker exec -it kv_mosquitto mosquitto_sub -t 'kv/#' -v
```

Register a user (curl to `/auth/register`) and events flash live in the subscriber terminal.
