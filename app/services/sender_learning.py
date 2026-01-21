"""
Sender Learning Service

Analyzes emails to automatically discover and learn new financial senders.
Uses DeepSeek AI to identify patterns in email senders.
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.deepseek import DeepSeekClient
from app.clients.gmail import GmailClient
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.repositories import KnownSenderRepository, SchedulerJobLogRepository
from app.models.schemas import EmailFilter

logger = get_logger(__name__)


class SenderLearningService:
    """
    Service for learning new financial email senders.
    
    Analyzes recent emails to identify patterns and automatically
    add new senders to the known senders dictionary.
    """
    
    def __init__(
        self,
        session: AsyncSession,
        gmail_client: GmailClient,
        deepseek_client: DeepSeekClient,
    ) -> None:
        self.session = session
        self.gmail = gmail_client
        self.deepseek = deepseek_client
        self.settings = get_settings()
        
        self._sender_repo = KnownSenderRepository(session)
        self._job_log_repo = SchedulerJobLogRepository(session)
    
    async def learn_from_recent_emails(
        self,
        email_count: int | None = None,
        days_back: int = 30,
    ) -> dict[str, Any]:
        """
        Analyze recent emails to learn new financial senders.
        
        Args:
            email_count: Number of emails to analyze.
            days_back: How many days back to look.
            
        Returns:
            Dictionary with learning results.
        """
        email_count = email_count or self.settings.scheduler_learning_email_count
        
        logger.info(
            "sender_learning_starting",
            email_count=email_count,
            days_back=days_back,
        )
        
        # Create job log
        job_log = await self._job_log_repo.create(
            job_name="sender_learning",
            job_type="learning",
        )
        
        try:
            # Fetch email summaries (just sender + subject)
            email_filter = EmailFilter(
                max_results=email_count,
                after_date=datetime.utcnow() - timedelta(days=days_back),
            )
            
            # Get emails without body (lightweight)
            email_summaries = await self.gmail.fetch_email_summaries(email_filter)
            
            if not email_summaries:
                logger.info("sender_learning_no_emails")
                await self._job_log_repo.complete(
                    job_log.id,
                    emails_processed=0,
                    senders_learned=0,
                    details={"message": "No emails found"},
                )
                return {
                    "emails_analyzed": 0,
                    "senders_learned": 0,
                    "new_senders": [],
                }
            
            # Get existing keywords to avoid duplicates
            existing_keywords = await self._sender_repo.get_all_keywords()
            
            # Filter out emails from already known senders
            unknown_emails = []
            for email in email_summaries:
                sender_lower = email.get("sender", "").lower()
                is_known = any(kw in sender_lower for kw in existing_keywords)
                if not is_known:
                    unknown_emails.append(email)
            
            logger.info(
                "sender_learning_analyzing",
                total_emails=len(email_summaries),
                unknown_emails=len(unknown_emails),
            )
            
            if not unknown_emails:
                await self._job_log_repo.complete(
                    job_log.id,
                    emails_processed=len(email_summaries),
                    senders_learned=0,
                    details={"message": "All senders already known"},
                )
                return {
                    "emails_analyzed": len(email_summaries),
                    "senders_learned": 0,
                    "new_senders": [],
                }
            
            # Analyze unknown senders with AI
            learned_senders = await self.deepseek.analyze_senders_for_learning(
                unknown_emails
            )
            
            # Add learned senders to database
            added_count = 0
            new_senders = []
            
            for sender in learned_senders:
                keyword = sender.get("keyword", "").lower()
                if keyword and keyword not in existing_keywords:
                    try:
                        await self._sender_repo.add_sender(
                            keyword=keyword,
                            sender_name=sender.get("sender_name", keyword.title()),
                            sender_type=sender.get("sender_type", "unknown"),
                            is_auto_learned=True,
                            confidence_score=sender.get("confidence_score", 0.8) * 100,
                        )
                        added_count += 1
                        new_senders.append({
                            "keyword": keyword,
                            "name": sender.get("sender_name"),
                            "type": sender.get("sender_type"),
                        })
                        existing_keywords.add(keyword)  # Prevent duplicates in batch
                    except Exception as e:
                        logger.warning(
                            "sender_learning_add_failed",
                            keyword=keyword,
                            error=str(e),
                        )
            
            await self.session.commit()
            
            # Complete job log
            await self._job_log_repo.complete(
                job_log.id,
                emails_processed=len(email_summaries),
                senders_learned=added_count,
                details={"new_senders": new_senders},
            )
            
            logger.info(
                "sender_learning_completed",
                emails_analyzed=len(email_summaries),
                senders_learned=added_count,
            )
            
            return {
                "emails_analyzed": len(email_summaries),
                "senders_learned": added_count,
                "new_senders": new_senders,
            }
            
        except Exception as e:
            logger.error("sender_learning_failed", error=str(e))
            await self._job_log_repo.fail(job_log.id, str(e))
            await self.session.commit()
            raise
    
    async def add_sender_manually(
        self,
        keyword: str,
        sender_name: str,
        sender_type: str = "bank",
    ) -> dict[str, Any]:
        """
        Manually add a known sender.
        
        Args:
            keyword: Keyword to match in sender email.
            sender_name: Human-readable name.
            sender_type: Type of sender (bank, payment, store, etc.).
            
        Returns:
            Created sender data.
        """
        if await self._sender_repo.exists(keyword):
            logger.warning("sender_already_exists", keyword=keyword)
            return {"error": "Sender already exists", "keyword": keyword}
        
        sender = await self._sender_repo.add_sender(
            keyword=keyword,
            sender_name=sender_name,
            sender_type=sender_type,
            is_auto_learned=False,
            confidence_score=100.0,
        )
        await self.session.commit()
        
        logger.info("sender_added_manually", keyword=keyword, name=sender_name)
        
        return {
            "id": sender.id,
            "keyword": sender.keyword,
            "sender_name": sender.sender_name,
            "sender_type": sender.sender_type,
        }
    
    async def bulk_add_senders(
        self,
        senders: list[dict[str, str]],
    ) -> dict[str, Any]:
        """
        Bulk add known senders.
        
        Args:
            senders: List of dicts with keyword, sender_name, sender_type.
            
        Returns:
            Results of bulk operation.
        """
        added = 0
        skipped = 0
        errors = []
        
        for sender_data in senders:
            keyword = sender_data.get("keyword", "").lower()
            if not keyword:
                errors.append({"error": "Missing keyword", "data": sender_data})
                continue
            
            if await self._sender_repo.exists(keyword):
                skipped += 1
                continue
            
            try:
                await self._sender_repo.add_sender(
                    keyword=keyword,
                    sender_name=sender_data.get("sender_name", keyword.title()),
                    sender_type=sender_data.get("sender_type", "unknown"),
                    is_auto_learned=False,
                    confidence_score=100.0,
                )
                added += 1
            except Exception as e:
                errors.append({"keyword": keyword, "error": str(e)})
        
        await self.session.commit()
        
        logger.info(
            "senders_bulk_added",
            added=added,
            skipped=skipped,
            errors=len(errors),
        )
        
        return {
            "added": added,
            "skipped": skipped,
            "errors": errors,
        }
    
    async def get_all_senders(
        self,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        """Get all known senders."""
        if include_inactive:
            from sqlalchemy import select
            from app.db.models import KnownSender
            
            query = select(KnownSender)
            result = await self.session.execute(query)
            senders = result.scalars().all()
        else:
            senders = await self._sender_repo.get_all_active()
        
        return [
            {
                "id": s.id,
                "keyword": s.keyword,
                "sender_name": s.sender_name,
                "sender_type": s.sender_type,
                "is_active": s.is_active,
                "is_auto_learned": s.is_auto_learned,
                "confidence_score": s.confidence_score,
                "emails_matched": s.emails_matched,
                "last_matched_at": s.last_matched_at,
                "created_at": s.created_at,
            }
            for s in senders
        ]
    
    async def deactivate_sender(self, keyword: str) -> bool:
        """Deactivate a sender by keyword."""
        await self._sender_repo.deactivate_sender(keyword)
        await self.session.commit()
        logger.info("sender_deactivated", keyword=keyword)
        return True
