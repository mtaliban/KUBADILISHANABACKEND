from typing import Optional, Literal
from pydantic import BaseModel, Field


class InitiatePaymentRequest(BaseModel):
    amount: int = Field(..., ge=500, le=1_000_000)
    method: Literal["mixx", "selcom", "airtel", "mpesa", "halopesa", "card"] = "mixx"
    phone: Optional[str] = None
    purpose: str = Field("post_vocha", max_length=100)


class InitiatePaymentResponse(BaseModel):
    order_id: str
    status: str
    amount: int
    currency: str = "TZS"
    method: str
    checkout_url: Optional[str] = None
    message: str


class WebhookPayload(BaseModel):
    order_id: str
    reference: Optional[str] = None
    result: str  # SUCCESS / FAILED / CANCELLED
    result_code: Optional[str] = None
    transid: Optional[str] = None
    signature: Optional[str] = None
