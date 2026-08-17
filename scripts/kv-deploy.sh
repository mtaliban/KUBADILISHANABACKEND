#!/bin/bash
# ============================================================
# kv-deploy.sh — Auto-deploy backend kwenye EC2.
#
# KAZI YAKE:
#   Inaangalia Docker Hub kila dakika 2 (cron) → ikiona digest
#   mpya ya mtalibani/kubadilishan:latest, inapull + restart
#   backend. Pia inafanya git pull ikiwa EC2 ina-build local.
#
# USANIDI (kwenye EC2, mara moja tu):
#   chmod +x kv-deploy.sh
#   # weka kwenye cron (kila dakika 2):
#   (crontab -l 2>/dev/null; echo "*/2 * * * * /home/ubuntu/KUBADILISHANABACKEND/kv-deploy.sh >> /home/ubuntu/kv-deploy.log 2>&1") | crontab -
#
# RUN MANUAL (sasa hivi):
#   ./kv-deploy.sh
# ============================================================
set -u

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG="/home/ubuntu/kv-deploy.log"
IMAGE="mtalibani/kubadilishan:latest"
DOCKER_HUB_API="https://hub.docker.com/v2/repositories/mtalibani/kubadilishan/tags/latest"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

# ── 1) Git pull (ikiwa EC2 ina-build local) ──────────────────
if [ -d "$APP_DIR/.git" ]; then
  BEFORE=$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null)
  git -C "$APP_DIR" fetch origin main >/dev/null 2>&1
  REMOTE=$(git -C "$APP_DIR" rev-parse origin/main 2>/dev/null)
  if [ -n "$BEFORE" ] && [ -n "$REMOTE" ] && [ "$BEFORE" != "$REMOTE" ]; then
    log "git: mabadiliko yamepatikana ($(echo "$BEFORE" | cut -c1-7) → $(echo "$REMOTE" | cut -c1-7))"
    git -C "$APP_DIR" pull --ff-only origin main >>"$LOG" 2>&1
  fi
fi

# ── 2) Angalia digest ya Docker Hub ──────────────────────────
NEW_DIGEST=$(curl -s --max-time 15 "$DOCKER_HUB_API" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    print(d.get('digest',''))
except Exception:
    print('')
" 2>/dev/null)

CUR_DIGEST=$(docker images --no-trunc --format '{{.Repository}}:{{.Tag}} {{.Digest}}' 2>/dev/null \
  | grep "^mtalibani/kubadilishan:latest " | awk '{print $2}')

if [ -z "$NEW_DIGEST" ]; then
  log "⚠️  Docker Hub haijibu — imeskipped (jaribu tena baada ya dakika 2)"
  exit 0
fi

if [ "$NEW_DIGEST" = "$CUR_DIGEST" ]; then
  exit 0   # tayari iko mpya — hakuna cha kufanya
fi

# ── 3) Pull + restart backend ────────────────────────────────
log "🚀 Digest mpya imepatikana: $CUR_DIGEST → $NEW_DIGEST"
cd "$APP_DIR" || exit 1

docker compose pull backend >>"$LOG" 2>&1
RC=$?
if [ $RC -ne 0 ]; then
  log "❌ docker compose pull imefeli (rc=$RC) — bado inajaribu up"
  # Jaribu tena bila pull (kama compose ina build local)
  docker compose up -d --build backend >>"$LOG" 2>&1
else
  docker compose up -d backend >>"$LOG" 2>&1
fi

sleep 5
HEALTH=$(curl -s --max-time 8 "http://localhost:8000/health" | head -c 120)
log "✅ Deploy imekamilika — health: $HEALTH"
