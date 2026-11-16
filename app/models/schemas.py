"""Validated owner and phone API inputs."""
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WebhookPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    event_id: UUID | None = None
    notification_key: str = ""
    post_time: int | None = None
    id: str = ""
    type: Literal["notification", "sms"] = "notification"
    app: str = ""
    sender: str = ""
    title: str = ""
    message: str = ""
    text: str = ""
    big_text: str = ""
    timestamp: str = ""
    date: str = ""
    category: str = ""
    device: str = ""

    @model_validator(mode="after")
    def require_content(self):
        if not (self.big_text.strip() or self.message.strip() or self.text.strip()):
            raise ValueError("Notification requires big_text, message or text")
        return self


class ManualTransaction(BaseModel):
    client_ref: str = Field(min_length=1, max_length=255)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    description: str = Field(min_length=1, max_length=4000)
    type: Literal["withdrawal", "deposit", "transfer", "refund"] = "withdrawal"
    category: str | None = None
    account: str | None = None
    merchant: str | None = None
    destination_account: str | None = None
    date: datetime | None = None


class SenderInput(BaseModel):
    keyword: str = Field(min_length=1)
    name: str = ""
    require_auth: bool = True
