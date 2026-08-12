#!/bin/bash
# ============================================================
# Wipe ALL non-admin users + data zao kwenye kv_mongodb —
# ADMINI HUHIFADHIWA. (Ina-run ndani ya backend container.)
# ============================================================
set -e
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if ! docker ps --format '{{.Names}}' | grep -q '^kv_mongodb$'; then
  echo "[wipe] kv_mongodb haipo — wipe imeskipped"
  exit 0
fi

MONGO_PASS=$(grep MONGO_ROOT_PASSWORD "$APP_DIR/.env" | cut -d= -f2)

echo "[wipe] Kuandika script ndani ya container..."
docker cp "$APP_DIR/scripts/wipe_users.py" kv_backend:/tmp/wipe_users.py >/dev/null

echo "[wipe] Kufuta watumiaji wote wasio-admin..."
docker exec kv_backend env \
  MONGO_URI="mongodb://admin:$MONGO_PASS@mongodb:27017/kubadilishana_vituo?authSource=admin" \
  python3 /tmp/wipe_users.py

echo "[wipe] Imekamilika ✅ — admini wamehifadhiwa"
