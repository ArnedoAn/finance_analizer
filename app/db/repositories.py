"""
Database Repositories

Data access layer with async CRUD operations for all database models.
Follows repository pattern for clean separation of concerns.
"""

from datetime import datetime, timedelta
import uuid
from typing import Sequence

from sqlalchemy import and_, delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.session import DEFAULT_SESSION_ID, normalize_session_id
from app.db.models import (
    AccountCache,
    AuditLog,
    CategoryCache,
    KnownSender,
    ProcessingJob,
    ProcessedEmail,
    ProcessedNotification,
    SchedulerJobLog,
    TagCache,
    TransactionFingerprint,
)
from app.models.schemas import AuditLogCreate, ProcessingStatus

logger = get_logger(__name__)


def _validate_session_id(session_id: str) -> str:
    normalized = normalize_session_id(session_id)
    if normalized is None:
        raise ValueError(f"Invalid session id: {session_id}")
    return normalized


class ProcessedEmailRepository:
    """Repository for managing processed email records."""
    
    def __init__(self, session: AsyncSession, session_id: str = DEFAULT_SESSION_ID) -> None:
        self.session = session
        self.session_id = _validate_session_id(session_id)
    
    async def exists(self, message_id: str, internal_id: str) -> bool:
        """Check if an email has been processed."""
        query = select(ProcessedEmail).where(
            and_(
                ProcessedEmail.session_id == self.session_id,
                ProcessedEmail.message_id == message_id,
                ProcessedEmail.internal_id == internal_id,
            )
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none() is not None
    
    async def mark_processed(
        self,
        message_id: str,
        internal_id: str,
        email_date: datetime,
    ) -> ProcessedEmail:
        """Mark an email as processed."""
        record = ProcessedEmail(
            session_id=self.session_id,
            message_id=message_id,
            internal_id=internal_id,
            email_date=email_date,
        )
        self.session.add(record)
        await self.session.flush()
        logger.debug("email_marked_processed", message_id=message_id[:20])
        return record
    
    async def get_processed_ids(
        self,
        since: datetime | None = None,
    ) -> set[str]:
        """Get set of processed email IDs."""
        query = select(ProcessedEmail.internal_id).where(
            ProcessedEmail.session_id == self.session_id
        )
        if since:
            query = query.where(ProcessedEmail.email_date >= since)
        
        result = await self.session.execute(query)
        return {row[0] for row in result.fetchall()}
    
    async def clear_all(self) -> int:
        """
        Clear all processed email records.
        
        WARNING: This is for testing only. Use with caution.
        
        Returns:
            Number of records deleted.
        """
        query = delete(ProcessedEmail).where(ProcessedEmail.session_id == self.session_id)
        result = await self.session.execute(query)
        await self.session.flush()
        
        count = result.rowcount
        logger.warning(
            "processed_emails_cleared",
            count=count,
            reason="test_mode_clear_processed enabled",
        )
        return count


class AuditLogRepository:
    """Repository for managing audit log entries."""
    
    def __init__(self, session: AsyncSession, session_id: str = DEFAULT_SESSION_ID) -> None:
        self.session = session
        self.session_id = _validate_session_id(session_id)
    
    async def create(self, data: AuditLogCreate) -> AuditLog:
        """Create a new audit log entry."""
        audit_log = AuditLog(
            session_id=data.session_id or self.session_id,
            email_message_id=data.email_message_id,
            email_internal_id=data.email_internal_id,
            email_subject=data.email_subject,
            email_sender=data.email_sender,
            email_date=data.email_date,
            status=data.status.value,
            analysis_result=data.analysis_result,
            firefly_transaction_id=data.firefly_transaction_id,
            error_message=data.error_message,
            error_details=data.error_details,
            processing_time_ms=data.processing_time_ms,
            dry_run=data.dry_run,
        )
        self.session.add(audit_log)
        await self.session.flush()
        logger.debug("audit_log_created", id=audit_log.id, status=data.status.value)
        return audit_log
    
    async def update_status(
        self,
        audit_id: int,
        status: ProcessingStatus,
        transaction_id: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Update audit log status."""
        values = {"status": status.value, "updated_at": datetime.utcnow()}
        if transaction_id:
            values["firefly_transaction_id"] = transaction_id
        if error_message:
            values["error_message"] = error_message
        
        query = update(AuditLog).where(
            and_(AuditLog.id == audit_id, AuditLog.session_id == self.session_id)
        ).values(**values)
        await self.session.execute(query)
        await self.session.flush()
    
    async def get_by_email(
        self,
        message_id: str,
        internal_id: str,
    ) -> AuditLog | None:
        """Get audit log by email identifiers."""
        query = select(AuditLog).where(
            and_(
                AuditLog.session_id == self.session_id,
                AuditLog.email_message_id == message_id,
                AuditLog.email_internal_id == internal_id,
            )
        ).order_by(AuditLog.created_at.desc())
        
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def get_recent(
        self,
        limit: int = 100,
        status: ProcessingStatus | None = None,
    ) -> Sequence[AuditLog]:
        """Get recent audit logs."""
        query = (
            select(AuditLog)
            .where(AuditLog.session_id == self.session_id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        if status:
            query = query.where(AuditLog.status == status.value)
        
        result = await self.session.execute(query)
        return result.scalars().all()
    
    async def get_statistics(
        self,
        since: datetime | None = None,
    ) -> dict[str, int]:
        """Get processing statistics."""
        from sqlalchemy import func
        
        query = select(
            AuditLog.status,
            func.count(AuditLog.id).label("count"),
        ).where(
            AuditLog.session_id == self.session_id
        ).group_by(AuditLog.status)
        
        if since:
            query = query.where(AuditLog.created_at >= since)
        
        result = await self.session.execute(query)
        return {row[0]: row[1] for row in result.fetchall()}


class AccountCacheRepository:
    """Repository for managing account cache entries."""
    
    def __init__(self, session: AsyncSession, session_id: str = DEFAULT_SESSION_ID) -> None:
        self.session = session
        self.session_id = _validate_session_id(session_id)
    
    async def get_by_name(
        self,
        name: str,
        account_type: str,
    ) -> AccountCache | None:
        """Get account by name and type."""
        query = select(AccountCache).where(
            and_(
                AccountCache.session_id == self.session_id,
                AccountCache.name == name,
                AccountCache.account_type == account_type,
            )
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def get_by_partial_name(
        self,
        partial_name: str,
        account_type: str,
    ) -> AccountCache | None:
        """
        Get account by partial name match (case-insensitive).
        
        Useful when AI extracts account names like "Lulo" that should
        match "Lulo Bank" in the cache.
        
        Args:
            partial_name: Partial name to search for.
            account_type: Account type to filter by.
            
        Returns:
            First matching account or None.
        """
        # Normalize the search term
        search_lower = partial_name.lower().strip()
        
        # Get all accounts of this type
        query = select(AccountCache).where(
            AccountCache.session_id == self.session_id,
            AccountCache.account_type == account_type
        )
        result = await self.session.execute(query)
        accounts = result.scalars().all()
        
        # Try different matching strategies
        for account in accounts:
            account_lower = account.name.lower()
            
            # Exact match (already tried, but just in case)
            if account_lower == search_lower:
                return account
            
            # Search term is contained in account name
            # e.g., "Lulo" matches "Lulo Bank"
            if search_lower in account_lower:
                return account
            
            # Account name is contained in search term
            # e.g., "Tarjeta débito 7556 Lulo bank" should match "Lulo"
            if account_lower in search_lower:
                return account
        
        return None
    
    async def get_by_firefly_id(self, firefly_id: str) -> AccountCache | None:
        """Get account by Firefly ID."""
        query = select(AccountCache).where(
            and_(
                AccountCache.session_id == self.session_id,
                AccountCache.firefly_id == firefly_id,
            )
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def upsert(
        self,
        firefly_id: str,
        name: str,
        account_type: str,
        currency_code: str = "USD",
        active: bool = True,
    ) -> AccountCache:
        """Insert or update account cache entry."""
        # Try to get existing
        existing = await self.get_by_firefly_id(firefly_id)
        
        if existing:
            existing.name = name
            existing.account_type = account_type
            existing.currency_code = currency_code
            existing.active = active
            await self.session.flush()
            return existing
        
        # Create new
        account = AccountCache(
            session_id=self.session_id,
            firefly_id=firefly_id,
            name=name,
            account_type=account_type,
            currency_code=currency_code,
            active=active,
        )
        self.session.add(account)
        await self.session.flush()
        return account
    
    async def get_all(self, account_type: str | None = None) -> Sequence[AccountCache]:
        """Get all cached accounts."""
        query = select(AccountCache).where(AccountCache.session_id == self.session_id)
        if account_type:
            query = query.where(AccountCache.account_type == account_type)
        
        result = await self.session.execute(query)
        return result.scalars().all()
    
    async def sync_from_firefly(
        self,
        accounts: list[dict],
    ) -> int:
        """Sync account cache from Firefly III accounts list."""
        count = 0
        for acc in accounts:
            await self.upsert(
                firefly_id=acc["id"],
                name=acc["name"],
                account_type=acc["type"],
                currency_code=acc.get("currency_code", "USD"),
                active=acc.get("active", True),
            )
            count += 1
        return count


class CategoryCacheRepository:
    """Repository for managing category cache entries."""
    
    def __init__(self, session: AsyncSession, session_id: str = DEFAULT_SESSION_ID) -> None:
        self.session = session
        self.session_id = _validate_session_id(session_id)
    
    async def get_by_name(self, name: str) -> CategoryCache | None:
        """Get category by name."""
        query = select(CategoryCache).where(
            and_(
                CategoryCache.session_id == self.session_id,
                CategoryCache.name == name,
            )
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def get_by_firefly_id(self, firefly_id: str) -> CategoryCache | None:
        """Get category by Firefly ID."""
        query = select(CategoryCache).where(
            and_(
                CategoryCache.session_id == self.session_id,
                CategoryCache.firefly_id == firefly_id,
            )
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def upsert(
        self,
        firefly_id: str,
        name: str,
    ) -> CategoryCache:
        """Insert or update category cache entry."""
        existing = await self.get_by_firefly_id(firefly_id)
        
        if existing:
            existing.name = name
            await self.session.flush()
            return existing
        
        category = CategoryCache(
            session_id=self.session_id,
            firefly_id=firefly_id,
            name=name,
        )
        self.session.add(category)
        await self.session.flush()
        return category
    
    async def get_all(self) -> Sequence[CategoryCache]:
        """Get all cached categories."""
        query = select(CategoryCache).where(CategoryCache.session_id == self.session_id)
        result = await self.session.execute(query)
        return result.scalars().all()
    
    async def sync_from_firefly(
        self,
        categories: list[dict],
    ) -> int:
        """Sync category cache from Firefly III categories list."""
        count = 0
        for cat in categories:
            await self.upsert(
                firefly_id=cat["id"],
                name=cat["name"],
            )
            count += 1
        return count


class TagCacheRepository:
    """Repository for managing tag cache entries."""
    
    def __init__(self, session: AsyncSession, session_id: str = DEFAULT_SESSION_ID) -> None:
        self.session = session
        self.session_id = _validate_session_id(session_id)
    
    async def get_by_tag(self, tag: str) -> TagCache | None:
        """Get tag by name."""
        query = select(TagCache).where(
            and_(
                TagCache.session_id == self.session_id,
                TagCache.tag == tag,
            )
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def upsert(self, firefly_id: str, tag: str) -> TagCache:
        """Insert or update tag cache entry."""
        existing = await self.get_by_tag(tag)
        
        if existing:
            existing.firefly_id = firefly_id
            await self.session.flush()
            return existing
        
        tag_record = TagCache(
            session_id=self.session_id,
            firefly_id=firefly_id,
            tag=tag,
        )
        self.session.add(tag_record)
        await self.session.flush()
        return tag_record
    
    async def get_all(self) -> Sequence[TagCache]:
        """Get all cached tags."""
        query = select(TagCache).where(TagCache.session_id == self.session_id)
        result = await self.session.execute(query)
        return result.scalars().all()


class KnownSenderRepository:
    """Repository for managing known financial email senders."""
    
    def __init__(self, session: AsyncSession, session_id: str = DEFAULT_SESSION_ID) -> None:
        self.session = session
        self.session_id = _validate_session_id(session_id)
    
    async def get_by_keyword(self, keyword: str) -> KnownSender | None:
        """Get sender by keyword."""
        query = select(KnownSender).where(KnownSender.keyword == keyword.lower())
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def find_by_email(self, email: str) -> KnownSender | None:
        """
        Find a known sender by matching keywords in the email address.
        
        Args:
            email: Email address (e.g., "notificaciones@lulobank.com")
            
        Returns:
            KnownSender if a match is found, None otherwise.
        """
        if not email:
            return None
        
        email_lower = email.lower()
        
        # Get all active senders and check if their keyword is in the email
        senders = await self.get_all_active()
        for sender in senders:
            if sender.keyword in email_lower:
                return sender
        
        return None
    
    async def get_all_active(self) -> Sequence[KnownSender]:
        """Get all active known senders."""
        query = select(KnownSender).where(KnownSender.is_active == True)
        result = await self.session.execute(query)
        return result.scalars().all()
    
    async def get_all_keywords(self) -> set[str]:
        """Get all active sender keywords as a set."""
        query = select(KnownSender.keyword).where(KnownSender.is_active == True)
        result = await self.session.execute(query)
        return {row[0].lower() for row in result.fetchall()}
    
    async def add_sender(
        self,
        keyword: str,
        sender_name: str,
        sender_type: str = "bank",
        is_auto_learned: bool = False,
        confidence_score: float = 100.0,
    ) -> KnownSender:
        """Add a new known sender."""
        sender = KnownSender(
            keyword=keyword.lower(),
            sender_name=sender_name,
            sender_type=sender_type,
            is_auto_learned=is_auto_learned,
            confidence_score=confidence_score,
        )
        self.session.add(sender)
        await self.session.flush()
        return sender
    
    async def update_match_count(self, keyword: str) -> None:
        """Increment match count for a sender."""
        query = (
            update(KnownSender)
            .where(KnownSender.keyword == keyword.lower())
            .values(
                emails_matched=KnownSender.emails_matched + 1,
                last_matched_at=datetime.utcnow(),
            )
        )
        await self.session.execute(query)
        await self.session.flush()
    
    async def deactivate_sender(self, keyword: str) -> None:
        """Deactivate a sender."""
        query = (
            update(KnownSender)
            .where(KnownSender.keyword == keyword.lower())
            .values(is_active=False)
        )
        await self.session.execute(query)
        await self.session.flush()
    
    async def exists(self, keyword: str) -> bool:
        """Check if a sender keyword exists."""
        query = select(KnownSender).where(KnownSender.keyword == keyword.lower())
        result = await self.session.execute(query)
        return result.scalar_one_or_none() is not None
    
    async def bulk_add(
        self,
        senders: list[dict],
        is_auto_learned: bool = False,
    ) -> int:
        """Bulk add senders, skipping existing ones."""
        count = 0
        for sender in senders:
            keyword = sender["keyword"].lower()
            if not await self.exists(keyword):
                await self.add_sender(
                    keyword=keyword,
                    sender_name=sender["sender_name"],
                    sender_type=sender.get("sender_type", "unknown"),
                    is_auto_learned=is_auto_learned,
                    confidence_score=sender.get("confidence_score", 80.0),
                )
                count += 1
        return count


class SchedulerJobLogRepository:
    """Repository for managing scheduler job logs."""
    
    def __init__(self, session: AsyncSession, session_id: str = DEFAULT_SESSION_ID) -> None:
        self.session = session
        self.session_id = _validate_session_id(session_id)
    
    async def create(
        self,
        job_name: str,
        job_type: str,
    ) -> SchedulerJobLog:
        """Create a new job log entry."""
        log = SchedulerJobLog(
            session_id=self.session_id,
            job_name=job_name,
            job_type=job_type,
            status="started",
        )
        self.session.add(log)
        await self.session.flush()
        return log
    
    async def complete(
        self,
        log_id: int,
        emails_processed: int = 0,
        transactions_created: int = 0,
        senders_learned: int = 0,
        details: dict | None = None,
    ) -> None:
        """Mark job as completed."""
        query = (
            update(SchedulerJobLog)
            .where(
                and_(
                    SchedulerJobLog.id == log_id,
                    SchedulerJobLog.session_id == self.session_id,
                )
            )
            .values(
                status="completed",
                completed_at=datetime.utcnow(),
                emails_processed=emails_processed,
                transactions_created=transactions_created,
                senders_learned=senders_learned,
                details=details,
            )
        )
        await self.session.execute(query)
        await self.session.flush()
    
    async def fail(
        self,
        log_id: int,
        error_message: str,
    ) -> None:
        """Mark job as failed."""
        query = (
            update(SchedulerJobLog)
            .where(
                and_(
                    SchedulerJobLog.id == log_id,
                    SchedulerJobLog.session_id == self.session_id,
                )
            )
            .values(
                status="failed",
                completed_at=datetime.utcnow(),
                error_message=error_message,
            )
        )
        await self.session.execute(query)
        await self.session.flush()
    
    async def get_last_run(self, job_name: str) -> SchedulerJobLog | None:
        """Get the last run of a specific job."""
        query = (
            select(SchedulerJobLog)
            .where(
                and_(
                    SchedulerJobLog.session_id == self.session_id,
                    SchedulerJobLog.job_name == job_name,
                )
            )
            .order_by(SchedulerJobLog.started_at.desc())
            .limit(1)
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def get_recent(
        self,
        limit: int = 50,
        job_type: str | None = None,
    ) -> Sequence[SchedulerJobLog]:
        """Get recent job logs."""
        query = (
            select(SchedulerJobLog)
            .where(SchedulerJobLog.session_id == self.session_id)
            .order_by(SchedulerJobLog.started_at.desc())
            .limit(limit)
        )
        
        if job_type:
            query = query.where(SchedulerJobLog.job_type == job_type)
        
        result = await self.session.execute(query)
        return result.scalars().all()


class ProcessedNotificationRepository:
    """Repository for managing processed notification records."""
    
    def __init__(self, session: AsyncSession, session_id: str = DEFAULT_SESSION_ID) -> None:
        self.session = session
        self.session_id = _validate_session_id(session_id)
    
    async def exists(self, notification_hash: str) -> bool:
        """Check if a notification has been processed."""
        query = select(ProcessedNotification).where(
            and_(
                ProcessedNotification.session_id == self.session_id,
                ProcessedNotification.notification_hash == notification_hash,
            )
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none() is not None
    
    async def mark_processed(
        self,
        notification_hash: str,
        source_app: str,
        sender: str,
        title: str,
        notification_date: datetime,
    ) -> ProcessedNotification:
        """Mark a notification as processed."""
        record = ProcessedNotification(
            session_id=self.session_id,
            notification_hash=notification_hash,
            source_app=source_app,
            sender=sender,
            title=title,
            notification_date=notification_date,
        )
        self.session.add(record)
        await self.session.flush()
        logger.debug("notification_marked_processed", hash=notification_hash[:12])
        return record
    
    async def get_recent(
        self,
        limit: int = 100,
        source_app: str | None = None,
    ) -> Sequence[ProcessedNotification]:
        """Get recent processed notifications."""
        query = (
            select(ProcessedNotification)
            .where(ProcessedNotification.session_id == self.session_id)
            .order_by(ProcessedNotification.processed_at.desc())
            .limit(limit)
        )
        if source_app:
            query = query.where(ProcessedNotification.source_app == source_app)
        
        result = await self.session.execute(query)
        return result.scalars().all()
    
    async def get_count(self, source_app: str | None = None) -> int:
        """Get count of processed notifications."""
        from sqlalchemy import func
        
        query = select(func.count(ProcessedNotification.id)).where(
            ProcessedNotification.session_id == self.session_id
        )
        if source_app:
            query = query.where(ProcessedNotification.source_app == source_app)
        
        result = await self.session.execute(query)
        return result.scalar_one()


class ProcessingJobRepository:
    """Repository for async processing jobs."""

    def __init__(self, session: AsyncSession, session_id: str = DEFAULT_SESSION_ID) -> None:
        self.session = session
        self.session_id = _validate_session_id(session_id)

    async def create(self, request_payload: dict | None = None) -> ProcessingJob:
        """Create a queued processing job."""
        job = ProcessingJob(
            id=uuid.uuid4().hex,
            session_id=self.session_id,
            status="queued",
            request_payload=request_payload,
        )
        self.session.add(job)
        await self.session.flush()
        return job

    async def mark_running(self, job_id: str) -> bool:
        """Mark a processing job as running."""
        query = (
            update(ProcessingJob)
            .where(
                and_(
                    ProcessingJob.id == job_id,
                    ProcessingJob.session_id == self.session_id,
                )
            )
            .values(
                status="running",
                started_at=datetime.utcnow(),
            )
        )
        result = await self.session.execute(query)
        await self.session.flush()
        return bool(result.rowcount)

    async def mark_completed(self, job_id: str, result_payload: dict | None = None) -> bool:
        """Mark a processing job as completed."""
        query = (
            update(ProcessingJob)
            .where(
                and_(
                    ProcessingJob.id == job_id,
                    ProcessingJob.session_id == self.session_id,
                )
            )
            .values(
                status="completed",
                completed_at=datetime.utcnow(),
                result_payload=result_payload,
                error_message=None,
            )
        )
        result = await self.session.execute(query)
        await self.session.flush()
        return bool(result.rowcount)

    async def mark_failed(self, job_id: str, error_message: str) -> bool:
        """Mark a processing job as failed."""
        query = (
            update(ProcessingJob)
            .where(
                and_(
                    ProcessingJob.id == job_id,
                    ProcessingJob.session_id == self.session_id,
                )
            )
            .values(
                status="failed",
                completed_at=datetime.utcnow(),
                error_message=error_message,
            )
        )
        result = await self.session.execute(query)
        await self.session.flush()
        return bool(result.rowcount)

    async def get_by_id(self, job_id: str) -> ProcessingJob | None:
        """Get a processing job by id for this session."""
        query = select(ProcessingJob).where(
            and_(
                ProcessingJob.id == job_id,
                ProcessingJob.session_id == self.session_id,
            )
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()


class TransactionFingerprintRepository:
    """
    Repository for cross-channel transaction deduplication.
    
    Stores fingerprints (hash of amount + date + account) for created
    transactions regardless of source channel. Used to detect when the
    same transaction arrives from both email and notification.
    """
    
    def __init__(self, session: AsyncSession, session_id: str = DEFAULT_SESSION_ID) -> None:
        self.session = session
        self.session_id = _validate_session_id(session_id)
    
    async def find_duplicate(
        self,
        fingerprint_hash: str,
        transaction_date: datetime,
        window_hours: int = 2,
    ) -> TransactionFingerprint | None:
        """
        Find a duplicate transaction within a time window.
        
        Args:
            fingerprint_hash: Hash of (amount + date + account).
            transaction_date: Date of the transaction to check.
            window_hours: Time window in hours for fuzzy matching.
            
        Returns:
            Existing fingerprint if duplicate found, None otherwise.
        """
        window_start = transaction_date - timedelta(hours=window_hours)
        window_end = transaction_date + timedelta(hours=window_hours)
        
        query = select(TransactionFingerprint).where(
            and_(
                TransactionFingerprint.session_id == self.session_id,
                TransactionFingerprint.fingerprint_hash == fingerprint_hash,
                TransactionFingerprint.transaction_date >= window_start,
                TransactionFingerprint.transaction_date <= window_end,
            )
        ).order_by(TransactionFingerprint.created_at.desc()).limit(1)
        
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def create(
        self,
        fingerprint_hash: str,
        amount: str,
        transaction_date: datetime,
        source_channel: str,
        source_id: str,
        description: str = "",
        firefly_transaction_id: str | None = None,
    ) -> TransactionFingerprint:
        """Create a new transaction fingerprint."""
        record = TransactionFingerprint(
            session_id=self.session_id,
            fingerprint_hash=fingerprint_hash,
            amount=amount,
            transaction_date=transaction_date,
            source_channel=source_channel,
            source_id=source_id,
            description=description,
            firefly_transaction_id=firefly_transaction_id,
        )
        self.session.add(record)
        await self.session.flush()
        logger.debug(
            "fingerprint_created",
            hash=fingerprint_hash[:12],
            channel=source_channel,
        )
        return record
    
    @staticmethod
    def compute_hash(
        amount: str,
        transaction_date: datetime,
        account_name: str,
    ) -> str:
        """
        Compute a fingerprint hash from transaction data.
        
        Normalizes the amount (rounds to integer for COP) and uses
        only the date part (not time) for matching.
        """
        import hashlib
        from decimal import Decimal, ROUND_HALF_UP
        
        # Normalize amount: round to integer for large currencies (COP)
        try:
            amt = Decimal(str(amount))
            normalized_amount = str(amt.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        except Exception:
            normalized_amount = str(amount)
        
        # Use only date part for matching
        date_str = transaction_date.strftime("%Y-%m-%d")
        
        # Normalize account name
        normalized_account = account_name.strip().lower()
        
        raw = f"{normalized_amount}:{date_str}:{normalized_account}"
        return hashlib.sha256(raw.encode()).hexdigest()
