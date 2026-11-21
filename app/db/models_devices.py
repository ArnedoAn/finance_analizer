"""Devices and enrollment codes (Phase 1, contract section 1).

Only SHA-256 hex digests of device keys and enrollment codes are stored; the
plaintext is shown once at creation and never persisted.
"""
from datetime import datetime

from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timezone import now_utc
from app.db.database import Base
from app.db.models import AwareDateTime


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scopes: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    last_used_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    expires_at: Mapped[datetime | None] = mapped_column(AwareDateTime())


class EnrollCode(Base):
    __tablename__ = "enroll_codes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scopes: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(AwareDateTime())
    used_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
