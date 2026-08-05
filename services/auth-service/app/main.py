import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .routes import auth

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(
    title="Auth Service",
    description="Registration + Login. Publishes user.registered event to MQTT.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)


@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok", "service": "auth-service"}
