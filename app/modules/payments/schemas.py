from typing import Optional
from pydantic import BaseModel, Field


class DonateRequest(BaseModel):
    amount: int = Field(..., ge=500, le=10_000_000)
    phone: Optional[str] = None
    sms_text: str = Field(..., min_length=10, max_length=1000)
    purpose: str = Field("donation", max_length=100)


class DonateResponse(BaseModel):
    order_id: str
    status: str
    amount: int
    currency: str = "TZS"
    message: str


class AdminReviewRequest(BaseModel):
    note: Optional[str] = Field(None, max_length=300)
