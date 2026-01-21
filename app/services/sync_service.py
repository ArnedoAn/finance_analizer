"""
Sync Service

Handles synchronization between local cache and Firefly III.
Ensures accounts and categories exist before creating transactions.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.firefly import FireflyClient
from app.core.config import get_settings
from app.core.exceptions import FireflyValidationError
from app.core.logging import get_logger
from app.db.repositories import (
    AccountCacheRepository,
    CategoryCacheRepository,
    KnownSenderRepository,
)
from app.models.schemas import AccountType, TransactionAnalysis, TransactionType

logger = get_logger(__name__)


class SyncService:
    """
    Service for synchronizing Firefly III data.
    
    Manages local cache of accounts and categories,
    and handles auto-creation when needed.
    """
    
    def __init__(
        self,
        session: AsyncSession,
        firefly_client: FireflyClient,
    ) -> None:
        self.session = session
        self.firefly = firefly_client
        self.settings = get_settings()
        self._account_repo = AccountCacheRepository(session)
        self._category_repo = CategoryCacheRepository(session)
        self._sender_repo = KnownSenderRepository(session)
    
    async def sync_accounts(self) -> int:
        """
        Sync all accounts from Firefly III to local cache.
        
        Returns:
            Number of accounts synced.
        """
        logger.info("sync_accounts_starting")
        
        accounts = await self.firefly.get_accounts()
        count = await self._account_repo.sync_from_firefly(accounts)
        
        logger.info("sync_accounts_completed", count=count)
        return count
    
    async def sync_categories(self) -> int:
        """
        Sync all categories from Firefly III to local cache.
        
        Returns:
            Number of categories synced.
        """
        logger.info("sync_categories_starting")
        
        categories = await self.firefly.get_categories()
        count = await self._category_repo.sync_from_firefly(categories)
        
        logger.info("sync_categories_completed", count=count)
        return count
    
    async def sync_all(self) -> dict[str, int]:
        """
        Sync all data from Firefly III.
        
        Returns:
            Dictionary with sync counts.
        """
        accounts = await self.sync_accounts()
        categories = await self.sync_categories()
        
        return {
            "accounts": accounts,
            "categories": categories,
        }
    
    async def resolve_source_account(
        self,
        analysis: TransactionAnalysis,
    ) -> dict[str, Any]:
        """
        Resolve the source account for a transaction.
        
        For withdrawals: source is an asset account (your bank/wallet)
        For deposits: source is a revenue account (income source)
        
        If the suggested account doesn't exist:
        1. Tries to find it using email sender (KnownSender)
        2. Attempts to create it if auto_create_accounts is enabled
        3. Falls back to default account only if creation fails or is disabled
        
        Returns:
            Account data dictionary with id and name.
        """
        if analysis.transaction_type == TransactionType.WITHDRAWAL:
            # Source is an asset account (e.g., Bancolombia, Nequi, Lulo)
            suggested_name = analysis.suggested_account_name
            account_type = AccountType.ASSET
            
            logger.debug(
                "resolving_source_asset_account",
                suggested=suggested_name,
                email_sender=analysis.email_sender[:50] if analysis.email_sender else "N/A",
            )
            
            # Try to find the suggested account in cache first
            if suggested_name:
                cached = await self._account_repo.get_by_name(
                    suggested_name, account_type.value
                )
                if cached:
                    logger.debug(
                        "source_account_from_cache",
                        name=cached.name,
                        type=cached.account_type,
                    )
                    return {
                        "id": cached.firefly_id,
                        "name": cached.name,
                        "type": cached.account_type,
                    }
                
                # Also try partial match (e.g., "Lulo" might match "Lulo Bank")
                cached = await self._account_repo.get_by_partial_name(
                    suggested_name, account_type.value
                )
                if cached:
                    logger.info(
                        "source_account_partial_match",
                        suggested=suggested_name,
                        matched=cached.name,
                    )
                    return {
                        "id": cached.firefly_id,
                        "name": cached.name,
                        "type": cached.account_type,
                    }
            
            # If suggested_name didn't match, try using email sender to find bank
            if analysis.email_sender:
                known_sender = await self._sender_repo.find_by_email(analysis.email_sender)
                if known_sender and known_sender.sender_type == "bank":
                    # Use the sender name to find the account
                    bank_name = known_sender.sender_name
                    logger.info(
                        "source_account_from_email_sender",
                        email_sender=analysis.email_sender[:50],
                        bank_name=bank_name,
                    )
                    
                    # Try to find account by bank name
                    cached = await self._account_repo.get_by_name(
                        bank_name, account_type.value
                    )
                    if cached:
                        logger.info(
                            "source_account_found_by_sender",
                            bank_name=bank_name,
                            account_name=cached.name,
                        )
                        return {
                            "id": cached.firefly_id,
                            "name": cached.name,
                            "type": cached.account_type,
                        }
                    
                    # Try partial match with bank name
                    cached = await self._account_repo.get_by_partial_name(
                        bank_name, account_type.value
                    )
                    if cached:
                        logger.info(
                            "source_account_partial_match_by_sender",
                            bank_name=bank_name,
                            matched=cached.name,
                        )
                        return {
                            "id": cached.firefly_id,
                            "name": cached.name,
                            "type": cached.account_type,
                        }
                    
                    # Account not found - try to create it if auto-create is enabled
                    if self.settings.auto_create_accounts:
                        logger.info(
                            "source_account_attempting_creation",
                            bank_name=bank_name,
                            account_type=account_type.value,
                        )
                        try:
                            account = await self.firefly.get_or_create_account(
                                name=bank_name,
                                account_type=account_type,
                                currency_code=analysis.currency,
                            )
                            # Update cache
                            await self._account_repo.upsert(
                                firefly_id=account["id"],
                                name=account["name"],
                                account_type=account["type"],
                                currency_code=account.get("currency_code", analysis.currency),
                            )
                            logger.info(
                                "source_account_created",
                                bank_name=bank_name,
                                account_id=account["id"],
                                account_name=account["name"],
                            )
                            return account
                        except FireflyValidationError as e:
                            logger.error(
                                "source_account_creation_validation_failed",
                                bank_name=bank_name,
                                error_message=e.message,
                                error_details=e.details,
                            )
                            # Continue to fallback
                        except Exception as e:
                            logger.error(
                                "source_account_creation_failed",
                                bank_name=bank_name,
                                error=str(e),
                                error_type=type(e).__name__,
                            )
                            # Continue to fallback
            
            # Determine account name to try creating (prefer bank_name from sender, then suggested_name)
            account_name_to_create = None
            if analysis.email_sender:
                known_sender = await self._sender_repo.find_by_email(analysis.email_sender)
                if known_sender and known_sender.sender_type == "bank":
                    account_name_to_create = known_sender.sender_name
                elif suggested_name:
                    account_name_to_create = suggested_name
            elif suggested_name:
                account_name_to_create = suggested_name
            
            # Try to create the account if we have a name and auto-create is enabled
            if account_name_to_create and self.settings.auto_create_accounts:
                logger.info(
                    "source_account_attempting_creation",
                    account_name=account_name_to_create,
                    account_type=account_type.value,
                )
                try:
                    account = await self.firefly.get_or_create_account(
                        name=account_name_to_create,
                        account_type=account_type,
                        currency_code=analysis.currency,
                    )
                    # Update cache
                    await self._account_repo.upsert(
                        firefly_id=account["id"],
                        name=account["name"],
                        account_type=account["type"],
                        currency_code=account.get("currency_code", analysis.currency),
                    )
                    logger.info(
                        "source_account_created",
                        account_name=account_name_to_create,
                        account_id=account["id"],
                        account_name_resolved=account["name"],
                    )
                    return account
                except FireflyValidationError as e:
                    logger.error(
                        "source_account_creation_validation_failed",
                        account_name=account_name_to_create,
                        error_message=e.message,
                        error_details=e.details,
                    )
                    # Continue to fallback
                except Exception as e:
                    logger.error(
                        "source_account_creation_failed",
                        account_name=account_name_to_create,
                        error=str(e),
                        error_type=type(e).__name__,
                    )
                    # Continue to fallback
            
            # Fall back to default asset account only if creation failed or is disabled
            default_name = self.settings.firefly_default_asset_account
            logger.info(
                "source_account_fallback_to_default",
                suggested=suggested_name,
                email_sender=analysis.email_sender[:50] if analysis.email_sender else "N/A",
                default=default_name,
            )
            
            cached = await self._account_repo.get_by_name(default_name, account_type.value)
            if cached:
                return {
                    "id": cached.firefly_id,
                    "name": cached.name,
                    "type": cached.account_type,
                }
            
            # Fetch from Firefly to ensure we have the account
            account = await self.firefly.get_account_by_name(default_name, account_type)
            if account:
                await self._account_repo.upsert(
                    firefly_id=account["id"],
                    name=account["name"],
                    account_type=account["type"],
                    currency_code=account.get("currency_code", analysis.currency),
                )
                return account
            
            raise ValueError(
                f"Default asset account '{default_name}' not found in Firefly III. "
                f"Please create it manually or update FIREFLY_DEFAULT_ASSET_ACCOUNT."
            )
        else:
            # Source is revenue account (for deposits - income source)
            account_name = (
                analysis.merchant
                or analysis.suggested_account_name
                or self.settings.firefly_default_revenue_account
            )
            account_type = AccountType.REVENUE
            
            logger.debug(
                "resolving_source_revenue_account",
                merchant=analysis.merchant,
                resolved=account_name,
            )
        
            # Check local cache first
            cached = await self._account_repo.get_by_name(account_name, account_type.value)
            if cached:
                logger.debug(
                    "source_account_from_cache",
                    name=cached.name,
                    type=cached.account_type,
                )
                return {
                    "id": cached.firefly_id,
                    "name": cached.name,
                    "type": cached.account_type,
                }
            
            # Get or create in Firefly (revenue accounts can be auto-created)
            logger.info(
                "source_account_get_or_create",
                name=account_name,
                type=account_type.value,
                auto_create=self.settings.auto_create_accounts,
            )
            
            account = await self.firefly.get_or_create_account(
                name=account_name,
                account_type=account_type,
                currency_code=analysis.currency,
            )
            
            # Update cache
            await self._account_repo.upsert(
                firefly_id=account["id"],
                name=account["name"],
                account_type=account["type"],
                currency_code=analysis.currency,
            )
            
            return account
    
    async def resolve_destination_account(
        self,
        analysis: TransactionAnalysis,
    ) -> dict[str, Any]:
        """
        Resolve the destination account for a transaction.
        
        For withdrawals: destination is an expense account (merchant)
        For deposits: destination is an asset account (your bank)
        
        Returns:
            Account data dictionary with id and name.
        """
        if analysis.transaction_type == TransactionType.WITHDRAWAL:
            # Destination is expense account (merchant like Netflix, Amazon, etc.)
            account_name = (
                analysis.merchant
                or self.settings.firefly_default_expense_account
            )
            account_type = AccountType.EXPENSE
            
            logger.debug(
                "resolving_destination_expense_account",
                merchant=analysis.merchant,
                resolved=account_name,
            )
        else:
            # Destination is asset account (for deposits - your bank)
            account_name = (
                analysis.suggested_account_name
                or self.settings.firefly_default_asset_account
            )
            account_type = AccountType.ASSET
            
            logger.debug(
                "resolving_destination_asset_account",
                suggested=analysis.suggested_account_name,
                resolved=account_name,
            )
        
        # Check local cache first
        cached = await self._account_repo.get_by_name(account_name, account_type.value)
        if cached:
            logger.debug(
                "destination_account_from_cache",
                name=cached.name,
                type=cached.account_type,
            )
            return {
                "id": cached.firefly_id,
                "name": cached.name,
                "type": cached.account_type,
            }
        
        # Get or create in Firefly (will auto-create if enabled)
        logger.info(
            "destination_account_get_or_create",
            name=account_name,
            type=account_type.value,
            auto_create=self.settings.auto_create_accounts,
        )
        
        account = await self.firefly.get_or_create_account(
            name=account_name,
            account_type=account_type,
            currency_code=analysis.currency,
        )
        
        # Update cache
        await self._account_repo.upsert(
            firefly_id=account["id"],
            name=account["name"],
            account_type=account["type"],
            currency_code=analysis.currency,
        )
        
        return account
    
    async def resolve_category(
        self,
        category_name: str,
    ) -> dict[str, Any]:
        """
        Resolve category by name, creating if needed.
        
        Returns:
            Category data dictionary with id and name.
        """
        if not category_name:
            category_name = "Sin Categoría"
        
        # Check local cache first
        cached = await self._category_repo.get_by_name(category_name)
        if cached:
            return {
                "id": cached.firefly_id,
                "name": cached.name,
            }
        
        # Get or create in Firefly
        category = await self.firefly.get_or_create_category(category_name)
        
        # Update cache
        await self._category_repo.upsert(
            firefly_id=category["id"],
            name=category["name"],
        )
        
        return category
    
    async def get_cached_accounts(
        self,
        account_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get all cached accounts."""
        accounts = await self._account_repo.get_all(account_type)
        return [
            {
                "id": a.firefly_id,
                "name": a.name,
                "type": a.account_type,
                "currency": a.currency_code,
            }
            for a in accounts
        ]
    
    async def get_cached_categories(self) -> list[dict[str, Any]]:
        """Get all cached categories."""
        categories = await self._category_repo.get_all()
        return [
            {
                "id": c.firefly_id,
                "name": c.name,
            }
            for c in categories
        ]
