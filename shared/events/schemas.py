"""Shared event schemas (Pydantic). Copy this file into each service that publishes/subscribes."""
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


class UserProfileUpdatedEvent(BaseModel):
    event: Literal["user.profile_updated"] = "user.profile_updated"
    user_id: str
    changed_fields: list[str]
    occurred_at: datetime = Field(default_factory=utcnow)


class UserDestinationChangedEvent(BaseModel):
    event: Literal["user.destination_changed"] = "user.destination_changed"
    user_id: str
    desired_destinations: list[Destination]
    occurred_at: datetime = Field(default_factory=utcnow)


class UserStationChangedEvent(BaseModel):
    event: Literal["user.station_changed"] = "user.station_changed"
    user_id: str
    current_station: Station
    occurred_at: datetime = Field(default_factory=utcnow)


class MatchFoundEvent(BaseModel):
    event: Literal["match.found"] = "match.found"
    user_a_id: str
    user_b_id: str
    score: float
    occurred_at: datetime = Field(default_factory=utcnow)


TOPIC_USER_REGISTERED = "kv/user/registered"
TOPIC_USER_PROFILE_UPDATED = "kv/user/profile_updated"
TOPIC_USER_DESTINATION_CHANGED = "kv/user/destination_changed"
TOPIC_USER_STATION_CHANGED = "kv/user/station_changed"
TOPIC_USER_DELETED = "kv/user/deleted"
TOPIC_MATCH_FOUND = "kv/match/found"
TOPIC_ALL_USER = "kv/user/#"
TOPIC_ALL_MATCH = "kv/match/#"
