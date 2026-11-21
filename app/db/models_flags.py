"""Runtime flags such as the auto-approval pause after 'No lo reconozco' (Phase 5 A7, contract section 13)."""
from datetime import datetime

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timezone import now_utc
from app.db.database import Base
from app.db.models import AwareDateTime


class RuntimeFlag(Base):
    __tablename__ = "runtime_flags"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)
