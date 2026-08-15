import gzip
import io
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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
from .modules.feedback.routes import router as feedback_router

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

class _SSEAwareGzipResponder:
    """Gzip streaming hiyo hiyo ya Starlette, ila SSE haijabanwi kamwe.

    Starlette GZipMiddleware inagzip streaming responses chunk-kwa-chunk,
    lakini gzip stream hifflushi tu baada ya ~8KB. SSE events ni ndogo
    (mamia ya bytes) — zilibaki kwenye buffer na browser ilipata BYTES 0
    kutoka /admin/live-events. Hii ndiyo ilikuwa sababu ya "events page sio
    real-time" — data ilionekana tu baada ya refresh! Sasa SSE inapita
    raw (bila gzip) ili kila event ifike PAPO HAPO.
    """

    def __init__(self, app: ASGIApp, minimum_size: int = 500) -> None:
        self.app = app
        self.minimum_size = minimum_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if "gzip" not in headers.get("Accept-Encoding", ""):
            await self.app(scope, receive, send)
            return
        responder = _GzipResponder(self.app, self.minimum_size)
        await responder(scope, receive, send)


class _GzipResponder:
    """Same streaming gzip logic as Starlette's GZipResponder, with an SSE bypass:
    kwa response za Content-Type: text/event-stream, mabodi yanapita RAW (sio
    gzipped) — vinginevyo gzip buffer inazuia events ndogo kufika kwa muda.
    """

    def __init__(self, app: ASGIApp, minimum_size: int) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.send: Send | None = None
        self.initial_message: Message | None = None
        self.started = False
        self.content_encoding_set = False
        self.is_sse = False
        self.buffer = io.BytesIO()
        self.gzip_file = gzip.GzipFile(mode="wb", fileobj=self.buffer)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.send = send
        with self.buffer, self.gzip_file:
            await self.app(scope, receive, self._send_with_gzip)

    async def _send_with_gzip(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.initial_message = message
            headers = Headers(raw=self.initial_message["headers"])
            self.content_encoding_set = "content-encoding" in headers
            ct = headers.get("Content-Type", "")
            if ct.startswith("text/event-stream"):
                # SSE → usigzip: tuma headers na mabodi yote RAW.
                self.is_sse = True
                await self.send(self.initial_message)
            return
        if self.is_sse:
            await self.send(message)
            return
        # ── Standard Starlette streaming gzip (same behaviour) ──
        if message["type"] == "http.response.body" and self.content_encoding_set:
            if not self.started:
                self.started = True
                await self.send(self.initial_message)
            await self.send(message)
            return
        if message["type"] == "http.response.body" and not self.started:
            self.started = True
            body = message.get("body", b"")
            more_body = message.get("more_body", False)
            if len(body) < self.minimum_size and not more_body:
                await self.send(self.initial_message)
                await self.send(message)
                return
            if not more_body:
                # Standard gzip response (ndogo — content-length inajulikana).
                self.gzip_file.write(body)
                self.gzip_file.close()
                body = self.buffer.getvalue()
                from starlette.datastructures import MutableHeaders
                headers = MutableHeaders(raw=self.initial_message["headers"])
                headers["Content-Encoding"] = "gzip"
                headers["Content-Length"] = str(len(body))
                headers.add_vary_header("Accept-Encoding")
                message["body"] = body
                await self.send(self.initial_message)
                await self.send(message)
                return
            # Streaming gzip (first chunk)
            from starlette.datastructures import MutableHeaders
            headers = MutableHeaders(raw=self.initial_message["headers"])
            headers["Content-Encoding"] = "gzip"
            headers.add_vary_header("Accept-Encoding")
            del headers["Content-Length"]
            self.gzip_file.write(body)
            message["body"] = self.buffer.getvalue()
            self.buffer.seek(0)
            self.buffer.truncate()
            await self.send(self.initial_message)
            await self.send(message)
            return
        if message["type"] == "http.response.body":
            # Remaining streaming chunks
            body = message.get("body", b"")
            more_body = message.get("more_body", False)
            self.gzip_file.write(body)
            if not more_body:
                self.gzip_file.close()
            message["body"] = self.buffer.getvalue()
            self.buffer.seek(0)
            self.buffer.truncate()
            await self.send(message)


app.add_middleware(_SSEAwareGzipResponder, minimum_size=1000)
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
app.include_router(feedback_router)
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
