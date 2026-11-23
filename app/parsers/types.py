"""Deterministic extraction contract shared by ingestion and ledger processing."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

Kind = Literal["withdrawal", "deposit", "transfer", "refund", "ignore", "review"]


@dataclass(frozen=True)
class Extraction:
    kind: Kind
    amount: Decimal | None = None
    currency: Literal["COP", "USD"] = "COP"
    merchant: str | None = None
    occurred_at: datetime | None = None
    date_only: date | None = None
    account_hint: str | None = None
    counterparty_hint: str | None = None
    template_id: str = "unknown"
    reason: str | None = None
    # Short Spanish description written by the LLM only (stored instead of the e-mail body once the item is final).
    summary: str | None = None
