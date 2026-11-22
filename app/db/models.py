"""Single-owner durable inbox and IMAP checkpoints."""
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.core.timezone import now_utc
from app.db.database import Base
from app.security.crypto import EncryptedText


class AwareDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Datetime must be timezone aware")
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class InboxItem(Base):
    __tablename__ = "inbox_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20), index=True)
    source_ref: Mapped[str] = mapped_column(String(512), unique=True)
    account_name: Mapped[str | None] = mapped_column(String(255))
    message_id: Mapped[str | None] = mapped_column(String(512), index=True)
    app: Mapped[str | None] = mapped_column(String(255))
    sender: Mapped[str] = mapped_column(String(512), default="")
    subject_or_title: Mapped[str] = mapped_column(Text, default="")
    raw_text: Mapped[str | None] = mapped_column(EncryptedText)
    occurred_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    method: Mapped[str | None] = mapped_column(String(20))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str | None] = mapped_column(String(10))
    merchant: Mapped[str | None] = mapped_column(String(512))
    category: Mapped[str | None] = mapped_column(String(255))
    asset_account: Mapped[str | None] = mapped_column(String(255))
    transaction_type: Mapped[str | None] = mapped_column(String(20))
    destination_account: Mapped[str | None] = mapped_column(String(255))
    original_kind: Mapped[str | None] = mapped_column(String(20))
    template_id: Mapped[str | None] = mapped_column(String(255))
    review_reason: Mapped[str | None] = mapped_column(Text)
    claim_token: Mapped[str | None] = mapped_column(String(36))
    claim_until: Mapped[datetime | None] = mapped_column(AwareDateTime())
    firefly_transaction_id: Mapped[str | None] = mapped_column(String(100))
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("inbox_items.id"))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(AwareDateTime(), index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)
    # v4 (Phase 4 migration adds these columns): owner/assistant description and context tags.
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str] | None] = mapped_column(JSON)


class MailAccountState(Base):
    __tablename__ = "mail_account_state"
    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    uidvalidity: Mapped[str | None] = mapped_column(String(100))
    last_uid: Mapped[int] = mapped_column(Integer, default=0)
    last_poll_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)


class KnownSender(Base):
    __tablename__ = "known_senders"
    id: Mapped[int] = mapped_column(primary_key=True)
    keyword: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    source: Mapped[str] = mapped_column(String(30), default="manual")
    status: Mapped[str] = mapped_column(String(30), default="active")
    require_auth: Mapped[bool] = mapped_column(Boolean, default=True)


class InboxEvent(Base):
    __tablename__ = "inbox_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    inbox_id: Mapped[int] = mapped_column(ForeignKey("inbox_items.id"), index=True)
    action: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)


class CycleRun(Base):
    __tablename__ = "cycle_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    finished_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    result: Mapped[str | None] = mapped_column(Text)


class Notification(Base):
    __tablename__ = "notifications"
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    last_sent_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)


# v4 domain tables live in one module per phase so parallel work never shares a file.
# Imported here so Base.metadata (tests, Alembic autogenerate) always sees them.
from app.db import (  # noqa: E402,F401
    models_alerts,
    models_assistant,
    models_devices,
    models_flags,
    models_ledger,
    models_rules,
)
