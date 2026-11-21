"""Alerts polled by the app (Phase 3, contract sections 5 and 13)."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timezone import now_utc
from app.db.database import Base
from app.db.models import AwareDateTime


class Alert(Base):
    """One event for the app to show. ``id`` only grows (SQLite AUTOINCREMENT never reuses ids),
    so clients can poll with ``after_id``. Anti-noise state lives in ``notifications``."""

    __tablename__ = "alerts"
    __table_args__ = {"sqlite_autoincrement": True}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text, default="")
    item_id: Mapped[int | None] = mapped_column(Integer, index=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str | None] = mapped_column(String(10))
    suggested_categories: Mapped[list[str] | None] = mapped_column(JSON)
    question: Mapped[str | None] = mapped_column(Text)
    options: Mapped[list[str] | None] = mapped_column(JSON)
    dedupe_key: Mapped[str | None] = mapped_column(String(255))
    acked_at: Mapped[datetime | None] = mapped_column(AwareDateTime(), index=True)
