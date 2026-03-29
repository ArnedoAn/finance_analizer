"""
Notification Processor Service

Orchestration service for processing phone notifications/SMS:
Phone App → DeepSeek AI → Firefly III

Handles idempotency, cross-channel dedup, audit logging, and error recovery.
"""

import time
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.deepseek import DeepSeekClient
from app.clients.firefly import FireflyClient
from app.core.config import get_settings
from app.core.exceptions import (
    DeepSeekError,
    DuplicateNotificationError,
    FireflyDuplicateError,
    FireflyError,
    NotificationFilteredError,
)
from app.core.logging import get_logger
from app.core.session import DEFAULT_SESSION_ID, normalize_session_id
from app.db.repositories import (
    AuditLogRepository,
    ProcessedNotificationRepository,
    TransactionFingerprintRepository,
)
from app.models.schemas import (
    AuditLogCreate,
    NotificationPayload,
    NotificationProcessingResult,
    ProcessingStatus,
    TransactionAnalysis,
)
from app.services.sync_service import SyncService
from app.services.transaction_service import TransactionService

logger = get_logger(__name__)


class NotificationProcessorService:
    """
    Service for processing financial notifications from phone apps.
    
    Coordinates the full workflow from notification reception through
    AI analysis to transaction creation in Firefly III.
    """
    
    def __init__(
        self,
        session: AsyncSession,
        deepseek_client: DeepSeekClient,
        firefly_client: FireflyClient,
        session_id: str = DEFAULT_SESSION_ID,
    ) -> None:
        self.session = session
        self.deepseek = deepseek_client
        self.firefly = firefly_client
        self.settings = get_settings()
        normalized_session_id = normalize_session_id(session_id)
        if normalized_session_id is None:
            raise ValueError(f"Invalid session id: {session_id}")
        self.session_id = normalized_session_id
        
        # Repositories
        self._notification_repo = ProcessedNotificationRepository(
            session,
            session_id=self.session_id,
        )
        self._fingerprint_repo = TransactionFingerprintRepository(
            session,
            session_id=self.session_id,
        )
        self._audit_repo = AuditLogRepository(session, session_id=self.session_id)
        
        # Services
        self._sync_service = SyncService(
            session,
            firefly_client,
            session_id=self.session_id,
        )
        self._transaction_service = TransactionService(
            session,
            firefly_client,
            self._sync_service,
            session_id=self.session_id,
        )
    
    def is_known_app(self, app_package: str) -> bool:
        """Check if the app is a known financial app."""
        known_apps = self.settings.webhook_known_apps_list
        return app_package in known_apps
    
    async def process_notification(
        self,
        notification: NotificationPayload,
        dry_run: bool = False,
    ) -> NotificationProcessingResult:
        """
        Process a single notification through the full pipeline.
        
        Flow:
        1. Check idempotency (skip if already processed)
        2. Filter by known apps
        3. Analyze with DeepSeek AI (AI also validates if financial)
        4. Check cross-channel fingerprint
        5. Create transaction in Firefly III
        6. Save fingerprint + audit log
        """
        start_time = time.time()
        dry_run = dry_run or self.settings.dry_run
        notification_hash = notification.notification_hash
        
        logger.info(
            "processing_notification",
            hash=notification_hash[:12],
            app=notification.app,
            title=notification.title[:50] if notification.title else "N/A",
        )
        
        # Step 1: Idempotency check
        already_processed = await self._notification_repo.exists(notification_hash)
        if already_processed:
            logger.info("notification_already_processed", hash=notification_hash[:12])
            return NotificationProcessingResult(
                notification_hash=notification_hash,
                source_app=notification.app,
                status=ProcessingStatus.SKIPPED,
                error_message="Notification already processed",
                processing_time_ms=int((time.time() - start_time) * 1000),
            )
        
        # Step 2: Known app filter
        if not self.is_known_app(notification.app):
            logger.info(
                "notification_unknown_app",
                app=notification.app,
                hash=notification_hash[:12],
            )
            return NotificationProcessingResult(
                notification_hash=notification_hash,
                source_app=notification.app,
                status=ProcessingStatus.SKIPPED,
                error_message=f"Unknown app: {notification.app}",
                processing_time_ms=int((time.time() - start_time) * 1000),
            )
        
        # Parse notification date
        notification_date = self._parse_notification_date(notification)
        
        # Initialize audit log
        audit_data = AuditLogCreate(
            session_id=self.session_id,
            email_message_id=notification_hash,
            email_internal_id=f"notif:{notification_hash[:16]}",
            email_subject=notification.title,
            email_sender=notification.app,
            email_date=notification_date,
            status=ProcessingStatus.PROCESSING,
            dry_run=dry_run,
        )
        
        analysis: TransactionAnalysis | None = None
        transaction_id: str | None = None
        error_message: str | None = None
        error_details: dict[str, Any] = {}
        final_status = ProcessingStatus.FAILED
        
        try:
            # Sync Firefly data
            try:
                await self._sync_service.sync_all()
            except Exception as e:
                logger.warning("notification_sync_warning", error=str(e))
            
            # Step 3: Analyze with DeepSeek AI
            analysis = await self._analyze_notification(notification)
            
            if analysis is None:
                # AI determined this is not a financial notification
                final_status = ProcessingStatus.SKIPPED
                error_message = "Not a financial notification (AI filtered)"
                
                await self._notification_repo.mark_processed(
                    notification_hash=notification_hash,
                    source_app=notification.app,
                    sender=notification.sender,
                    title=notification.title,
                    notification_date=notification_date,
                )
                
                audit_data.status = final_status
                audit_data.error_message = error_message
                audit_data.processing_time_ms = int((time.time() - start_time) * 1000)
                await self._audit_repo.create(audit_data)
                await self.session.commit()
                
                return NotificationProcessingResult(
                    notification_hash=notification_hash,
                    source_app=notification.app,
                    status=final_status,
                    error_message=error_message,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )
            
            audit_data.analysis_result = analysis.model_dump(mode="json")
            audit_data.status = ProcessingStatus.ANALYZED
            
            # Step 4: Cross-channel fingerprint check
            fingerprint_hash = TransactionFingerprintRepository.compute_hash(
                amount=str(analysis.amount),
                transaction_date=analysis.date,
                account_name=analysis.suggested_account_name,
            )
            
            existing_fingerprint = await self._fingerprint_repo.find_duplicate(
                fingerprint_hash=fingerprint_hash,
                transaction_date=analysis.date,
                window_hours=self.settings.webhook_fingerprint_window_hours,
            )
            
            if existing_fingerprint:
                logger.warning(
                    "notification_cross_channel_duplicate",
                    hash=notification_hash[:12],
                    fingerprint=fingerprint_hash[:12],
                    original_channel=existing_fingerprint.source_channel,
                    original_id=existing_fingerprint.source_id[:20],
                )
                final_status = ProcessingStatus.SKIPPED
                error_message = (
                    f"Cross-channel duplicate detected "
                    f"(original: {existing_fingerprint.source_channel})"
                )
                
                await self._notification_repo.mark_processed(
                    notification_hash=notification_hash,
                    source_app=notification.app,
                    sender=notification.sender,
                    title=notification.title,
                    notification_date=notification_date,
                )
                
                audit_data.status = final_status
                audit_data.error_message = error_message
                audit_data.processing_time_ms = int((time.time() - start_time) * 1000)
                await self._audit_repo.create(audit_data)
                await self.session.commit()
                
                return NotificationProcessingResult(
                    notification_hash=notification_hash,
                    source_app=notification.app,
                    status=final_status,
                    analysis=analysis,
                    error_message=error_message,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )
            
            # Step 5: Create transaction
            result = await self._transaction_service.create_from_analysis(
                analysis,
                external_id=f"notif:{notification_hash}",
                dry_run=dry_run,
                transaction_datetime=notification_date,
            )
            
            transaction_id = result.get("id")
            audit_data.firefly_transaction_id = transaction_id
            final_status = ProcessingStatus.DRY_RUN if dry_run else ProcessingStatus.CREATED
            audit_data.status = final_status
            
            # Step 6: Save fingerprint + mark processed
            if not dry_run:
                await self._fingerprint_repo.create(
                    fingerprint_hash=fingerprint_hash,
                    amount=str(analysis.amount),
                    transaction_date=analysis.date,
                    source_channel="notification",
                    source_id=notification_hash,
                    description=analysis.description,
                    firefly_transaction_id=transaction_id,
                )
                
                await self._notification_repo.mark_processed(
                    notification_hash=notification_hash,
                    source_app=notification.app,
                    sender=notification.sender,
                    title=notification.title,
                    notification_date=notification_date,
                )
            
        except FireflyDuplicateError as e:
            logger.warning(
                "notification_duplicate_transaction",
                hash=notification_hash[:12],
            )
            final_status = ProcessingStatus.SKIPPED
            error_message = "Duplicate transaction detected by Firefly"
            error_details = e.details
            
            await self._notification_repo.mark_processed(
                notification_hash=notification_hash,
                source_app=notification.app,
                sender=notification.sender,
                title=notification.title,
                notification_date=notification_date,
            )
            
        except DeepSeekError as e:
            logger.error(
                "notification_analysis_failed",
                hash=notification_hash[:12],
                error=str(e),
            )
            error_message = f"AI analysis failed: {e.message}"
            error_details = e.details
            
        except FireflyError as e:
            logger.error(
                "notification_firefly_error",
                hash=notification_hash[:12],
                error=str(e),
            )
            error_message = f"Firefly error: {e.message}"
            error_details = e.details
            
        except Exception as e:
            logger.error(
                "notification_processing_error",
                hash=notification_hash[:12],
                error=str(e),
            )
            error_message = str(e)
        
        # Record audit log
        processing_time = int((time.time() - start_time) * 1000)
        audit_data.status = final_status
        audit_data.error_message = error_message
        audit_data.error_details = error_details or None
        audit_data.processing_time_ms = processing_time
        
        await self._audit_repo.create(audit_data)
        await self.session.commit()
        
        return NotificationProcessingResult(
            notification_hash=notification_hash,
            source_app=notification.app,
            status=final_status,
            analysis=analysis,
            transaction_id=transaction_id,
            error_message=error_message,
            error_details=error_details,
            processing_time_ms=processing_time,
        )
    
    async def _analyze_notification(
        self,
        notification: NotificationPayload,
    ) -> TransactionAnalysis | None:
        """Analyze notification content with DeepSeek AI."""
        return await self.deepseek.analyze_notification(
            notification_content=notification.content,
            notification_title=notification.title,
            source_app=notification.app,
            sender=notification.sender,
            notification_date=notification.date,
            preferred_currency=self.settings.default_currency,
        )
    
    async def get_history(
        self,
        limit: int = 100,
        source_app: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get notification processing history."""
        records = await self._notification_repo.get_recent(limit, source_app)
        return [
            {
                "id": r.id,
                "notification_hash": r.notification_hash,
                "source_app": r.source_app,
                "sender": r.sender,
                "title": r.title,
                "notification_date": r.notification_date.isoformat(),
                "processed_at": r.processed_at.isoformat(),
            }
            for r in records
        ]
    
    async def get_statistics(self) -> dict[str, Any]:
        """Get notification processing statistics."""
        total = await self._notification_repo.get_count()
        
        # Per-app counts
        by_app: dict[str, int] = {}
        for app_name in self.settings.webhook_known_apps_list:
            count = await self._notification_repo.get_count(source_app=app_name)
            if count > 0:
                by_app[app_name] = count
        
        return {
            "total_processed": total,
            "by_app": by_app,
            "known_apps": self.settings.webhook_known_apps_list,
        }
    
    def _parse_notification_date(self, notification: NotificationPayload) -> datetime:
        """Parse date from notification payload."""
        # Try timestamp first, then date field
        for date_str in (notification.timestamp, notification.date):
            if not date_str:
                continue
            for fmt in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M",
                "%Y-%m-%d",
                "%d/%m/%Y %H:%M:%S",
                "%d/%m/%Y",
            ):
                try:
                    return datetime.strptime(date_str, fmt)
                except ValueError:
                    continue
        
        # Fallback to now
        return datetime.utcnow()
