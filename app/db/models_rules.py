"""Merchants and categorization rules (Phase 5 A6, contract section 7)."""
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timezone import now_utc
from app.db.database import Base
from app.db.models import AwareDateTime


class Merchant(Base):
    """One row per merchant name normalized with ``app.parsers.normalize_merchant``."""

    __tablename__ = "merchants"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    default_category: Mapped[str | None] = mapped_column(String(255))
    count: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(AwareDateTime())


class MerchantItem(Base):
    """Marks inbox items already counted in ``Merchant.count`` so reprocessing never counts twice."""

    __tablename__ = "merchant_items"
    item_id: Mapped[int] = mapped_column(ForeignKey("inbox_items.id"), primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), index=True)


class Rule(Base):
    """``match`` is merchant_exact | merchant_contains | text_contains; ``source`` is manual | learned.

    ``active`` is False for a learned rule the owner contradicted (it stays for the audit and for re-learning)."""

    __tablename__ = "rules"
    __table_args__ = (UniqueConstraint("match", "pattern", name="uq_rules_match_pattern"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    match: Mapped[str] = mapped_column(String(20))
    pattern: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(255))
    merchant: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(10), default="manual")
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
