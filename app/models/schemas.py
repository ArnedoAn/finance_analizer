"""Validated owner and phone API inputs."""
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Column sizes (app 255, sender 512) and a body ceiling well above the 4000 chars that are stored: an oversized
# value must be a 422, not a database error that the phone outbox would retry forever.
MAX_BODY_CHARS = 20000


class WebhookPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    event_id: UUID | None = None
    notification_key: str = ""
    post_time: int | None = None
    id: str = ""
    type: Literal["notification", "sms"] = "notification"
    app: str = Field(default="", max_length=255)
    sender: str = Field(default="", max_length=512)
    title: str = Field(default="", max_length=500)
    message: str = Field(default="", max_length=MAX_BODY_CHARS)
    text: str = Field(default="", max_length=MAX_BODY_CHARS)
    big_text: str = Field(default="", max_length=MAX_BODY_CHARS)
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
    model_config = ConfigDict(extra="forbid")
    keyword: str = Field(min_length=1, max_length=255, description="Exact address (alertas@bank.com) or domain (bank.com)")
    name: str = Field(default="", max_length=255)
    require_auth: bool = True


class HistoryMailRequest(BaseModel):
    """Re-read the mailboxes between two Bogota calendar dates (both inclusive)."""
    model_config = ConfigDict(extra="forbid")
    since: date
    until: date
    dry_run: bool = True

    @model_validator(mode="after")
    def check_range(self):
        if self.since > self.until:
            raise ValueError("since must not be after until")
        if self.since < date(2020, 1, 1):
            raise ValueError("since must be 2020-01-01 or later")
        return self


class HistorySms(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sender: str = Field(min_length=1, max_length=512)
    body: str = Field(min_length=1, max_length=MAX_BODY_CHARS)
    date: int = Field(description="Provider timestamp in epoch milliseconds, as the live capture sends it")


class HistorySmsBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dry_run: bool = True
    items: list[HistorySms] = Field(min_length=1, max_length=200)
