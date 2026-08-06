from typing import Optional
from pydantic import BaseModel, Field


class SendMessageRequest(BaseModel):
    to_user_id: str
    text: str = Field(..., min_length=1, max_length=2000)


class CallLogRequest(BaseModel):
    to_user_id: str
    outcome: str = Field("initiated", pattern="^(initiated|answered|missed)$")
