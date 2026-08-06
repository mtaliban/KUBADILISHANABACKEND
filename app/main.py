import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .events.subscriber import start_subscriber, stop_subscriber
from .modules.auth.routes import router as auth_router
from .modules.users.routes import router as users_router
from .modules.locations.routes import router as locations_router
from .modules.matches.routes import router as matches_router
from .modules.messaging.routes import router as messages_router, ws_router
from .modules.admin.routes import router as admin_router

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

os.environ["_KV_STARTED_AT"] = datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def lifespan(app: FastAPI):
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

**Event-driven core:** MQTT broker (Mosquitto local, or HiveMQ Cloud / EMQX Cloud
in production). Every mutation publishes an event; subscribers (matching + analytics)
run in the same process for simplicity.

**Broker config:** set env vars `MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`,
`MQTT_PASSWORD`, `MQTT_USE_TLS`.""",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# All feature routers
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(locations_router)
app.include_router(matches_router)
app.include_router(messages_router)
app.include_router(admin_router)
app.include_router(ws_router)  # /ws?token=


@app.get("/", tags=["ops"])
async def root():
    return {"name": "Kubadilishana Vituo Backend", "version": "1.0.0",
            "docs": "/docs", "modules": ["auth", "users", "locations", "matches", "messaging", "admin"]}


@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok", "started_at": os.environ.get("_KV_STARTED_AT")}
