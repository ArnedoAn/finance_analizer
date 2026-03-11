"""
SQLAlchemy ORM Models

Database models for:
- Audit logging (tracking processed emails and results)
- Idempotency (preventing duplicate processing)
- Caching (local copies of Firefly accounts/categories)
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    Index,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class ProcessedEmail(Base):
    """
    Tracks emails that have been processed.
    
    Used for idempotency to prevent reprocessing the same email.
    The combination of message_id and internal_id is unique.
    """
    
    __tablename__ = "processed_emails"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    internal_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    email_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now()
    )
    
    __table_args__ = (
        Index("ix_processed_emails_unique", "message_id", "internal_id", unique=True),
    )
    
    def __repr__(self) -> str:
        return f"<ProcessedEmail(id={self.id}, message_id={self.message_id[:20]}...)>"


class AuditLog(Base):
    """
    Complete audit trail of email processing.
    
    Records every processing attempt with full details for debugging
    and compliance purposes.
    """
    
    __tablename__ = "audit_logs"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    
    # Email identification
    email_message_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    email_internal_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    email_subject: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    email_sender: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    email_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    
    # Processing status
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    
    # Analysis result (JSON)
    analysis_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    
    # Firefly transaction reference
    firefly_transaction_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True
    )
    
    # Error information
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    
    # Metadata
    processing_time_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now(), onupdate=func.now()
    )
    
    __table_args__ = (
        Index("ix_audit_logs_email", "email_message_id", "email_internal_id"),
        Index("ix_audit_logs_date", "email_date"),
        Index("ix_audit_logs_status_date", "status", "created_at"),
    )
    
    def __repr__(self) -> str:
        return f"<AuditLog(id={self.id}, status={self.status}, tx={self.firefly_transaction_id})>"


class AccountCache(Base):
    """
    Local cache of Firefly III accounts.
    
    Reduces API calls by caching account name-to-ID mappings.
    """
    
    __tablename__ = "account_cache"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    firefly_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    account_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now(), onupdate=func.now()
    )
    
    __table_args__ = (
        Index("ix_account_cache_name_type", "name", "account_type", unique=True),
    )
    
    def __repr__(self) -> str:
        return f"<AccountCache(id={self.id}, name={self.name}, type={self.account_type})>"


class CategoryCache(Base):
    """
    Local cache of Firefly III categories.
    
    Reduces API calls by caching category name-to-ID mappings.
    """
    
    __tablename__ = "category_cache"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    firefly_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now(), onupdate=func.now()
    )
    
    def __repr__(self) -> str:
        return f"<CategoryCache(id={self.id}, name={self.name})>"


class TagCache(Base):
    """
    Local cache of Firefly III tags.
    
    Optional feature for enhanced categorization.
    """
    
    __tablename__ = "tag_cache"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    firefly_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    tag: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now()
    )
    
    def __repr__(self) -> str:
        return f"<TagCache(id={self.id}, tag={self.tag})>"


class KnownSender(Base):
    """
    Dictionary of known financial email senders.
    
    Stores keywords that identify financial emails from specific senders.
    Can be populated manually or learned automatically from email analysis.
    """
    
    __tablename__ = "known_senders"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    keyword: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    sender_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sender_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="bank"
    )  # bank, payment_processor, store, etc.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_auto_learned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confidence_score: Mapped[float] = mapped_column(Integer, nullable=False, default=100)
    emails_matched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_matched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now(), onupdate=func.now()
    )
    
    __table_args__ = (
        Index("ix_known_senders_active", "is_active", "keyword"),
    )
    
    def __repr__(self) -> str:
        return f"<KnownSender(keyword={self.keyword}, name={self.sender_name})>"


class ProcessedNotification(Base):
    """
    Tracks notifications that have been processed.
    
    Used for idempotency to prevent reprocessing the same notification/SMS.
    The notification_hash is a SHA-256 of (sender + message + timestamp).
    """
    
    __tablename__ = "processed_notifications"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    notification_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    source_app: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    sender: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    notification_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now()
    )
    
    def __repr__(self) -> str:
        return f"<ProcessedNotification(id={self.id}, hash={self.notification_hash[:12]}..., app={self.source_app})>"


class TransactionFingerprint(Base):
    """
    Cross-channel transaction deduplication.
    
    Stores a fingerprint (hash of amount + date + account) for each created
    transaction, regardless of source channel (email or notification).
    Used to detect when the same transaction arrives from both email and SMS.
    """
    
    __tablename__ = "transaction_fingerprints"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fingerprint_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    amount: Mapped[str] = mapped_column(String(50), nullable=False)
    transaction_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source_channel: Mapped[str] = mapped_column(String(20), nullable=False)  # email, notification
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    firefly_transaction_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now()
    )
    
    __table_args__ = (
        Index("ix_fingerprint_hash_date", "fingerprint_hash", "transaction_date"),
    )
    
    def __repr__(self) -> str:
        return f"<TransactionFingerprint(id={self.id}, channel={self.source_channel}, amount={self.amount})>"


class SchedulerJobLog(Base):
    """
    Log of scheduled job executions.
    
    Tracks when CRON jobs run and their results.
    """
    
    __tablename__ = "scheduler_job_logs"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(50), nullable=False)  # processing, learning
    status: Mapped[str] = mapped_column(String(50), nullable=False)  # started, completed, failed
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    
    # Results
    emails_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transactions_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    senders_learned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    
    __table_args__ = (
        Index("ix_scheduler_job_logs_job_date", "job_name", "started_at"),
    )
    
    def __repr__(self) -> str:
        return f"<SchedulerJobLog(job={self.job_name}, status={self.status})>"
