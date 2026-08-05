from typing import Optional
from pydantic import BaseModel, Field


class StationInput(BaseModel):
    region_id: int
    region_name: str
    district_id: int
    district_name: str
    facility_id: Optional[str] = None
    facility_name: Optional[str] = None
    facility_type: Optional[str] = None


class DestinationInput(BaseModel):
    region_id: int
    region_name: str
    district_id: Optional[int] = None
    district_name: Optional[str] = None
    facility_id: Optional[str] = None
    facility_name: Optional[str] = None
    notes: Optional[str] = Field(None, max_length=200)


class UpdateProfileRequest(BaseModel):
    full_name: Optional[str] = Field(None, min_length=3, max_length=100)
    phone_alt: Optional[str] = None
    subjects: Optional[list[str]] = None


class UpdateStationRequest(BaseModel):
    current_station: StationInput


class UpdateDestinationsRequest(BaseModel):
    desired_destinations: list[DestinationInput] = Field(..., min_length=1, max_length=15)


class NotificationPrefs(BaseModel):
    new_matches: bool = True
    messages: bool = True


class UserProfileResponse(BaseModel):
    user_id: str
    full_name: str
    phone_primary: str
    phone_alt: Optional[str] = None
    category: str
    cadre_code: str
    cadre_display: str
    subjects: list[str]
    current_station: dict
    desired_destinations: list[dict]
    notification_prefs: NotificationPrefs
    status: str
    is_verified: bool
