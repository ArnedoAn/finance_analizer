"""Manual ledger edit history: what every create/update/delete changed, kept for undo (contract section 15)."""
from datetime import datetime

from sqlalchemy import JSON, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timezone import now_utc
from app.db.database import Base
from app.db.models import AwareDateTime


class LedgerChange(Base):
    """One manual write to the Firefly ledger.

    ``before_version`` / ``after_version`` hold the complete normalized transaction (see ``services/ledger.py``)
    so the change can be undone without asking Firefly what used to be there. ``client_ref`` is only set on
    creations and makes them idempotent. ``inbox_before`` is the synchronized inbox item as it was before the
    change (None when the movement has no inbox item).
    """

    __tablename__ = "ledger_changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[str] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(10))
    firefly_id: Mapped[str] = mapped_column(String(100), index=True)
    client_ref: Mapped[str | None] = mapped_column(String(80), unique=True)
    at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, index=True)
    # Like ``audit``: no foreign key, the record must outlive a revoked device.
    actor: Mapped[str] = mapped_column(String(100), default="system")
    device_id: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text)
    before_version: Mapped[dict | None] = mapped_column(JSON)
    after_version: Mapped[dict | None] = mapped_column(JSON)
    inbox_id: Mapped[int | None] = mapped_column(Integer)
    inbox_before: Mapped[dict | None] = mapped_column(JSON)
    undone_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
