"""Validated inputs and LLM extraction output."""
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TransactionAnalysis(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    is_transaction: bool = True
    amount: Decimal | None = Field(default=None, gt=0)
    currency: str = "COP"
    merchant: str = ""
    description: str = ""
    category: str | None = None
    asset_account: str | None = None
    transaction_type: Literal["withdrawal", "deposit"] = "withdrawal"
    date: str | None = None

    @model_validator(mode="after")
    def require_amount(self):
        if self.is_transaction and self.amount is None:
            raise ValueError("A transaction requires a positive amount")
        return self


class WebhookPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
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
    amount: Decimal = Field(gt=0)
    description: str = Field(min_length=1)
    type: Literal["withdrawal", "deposit"] = "withdrawal"
    category: str | None = None
    account: str | None = None
    merchant: str | None = None
    date: str | None = None


class SenderInput(BaseModel):
    keyword: str = Field(min_length=1)
    name: str = ""
