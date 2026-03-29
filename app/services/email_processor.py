"""
Email Processor Service

Main orchestration service that coordinates the full workflow:
Gmail → DeepSeek AI → Firefly III

Handles idempotency, audit logging, and error recovery.
"""

import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.deepseek import DeepSeekClient
from app.clients.firefly import FireflyClient
from app.clients.gmail import GmailClient
from app.core.config import get_settings
from app.core.exceptions import (
    DeepSeekError,
    DuplicateEmailError,
    FireflyDuplicateError,
    FireflyError,
    GmailError,
    ProcessingError,
)
from app.core.logging import get_logger
from app.core.session import DEFAULT_SESSION_ID, normalize_session_id
from app.db.repositories import (
    AuditLogRepository,
    KnownSenderRepository,
    ProcessedEmailRepository,
    TransactionFingerprintRepository,
)
from app.models.schemas import (
    AuditLogCreate,
    BatchProcessRequest,
    BatchProcessResponse,
    EmailFilter,
    EmailMessage,
    ProcessingResult,
    ProcessingStatus,
    TransactionAnalysis,
)
from app.services.sync_service import SyncService
from app.services.transaction_service import TransactionService

logger = get_logger(__name__)


class EmailProcessorService:
    """
    Main service for processing financial emails.
    
    Coordinates the full workflow from email fetching through
    AI analysis to transaction creation in Firefly III.
    """
    
    def __init__(
        self,
        session: AsyncSession,
        gmail_client: GmailClient,
        deepseek_client: DeepSeekClient,
        firefly_client: FireflyClient,
        session_id: str = DEFAULT_SESSION_ID,
    ) -> None:
        self.session = session
        self.gmail = gmail_client
        self.deepseek = deepseek_client
        self.firefly = firefly_client
        self.settings = get_settings()
        normalized_session_id = normalize_session_id(session_id)
        if normalized_session_id is None:
            raise ValueError(f"Invalid session id: {session_id}")
        self.session_id = normalized_session_id
        
        # Repositories
        self._processed_repo = ProcessedEmailRepository(session, session_id=self.session_id)
        self._audit_repo = AuditLogRepository(session, session_id=self.session_id)
        self._sender_repo = KnownSenderRepository(session, session_id=self.session_id)
        self._fingerprint_repo = TransactionFingerprintRepository(session, session_id=self.session_id)
        
        # Services
        self._sync_service = SyncService(session, firefly_client, session_id=self.session_id)
        self._transaction_service = TransactionService(
            session,
            firefly_client,
            self._sync_service,
            session_id=self.session_id,
        )
    
    async def process_batch(
        self,
        request: BatchProcessRequest | None = None,
    ) -> BatchProcessResponse:
        """
        Process a batch of emails from Gmail.
        
        This is the main entry point for batch processing.
        
        Args:
            request: Batch processing configuration.
            
        Returns:
            BatchProcessResponse with results.
        """
        start_time = time.time()
        request = request or BatchProcessRequest()
        
        # TEST MODE: Clear processed emails table if enabled
        if self.settings.test_mode_clear_processed:
            logger.warning(
                "test_mode_clearing_processed_emails",
                warning="TEST MODE: Clearing processed emails table",
            )
            await self._processed_repo.clear_all()
            await self.session.commit()
        
        logger.info(
            "batch_processing_starting",
            max_emails=request.max_emails,
            dry_run=request.dry_run or self.settings.dry_run,
            use_known_senders=request.use_known_senders,
            test_mode_clear=self.settings.test_mode_clear_processed,
        )
        
        # Build email filter
        filter_config = EmailFilter(
            subjects=(
                request.subject_filters or self.settings.gmail_subjects_list
                if self.settings.gmail_use_subject_filters
                else []
            ),
            max_results=request.max_emails,
            after_date=request.after_date or datetime.utcnow() - timedelta(
                days=self.settings.email_lookback_days
            ),
        )
        
        # Get already processed email IDs for deduplication
        # In test mode, this will be empty since we just cleared the table
        processed_ids = await self._processed_repo.get_processed_ids(
            since=filter_config.after_date
        )
        
        # Ensure Gmail is authenticated
        try:
            await self.gmail.authenticate()
        except Exception as e:
            logger.error("batch_gmail_auth_error", error=str(e))
            return BatchProcessResponse(
                success=False,
                total_emails=0,
                processed=0,
                skipped=0,
                failed=0,
                results=[],
                errors=[f"Gmail authentication failed: {str(e)}"],
                processing_time=time.time() - start_time,
                dry_run=request.dry_run or self.settings.dry_run,
            )
        
        # Sync Firefly accounts and categories before processing
        try:
            await self._sync_service.sync_all()
        except Exception as e:
            logger.warning("batch_sync_warning", error=str(e))
            # Continue anyway, will create accounts as needed
        
        # Fetch emails from Gmail
        try:
            if request.use_known_senders:
                # Get known sender keywords
                sender_keywords = await self._sender_repo.get_all_keywords()
                
                if sender_keywords:
                    logger.info(
                        "batch_using_known_senders",
                        keyword_count=len(sender_keywords),
                    )
                    emails = await self.gmail.fetch_emails_by_senders(
                        sender_keywords=sender_keywords,
                        filter_config=filter_config,
                        exclude_ids=processed_ids,
                    )
                    
                    # Update match counts for senders found
                    for email in emails:
                        sender_lower = email.sender.lower()
                        for keyword in sender_keywords:
                            if keyword in sender_lower:
                                await self._sender_repo.update_match_count(keyword)
                                break
                else:
                    # No known senders, fall back to subject-based filtering
                    logger.warning("batch_no_known_senders_falling_back")
                    emails = await self.gmail.fetch_emails(
                        filter_config=filter_config,
                        exclude_ids=processed_ids,
                    )
            else:
                # Traditional subject-based filtering
                emails = await self.gmail.fetch_emails(
                    filter_config=filter_config,
                    exclude_ids=processed_ids,
                )
        except GmailError as e:
            logger.error("batch_gmail_error", error=str(e))
            return BatchProcessResponse(
                total_emails=0,
                processed=0,
                created=0,
                skipped=0,
                failed=1,
                dry_run=request.dry_run or self.settings.dry_run,
                results=[],
                processing_time_ms=int((time.time() - start_time) * 1000),
            )
        
        # Prefilter obvious non-financial emails before AI analysis.
        sender_keywords = await self._sender_repo.get_all_keywords()
        candidate_emails: list[EmailMessage] = []
        prefiltered_results: list[ProcessingResult] = []
        for email in emails:
            if self._is_likely_financial_email(
                email,
                sender_keywords=sender_keywords,
                subject_filters=filter_config.subjects,
            ):
                candidate_emails.append(email)
            else:
                prefiltered_results.append(
                    ProcessingResult(
                        email_id=email.internal_id,
                        status=ProcessingStatus.SKIPPED,
                        error_message="Filtered as non-financial email",
                    )
                )

        if prefiltered_results:
            logger.info(
                "batch_prefilter_applied",
                total=len(emails),
                candidates=len(candidate_emails),
                skipped=len(prefiltered_results),
            )

        # Process each candidate email
        results: list[ProcessingResult] = []
        created = 0
        skipped = len(prefiltered_results)
        failed = 0
        results.extend(prefiltered_results)
        
        for email in candidate_emails:
            try:
                result = await self.process_single_email(
                    email,
                    dry_run=request.dry_run,
                )
                results.append(result)
                
                if result.status == ProcessingStatus.CREATED:
                    created += 1
                elif result.status == ProcessingStatus.DRY_RUN:
                    created += 1  # Count dry-run as success
                elif result.status == ProcessingStatus.SKIPPED:
                    skipped += 1
                elif result.status == ProcessingStatus.FAILED:
                    failed += 1
                    
            except Exception as e:
                logger.error(
                    "batch_email_error",
                    email_id=email.internal_id,
                    error=str(e),
                )
                failed += 1
                results.append(ProcessingResult(
                    email_id=email.internal_id,
                    status=ProcessingStatus.FAILED,
                    error_message=str(e),
                ))
        
        processing_time = int((time.time() - start_time) * 1000)
        
        logger.info(
            "batch_processing_completed",
            total=len(results),
            created=created,
            skipped=skipped,
            failed=failed,
            time_ms=processing_time,
        )
        
        return BatchProcessResponse(
            total_emails=len(results),
            processed=len(results),
            created=created,
            skipped=skipped,
            failed=failed,
            dry_run=request.dry_run or self.settings.dry_run,
            results=results,
            processing_time_ms=processing_time,
        )
    
    async def process_single_email(
        self,
        email: EmailMessage,
        dry_run: bool = False,
    ) -> ProcessingResult:
        """
        Process a single email through the full pipeline.
        
        Flow:
        1. Check idempotency (skip if already processed)
        2. Analyze with DeepSeek AI
        3. Create transaction in Firefly III
        4. Record in audit log
        
        Args:
            email: Email message to process.
            dry_run: If True, analyze but don't create transaction.
            
        Returns:
            ProcessingResult with status and details.
        """
        start_time = time.time()
        dry_run = dry_run or self.settings.dry_run
        # Normalize email_date to naive datetime (PostgreSQL column is TIMESTAMP WITHOUT TIME ZONE)
        email_date = (
            email.date.replace(tzinfo=None)
            if getattr(email.date, "tzinfo", None)
            else email.date
        )
        
        logger.info(
            "processing_email",
            email_id=email.internal_id,
            subject=email.subject[:50] if email.subject else "N/A",
        )
        
        # Check idempotency (skip check in test mode)
        if not self.settings.test_mode_clear_processed:
            already_processed = await self._processed_repo.exists(
                email.message_id,
                email.internal_id,
            )
            
            if already_processed:
                logger.info("email_already_processed", email_id=email.internal_id)
                return ProcessingResult(
                    email_id=email.internal_id,
                    status=ProcessingStatus.SKIPPED,
                    error_message="Email already processed",
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )
        
        # Initialize audit log
        audit_data = AuditLogCreate(
            session_id=self.session_id,
            email_message_id=email.message_id,
            email_internal_id=email.internal_id,
            email_subject=email.subject,
            email_sender=email.sender,
            email_date=email_date,
            status=ProcessingStatus.PROCESSING,
            dry_run=dry_run,
        )
        
        analysis: TransactionAnalysis | None = None
        transaction_id: str | None = None
        error_message: str | None = None
        error_details: dict[str, Any] = {}
        final_status = ProcessingStatus.FAILED
        
        try:
            # Step 1: Analyze email with AI
            analysis = await self._analyze_email(email)
            audit_data.analysis_result = analysis.model_dump(mode="json")
            audit_data.status = ProcessingStatus.ANALYZED
            
            # Step 2: Create transaction (or simulate in dry-run)
            result = await self._transaction_service.create_from_analysis(
                analysis,
                external_id=email.idempotency_key,
                dry_run=dry_run,
                # Use the email's datetime (with time) for Firefly transaction date
                transaction_datetime=email_date,
                source_channel="email",
            )
            
            transaction_id = result.get("id")
            audit_data.firefly_transaction_id = transaction_id
            
            final_status = ProcessingStatus.DRY_RUN if dry_run else ProcessingStatus.CREATED
            audit_data.status = final_status
            
            # Mark as processed (skip in test mode to allow reprocessing)
            if not dry_run and not self.settings.test_mode_clear_processed:
                await self._processed_repo.mark_processed(
                    email.message_id,
                    email.internal_id,
                    email_date,
                )
                
                # Save fingerprint for cross-channel deduplication
                fingerprint_hash = TransactionFingerprintRepository.compute_hash(
                    amount=str(analysis.amount),
                    transaction_date=analysis.date,
                    account_name=analysis.suggested_account_name,
                )
                await self._fingerprint_repo.create(
                    fingerprint_hash=fingerprint_hash,
                    amount=str(analysis.amount),
                    transaction_date=analysis.date,
                    source_channel="email",
                    source_id=email.idempotency_key,
                    description=analysis.description,
                    firefly_transaction_id=transaction_id,
                )
            
        except FireflyDuplicateError as e:
            logger.warning(
                "email_duplicate_transaction",
                email_id=email.internal_id,
            )
            final_status = ProcessingStatus.SKIPPED
            error_message = "Duplicate transaction detected"
            error_details = e.details
            
            # Mark as processed to avoid retry (skip in test mode)
            if not self.settings.test_mode_clear_processed:
                await self._processed_repo.mark_processed(
                    email.message_id,
                    email.internal_id,
                    email_date,
                )
            
        except DeepSeekError as e:
            logger.error(
                "email_analysis_failed",
                email_id=email.internal_id,
                error=str(e),
            )
            error_message = f"AI analysis failed: {e.message}"
            error_details = e.details
            
        except FireflyError as e:
            logger.error(
                "email_firefly_error",
                email_id=email.internal_id,
                error=str(e),
            )
            error_message = f"Firefly error: {e.message}"
            error_details = e.details
            
        except Exception as e:
            logger.error(
                "email_processing_error",
                email_id=email.internal_id,
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
        
        return ProcessingResult(
            email_id=email.internal_id,
            status=final_status,
            analysis=analysis,
            transaction_id=transaction_id,
            error_message=error_message,
            error_details=error_details,
            processing_time_ms=processing_time,
        )
    
    async def _analyze_email(self, email: EmailMessage) -> TransactionAnalysis:
        """Analyze email content with DeepSeek AI."""
        return await self.deepseek.analyze_email(
            email_content=email.body,
            email_subject=email.subject,
            email_sender=email.sender,
            preferred_currency=self.settings.default_currency,
        )

    def _is_likely_financial_email(
        self,
        email: EmailMessage,
        sender_keywords: set[str],
        subject_filters: list[str],
    ) -> bool:
        """Heuristic filter to avoid sending obvious non-financial emails to AI."""
        sender_lower = (email.sender or "").lower()
        subject_lower = (email.subject or "").lower()
        body_lower = (email.body or "").lower()

        if any(keyword in sender_lower for keyword in sender_keywords):
            return True

        normalized_subject_filters = [s.lower().strip() for s in subject_filters if s.strip()]
        if any(token in subject_lower for token in normalized_subject_filters):
            return True

        financial_tokens = (
            "compra",
            "pago",
            "transfer",
            "debito",
            "crédito",
            "credito",
            "factura",
            "recibo",
            "retiro",
            "abono",
            "consumo",
            "tarjeta",
            "saldo",
            "transaccion",
        )
        searchable = f"{subject_lower} {body_lower[:800]}"
        return any(token in searchable for token in financial_tokens)
    
    async def analyze_email_preview(
        self,
        email_id: str,
    ) -> dict[str, Any]:
        """
        Analyze a specific email without creating a transaction.
        
        Useful for previewing what the AI extracts before processing.
        
        Args:
            email_id: Gmail internal ID.
            
        Returns:
            Dictionary with email and analysis data.
        """
        email = await self.gmail.get_message_by_id(email_id)
        
        if not email:
            raise ProcessingError(
                f"Email not found: {email_id}",
                details={"email_id": email_id},
            )
        
        analysis = await self._analyze_email(email)
        
        return {
            "email": {
                "id": email.internal_id,
                "subject": email.subject,
                "sender": email.sender,
                "date": email.date.isoformat(),
                "snippet": email.snippet,
            },
            "analysis": analysis.model_dump(mode="json"),
        }
    
    async def reprocess_failed(
        self,
        limit: int = 50,
    ) -> BatchProcessResponse:
        """
        Reprocess previously failed emails.
        
        Args:
            limit: Maximum number of failed emails to retry.
            
        Returns:
            BatchProcessResponse with results.
        """
        start_time = time.time()
        
        # Get failed audit logs
        failed_logs = await self._audit_repo.get_recent(
            limit=limit,
            status=ProcessingStatus.FAILED,
        )
        
        logger.info("reprocessing_failed", count=len(failed_logs))
        
        results: list[ProcessingResult] = []
        created = 0
        failed = 0
        
        for log in failed_logs:
            try:
                # Fetch email again
                email = await self.gmail.get_message_by_id(log.email_internal_id)
                
                if not email:
                    logger.warning(
                        "reprocess_email_not_found",
                        email_id=log.email_internal_id,
                    )
                    continue
                
                # Process again
                result = await self.process_single_email(email)
                results.append(result)
                
                if result.status == ProcessingStatus.CREATED:
                    created += 1
                elif result.status == ProcessingStatus.FAILED:
                    failed += 1
                    
            except Exception as e:
                logger.error(
                    "reprocess_error",
                    email_id=log.email_internal_id,
                    error=str(e),
                )
                failed += 1
        
        return BatchProcessResponse(
            total_emails=len(failed_logs),
            processed=len(results),
            created=created,
            skipped=0,
            failed=failed,
            dry_run=False,
            results=results,
            processing_time_ms=int((time.time() - start_time) * 1000),
        )
    
    async def get_audit_logs(
        self,
        limit: int = 100,
        status: ProcessingStatus | None = None,
    ) -> list[dict[str, Any]]:
        """Get recent audit logs."""
        logs = await self._audit_repo.get_recent(limit, status)
        
        return [
            {
                "id": log.id,
                "email_id": log.email_internal_id,
                "email_subject": log.email_subject,
                "email_sender": log.email_sender,
                "email_date": log.email_date.isoformat(),
                "status": log.status,
                "transaction_id": log.firefly_transaction_id,
                "error_message": log.error_message,
                "processing_time_ms": log.processing_time_ms,
                "dry_run": log.dry_run,
                "created_at": log.created_at.isoformat(),
            }
            for log in logs
        ]
    
    async def get_statistics(self) -> dict[str, Any]:
        """Get processing statistics."""
        stats = await self._audit_repo.get_statistics()
        
        return {
            "by_status": stats,
            "total": sum(stats.values()),
        }
