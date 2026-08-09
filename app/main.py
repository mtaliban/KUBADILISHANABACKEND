import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

from .config import settings
from .events.subscriber import start_subscriber, stop_subscriber
from .modules.auth.routes import router as auth_router
from .modules.users.routes import router as users_router
from .modules.locations.routes import router as locations_router
from .modules.matches.routes import router as matches_router
from .modules.messaging.routes import router as messages_router, ws_router
from .modules.admin.routes import router as admin_router
from .modules.payments.routes import router as payments_router
from .modules.notifications.routes import router as notifications_router
from .modules.announcements.routes import router as announcements_router

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

os.environ["_KV_STARTED_AT"] = datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.jwt_secret in ("change-me-in-production", ""):
        logger.warning("⚠️  JWT_SECRET ni default! Weka env JWT_SECRET kabla ya production — "
                       "vinginevyo mtu yeyote anaweza kuforge tokens.")
    logger.info(f"Starting backend. MQTT: {settings.mqtt_host}:{settings.mqtt_port}, Mongo: OK")
    try:
        start_subscriber()
    except Exception as e:
        logger.exception(f"MQTT subscriber failed to start: {e}")
    yield
    stop_subscriber()


app = FastAPI(
    title="Kubadilishana Vituo — Backend",
    description="""Single monolith backend for the whole platform.

**Modules:** auth · users · locations · matches · messaging · admin

**Event-driven core:** MQTT broker (self-hosted Mosquitto kwenye docker-compose —
hakuna cloud broker). Every mutation publishes an event; subscribers (matching +
analytics) run in the same process for simplicity.

**Broker config:** set env vars `MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`,
`MQTT_PASSWORD`, `MQTT_USE_TLS`.""",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    # Accept any Cloudflare quick-tunnel origin (https://xxxx.trycloudflare.com)
    # plus localhost variants — otherwise the browser blocks cross-origin
    # requests and users see "invalid credentials" on the public URL.
    allow_origin_regex=r"https://.*\.trycloudflare\.com|http://localhost(:\d+)?|https://localhost(:\d+)?|http://127\.0\.0\.1(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Basic hardening: clickjacking, sniffing, MIME, referrer leaks."""
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    resp.headers["X-XSS-Protection"] = "1; mode=block"
    return resp

# All feature routers
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(locations_router)
app.include_router(matches_router)
app.include_router(messages_router)
app.include_router(admin_router)
app.include_router(payments_router)
app.include_router(notifications_router)
app.include_router(announcements_router)
app.include_router(ws_router)  # /ws?token=


@app.get("/", tags=["ops"])
async def root():
    return {"name": "Kubadilishana Vituo Backend", "version": "1.0.0",
            "docs": "/docs", "modules": ["auth", "users", "locations", "matches", "messaging", "admin"]}


@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok", "started_at": os.environ.get("_KV_STARTED_AT")}


# ─── Prometheus metrics ──────────────────────────────
REQ_COUNT = Counter("kv_http_requests_total", "HTTP requests", ["method", "path", "status"])
REQ_LATENCY = Histogram("kv_http_request_seconds", "HTTP request latency", ["method", "path"])


@app.middleware("http")
async def _prom_middleware(request: Request, call_next):
    start = time.time()
    resp = await call_next(request)
    elapsed = time.time() - start
    # normalize path template to avoid explosion (skip metrics on /metrics itself)
    path = request.url.path
    if path != "/metrics":
        # collapse dynamic segments
        norm = "/" + "/".join(p if not (len(p) == 24 and all(c in "0123456789abcdef" for c in p)) else ":id" for p in path.strip("/").split("/"))
        REQ_COUNT.labels(request.method, norm, resp.status_code).inc()
        REQ_LATENCY.labels(request.method, norm).observe(elapsed)
    return resp


@app.get("/metrics", tags=["ops"])
async def metrics():
    """Prometheus scrape endpoint."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
