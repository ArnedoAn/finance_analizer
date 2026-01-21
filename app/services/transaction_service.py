"""
Transaction Service

Handles creation of transactions in Firefly III with proper
account and category resolution.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.firefly import FireflyClient
from app.core.config import get_settings
from app.core.exceptions import FireflyDuplicateError, TransactionCreationError
from app.core.logging import get_logger
from app.models.schemas import (
    TransactionAnalysis,
    TransactionCreate,
    TransactionSplit,
)
from app.services.sync_service import SyncService

logger = get_logger(__name__)


class TransactionService:
    """
    Service for creating transactions in Firefly III.
    
    Handles the full workflow of resolving accounts, categories,
    and creating the transaction with proper error handling.
    """
    
    def __init__(
        self,
        session: AsyncSession,
        firefly_client: FireflyClient,
        sync_service: SyncService,
    ) -> None:
        self.session = session
        self.firefly = firefly_client
        self.sync = sync_service
        self.settings = get_settings()
    
    async def create_from_analysis(
        self,
        analysis: TransactionAnalysis,
        external_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """
        Create a Firefly III transaction from AI analysis.
        
        Args:
            analysis: Transaction analysis from DeepSeek.
            external_id: External reference ID for deduplication.
            dry_run: If True, don't actually create the transaction.
            
        Returns:
            Transaction data if created, or mock data if dry_run.
            
        Raises:
            TransactionCreationError: If creation fails.
        """
        logger.info(
            "transaction_creating",
            description=analysis.description[:50],
            amount=str(analysis.amount),
            type=analysis.transaction_type.value,
            dry_run=dry_run,
        )
        
        try:
            # Resolve accounts
            logger.debug(
                "resolving_accounts",
                transaction_type=analysis.transaction_type.value,
                suggested_account=analysis.suggested_account_name,
                merchant=analysis.merchant,
            )
            
            source_account = await self.sync.resolve_source_account(analysis)
            destination_account = await self.sync.resolve_destination_account(analysis)
            
            logger.info(
                "accounts_resolved",
                source=source_account["name"],
                source_type=source_account.get("type"),
                destination=destination_account["name"],
                destination_type=destination_account.get("type"),
            )
            
            # Resolve category
            category = await self.sync.resolve_category(analysis.suggested_category)
            
            logger.debug(
                "category_resolved",
                suggested=analysis.suggested_category,
                resolved=category["name"],
            )
            
            # Format date
            date_str = analysis.date.strftime("%Y-%m-%d")
            
            # Build transaction - Use only names, let Firefly resolve IDs
            transaction = TransactionCreate(
                error_if_duplicate_hash=True,
                apply_rules=True,
                fire_webhooks=True,
                transactions=[
                    TransactionSplit(
                        type=analysis.transaction_type,
                        date=date_str,
                        amount=str(analysis.amount),
                        description=analysis.description,
                        source_name=source_account["name"],
                        destination_name=destination_account["name"],
                        category_name=category["name"],
                        currency_code=analysis.currency,
                        external_id=external_id,
                        notes=self._build_notes(analysis),
                    )
                ],
            )
            
            # Dry run - return mock result
            if dry_run or self.settings.dry_run:
                logger.info(
                    "transaction_dry_run",
                    description=analysis.description[:50],
                )
                return {
                    "id": "dry-run",
                    "transaction_journal_id": "dry-run",
                    "type": analysis.transaction_type.value,
                    "date": date_str,
                    "amount": str(analysis.amount),
                    "description": analysis.description,
                    "source_name": source_account["name"],
                    "destination_name": destination_account["name"],
                    "category_name": category["name"],
                    "dry_run": True,
                }
            
            # Create transaction in Firefly
            result = await self.firefly.create_transaction(transaction)
            
            logger.info(
                "transaction_created",
                id=result["id"],
                description=analysis.description[:50],
            )
            
            return result
            
        except FireflyDuplicateError as e:
            logger.warning(
                "transaction_duplicate",
                description=analysis.description[:50],
                error=str(e),
            )
            raise
        except Exception as e:
            logger.error(
                "transaction_creation_failed",
                description=analysis.description[:50],
                error=str(e),
            )
            raise TransactionCreationError(
                f"Failed to create transaction: {analysis.description[:50]}",
                details={"analysis": analysis.model_dump(mode="json")},
                original_error=e,
            ) from e
    
    def _build_notes(self, analysis: TransactionAnalysis) -> str:
        """Build transaction notes from analysis."""
        notes_parts = [
            "Created by Finance Analyzer",
            f"Merchant: {analysis.merchant}" if analysis.merchant else None,
            f"AI Confidence: {analysis.confidence_score:.0%}",
        ]
        
        return "\n".join(filter(None, notes_parts))
    
    async def get_transaction(self, transaction_id: str) -> dict[str, Any]:
        """Get transaction by ID."""
        return await self.firefly.get_transaction(transaction_id)
