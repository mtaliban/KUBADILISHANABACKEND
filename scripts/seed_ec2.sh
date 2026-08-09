#!/bin/bash
# ============================================================
# Seed reference data (regions/districts/schools/facilities/
# cadres/subjects) kwenye kv_mongodb — idempotent.
# Inaruka ikiwa data tayari ipo (regions count > 0).
# Ina-run ndani ya backend container (pymongo iko hapo).
# ============================================================
set -e
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if ! docker ps --format '{{.Names}}' | grep -q '^kv_mongodb$'; then
  echo "[seed] kv_mongodb haipo — seed imeskipped"
  exit 0
fi

MONGO_PASS=$(grep MONGO_ROOT_PASSWORD "$APP_DIR/.env" | cut -d= -f2)
COUNT=$(docker exec kv_mongodb mongo --quiet \
  "mongodb://admin:$MONGO_PASS@localhost:27017/kubadilishana_vituo?authSource=admin" \
  --eval 'print(db.regions.countDocuments({}))' 2>/dev/null || echo "err")

if [ "$COUNT" != "0" ]; then
  echo "[seed] regions tayari zina data ($COUNT) — seed imeskipped"
  exit 0
fi

echo "[seed] data haipo — ina-seed sasa..."
docker cp "$APP_DIR/scripts/seed_data.py" kv_backend:/tmp/seed_data.py >/dev/null
docker cp "$APP_DIR/tanzania_data/json" kv_backend:/tmp/tz_edu >/dev/null
docker cp "$APP_DIR/tanzania_health_data/json" kv_backend:/tmp/tz_health >/dev/null
docker exec kv_backend env \
  MONGO_URI="mongodb://admin:$MONGO_PASS@mongodb:27017/kubadilishana_vituo?authSource=admin" \
  TZ_EDU_DIR=/tmp/tz_edu \
  TZ_HEALTH_DIR=/tmp/tz_health \
  python3 /tmp/seed_data.py
echo "[seed] seeding imekamilika ✅"
