import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .core.config import settings
from .routes import messages, ws

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(
    title="Messaging Service",
    description="Chat (REST + WebSocket) + Call logs + Contacts. Publishes kv/message/*, kv/call/* to MQTT.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(messages.router)
app.include_router(ws.router)


@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok", "service": "messaging-service"}
