"""Audit log, assistant proposals and batches (Phase 4, contract section 9)."""
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timezone import now_utc
from app.db.database import Base
from app.db.models import AwareDateTime


class Audit(Base):
    """Append-only record of who changed what. ``detail`` never holds raw text or secrets."""

    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, index=True)
    # No foreign key: the record must outlive a revoked or deleted device.
    device_id: Mapped[int | None] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(100), default="system")
    action: Mapped[str] = mapped_column(String(60), index=True)
    target_type: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[str] = mapped_column(String(100))
    detail: Mapped[dict | None] = mapped_column(JSON)


class ProposalBatch(Base):
    __tablename__ = "proposal_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    window_from: Mapped[datetime] = mapped_column(AwareDateTime())
    window_to: Mapped[datetime] = mapped_column(AwareDateTime())
    instruction: Mapped[str] = mapped_column(Text, default="")
    # open | applied | partially_applied | discarded | reverted
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    applied_at: Mapped[datetime | None] = mapped_column(AwareDateTime())


class Proposal(Base):
    __tablename__ = "proposals"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("proposal_batches.id"), index=True)
    # Inbox item the proposal changes; null for the actions on something else (see ``target_ref``).
    target_id: Mapped[int | None] = mapped_column(ForeignKey("inbox_items.id"), index=True, nullable=True)
    # What a non-inbox action points at: a Firefly transaction id, a category, budget or rule. ``subject`` is its label.
    target_ref: Mapped[str | None] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(String(255))
    field: Mapped[str] = mapped_column(String(30))
    old_value: Mapped[Any] = mapped_column(JSON, nullable=True)
    new_value: Mapped[Any] = mapped_column(JSON, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    risky: Mapped[bool] = mapped_column(Boolean, default=False)
    # proposed | applied | discarded | failed | reverted
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    applied_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    reverted_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    # What applying left behind (the ledger change id that "undo batch" reverts).
    result: Mapped[Any] = mapped_column(JSON, nullable=True)
