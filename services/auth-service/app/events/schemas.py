"""Local copy of shared event schemas (kept in-service to avoid tight coupling)."""
from datetime import datetime, timezone
from typing import Literal, Optional
from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Station(BaseModel):
    region_id: int
    region_name: str
    district_id: Optional[int] = None
    district_name: Optional[str] = None
    facility_id: Optional[str] = None
    facility_name: Optional[str] = None
    facility_type: Optional[str] = None


class Destination(BaseModel):
    region_id: int
    region_name: str
    district_id: Optional[int] = None
    district_name: Optional[str] = None
    facility_id: Optional[str] = None
    facility_name: Optional[str] = None
    notes: Optional[str] = None


class UserRegisteredEvent(BaseModel):
    event: Literal["user.registered"] = "user.registered"
    user_id: str
    category: Literal["health", "education"]
    cadre_code: str
    subjects: list[str] = Field(default_factory=list)
    current_station: Station
    desired_destinations: list[Destination]
    occurred_at: datetime = Field(default_factory=utcnow)


TOPIC_USER_REGISTERED = "kv/user/registered"
