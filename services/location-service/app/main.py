from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .core.config import settings
from .routes import locations, cadres

app = FastAPI(
    title="Location Service",
    description="Cascading location + cadre + subject data (Regions, Districts, Facilities, Cadres, Subjects)",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(locations.router)
app.include_router(cadres.router)


@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok", "service": "location-service"}
