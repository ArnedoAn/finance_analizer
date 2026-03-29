"""
Firefly III API Client

Handles all communication with Firefly III personal finance manager.
Provides CRUD operations for accounts, categories, and transactions.
"""

from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import get_settings
from app.core.exceptions import (
    FireflyAPIError,
    FireflyAuthenticationError,
    FireflyDuplicateError,
    FireflyNotFoundError,
    FireflyValidationError,
)
from app.core.logging import get_logger
from app.core.security import get_token_encryption
from app.core.session import DEFAULT_SESSION_ID, normalize_session_id
from app.models.schemas import (
    AccountCreate,
    AccountResponse,
    AccountType,
    CategoryCreate,
    CategoryResponse,
    TransactionCreate,
    TransactionResponse,
    TransactionType,
)

logger = get_logger(__name__)


class FireflyClient:
    """
    Client for Firefly III API.
    
    Provides methods for managing accounts, categories, tags,
    and transactions in Firefly III.
    """
    
    def __init__(self, session_id: str = DEFAULT_SESSION_ID) -> None:
        self.settings = get_settings()
        normalized_session_id = normalize_session_id(session_id)
        if normalized_session_id is None:
            raise ValueError(f"Invalid session id: {session_id}")
        self.session_id = normalized_session_id
        self._token_path = self._resolve_token_path(normalized_session_id)
        self._token: str | None = None
        self._token_source: str | None = None
        self._token_loaded = False
        self._client: httpx.AsyncClient | None = None
        self._base_url = self.settings.firefly_base_url.rstrip("/")

    def _resolve_token_path(self, session_id: str) -> Path:
        """
        Resolve token path for a session.

        Uses the legacy env token for default session unless a session token
        was explicitly stored.
        """
        base = self.settings.firefly_token_path
        if session_id == DEFAULT_SESSION_ID:
            return base
        return base.with_name(f"{base.stem}_{session_id}{base.suffix}")

    async def _load_session_token(self) -> str | None:
        """Load session-scoped Firefly token from encrypted storage."""
        token_path = self._token_path
        if not token_path.exists():
            return None
        try:
            encryption = get_token_encryption()
            encrypted_data = token_path.read_text()
            token_data = encryption.decrypt_dict(encrypted_data)
            token = token_data.get("token")
            if isinstance(token, str) and token.strip():
                return token.strip()
            return None
        except Exception as e:
            logger.warning(
                "firefly_failed_load_session_token",
                session_id=self.session_id,
                error=str(e),
            )
            return None

    async def _save_session_token(self, token: str) -> None:
        """Persist a session-scoped Firefly token in encrypted storage."""
        token_path = self._token_path
        token_path.parent.mkdir(parents=True, exist_ok=True)
        encryption = get_token_encryption()
        encrypted = encryption.encrypt_dict({"token": token})
        token_path.write_text(encrypted)

    async def _delete_session_token(self) -> None:
        """Delete session-scoped Firefly token from disk."""
        token_path = self._token_path
        if token_path.exists():
            token_path.unlink()

    async def set_session_token(self, token: str) -> None:
        """Set and persist token for this session."""
        normalized = token.strip()
        if not normalized:
            raise FireflyAuthenticationError("Firefly token cannot be empty")
        await self._save_session_token(normalized)
        self._token = normalized
        self._token_source = "session"
        self._token_loaded = True
        await self.close()
        logger.info("firefly_session_token_updated", session_id=self.session_id)

    async def clear_session_token(self) -> None:
        """Clear persisted token for this session and reset runtime client."""
        await self._delete_session_token()
        self._token = None
        self._token_source = None
        self._token_loaded = True
        await self.close()
        logger.info("firefly_session_token_cleared", session_id=self.session_id)

    async def has_session_token(self) -> bool:
        """Check whether this session has a usable Firefly token."""
        token = await self._resolve_token()
        return token is not None

    async def get_token_source(self) -> str | None:
        """Return token source: session, default_env, or None."""
        await self._resolve_token()
        return self._token_source

    async def get_active_token(self) -> str:
        """Return the active token or raise if missing."""
        token = await self._resolve_token()
        if token is None:
            raise FireflyAuthenticationError(
                "No Firefly token configured for this session",
                details={
                    "session_id": self.session_id,
                    "action": "set_firefly_token",
                },
            )
        return token

    async def _resolve_token(self) -> str | None:
        """Resolve token using session storage with env fallback for default."""
        if self._token_loaded:
            return self._token

        token = await self._load_session_token()
        if token:
            self._token = token
            self._token_source = "session"
            self._token_loaded = True
            return token

        if self.session_id == DEFAULT_SESSION_ID:
            fallback = self.settings.firefly_api_token.get_secret_value().strip()
            self._token = fallback if fallback else None
            self._token_source = "default_env" if self._token else None
            self._token_loaded = True
            return self._token

        self._token = None
        self._token_source = None
        self._token_loaded = True
        return None
    
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        token = await self.get_active_token()

        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=f"{self._base_url}/api/v1",
                timeout=httpx.Timeout(self.settings.firefly_timeout),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/vnd.api+json",
                },
            )
        return self._client
    
    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
    
    def _handle_error(self, response: httpx.Response, context: str = "") -> None:
        """Handle API error responses."""
        status = response.status_code
        
        try:
            error_data = response.json()
            message = error_data.get("message", response.text[:500])
            errors = error_data.get("errors", {})
        except Exception:
            message = response.text[:500]
            errors = {}
        
        if status == 401:
            raise FireflyAuthenticationError(
                "Firefly III authentication failed",
                details={"message": message},
            )
        
        if status == 404:
            raise FireflyNotFoundError(
                f"Resource not found: {context}",
                details={"message": message},
            )
        
        if status == 422:
            # Check for duplicate
            if "duplicate" in message.lower():
                raise FireflyDuplicateError(
                    f"Duplicate entry: {context}",
                    details={"message": message, "errors": errors},
                )
            raise FireflyValidationError(
                f"Validation error: {context}",
                details={"message": message, "errors": errors},
            )
        
        raise FireflyAPIError(
            f"Firefly API error ({status}): {context}",
            details={"status": status, "message": message},
        )
    
    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before_sleep=lambda retry_state: logger.warning(
            "firefly_retry",
            attempt=retry_state.attempt_number,
        ),
    )
    async def _request(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """Make API request with retry logic."""
        client = await self._get_client()
        
        response = await client.request(method, endpoint, **kwargs)
        
        if not response.is_success:
            self._handle_error(response, endpoint)
        
        if response.status_code == 204:
            return None
        
        return response.json()
    
    # =========================================================================
    # Account Operations
    # =========================================================================
    
    async def get_accounts(
        self,
        account_type: AccountType | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get all accounts, optionally filtered by type.
        
        Args:
            account_type: Filter by account type.
            
        Returns:
            List of account data dictionaries.
        """
        params = {}
        if account_type:
            params["type"] = account_type.value
        
        accounts = []
        page = 1
        
        while True:
            params["page"] = page
            response = await self._request("GET", "/accounts", params=params)
            
            data = response.get("data", [])
            if not data:
                break
            
            for item in data:
                attrs = item.get("attributes", {})
                accounts.append({
                    "id": item.get("id"),
                    "name": attrs.get("name"),
                    "type": attrs.get("type"),
                    "currency_code": attrs.get("currency_code", "USD"),
                    "active": attrs.get("active", True),
                    "current_balance": attrs.get("current_balance", "0"),
                })
            
            # Check for more pages
            meta = response.get("meta", {})
            pagination = meta.get("pagination", {})
            if page >= pagination.get("total_pages", 1):
                break
            page += 1
        
        logger.info("firefly_accounts_fetched", count=len(accounts))
        return accounts
    
    async def get_account_by_name(
        self,
        name: str,
        account_type: AccountType,
    ) -> dict[str, Any] | None:
        """
        Get account by name and type (case-insensitive, flexible matching).
        
        Tries exact match first, then partial match.
        """
        accounts = await self.get_accounts(account_type)
        name_lower = name.lower().strip()
        
        # First try exact match (case-insensitive)
        for acc in accounts:
            if acc["name"].lower().strip() == name_lower:
                return acc
        
        # Then try partial match (name contains search or search contains name)
        for acc in accounts:
            acc_name_lower = acc["name"].lower().strip()
            if name_lower in acc_name_lower or acc_name_lower in name_lower:
                logger.debug(
                    "firefly_account_partial_match",
                    searched=name,
                    found=acc["name"],
                )
                return acc
        
        return None
    
    async def create_account(self, account: AccountCreate) -> dict[str, Any]:
        """
        Create a new account.
        
        Args:
            account: Account creation data.
            
        Returns:
            Created account data.
        """
        payload = {
            "name": account.name,
            "type": account.type.value,
            "currency_code": account.currency_code,
            "active": account.active,
            "include_net_worth": account.include_net_worth,
        }
        
        # Account role is required for asset accounts in Firefly III
        if account.type == AccountType.ASSET:
            payload["account_role"] = account.account_role or "defaultAsset"
        
        if account.notes:
            payload["notes"] = account.notes
        
        logger.info(
            "firefly_creating_account",
            name=account.name,
            type=account.type.value,
            currency_code=account.currency_code,
            payload=payload,
        )
        
        try:
            response = await self._request("POST", "/accounts", json=payload)
        except FireflyValidationError as e:
            # Log full error details for debugging
            logger.error(
                "firefly_account_validation_failed",
                name=account.name,
                type=account.type.value,
                error_message=e.message,
                error_details=e.details,
            )
            raise
        except Exception as e:
            logger.error(
                "firefly_account_creation_error",
                name=account.name,
                type=account.type.value,
                error=str(e),
            )
            raise
        
        data = response.get("data", {})
        attrs = data.get("attributes", {})
        
        result = {
            "id": data.get("id"),
            "name": attrs.get("name"),
            "type": attrs.get("type"),
            "currency_code": attrs.get("currency_code", "USD"),
            "active": attrs.get("active", True),
        }
        
        logger.info("firefly_account_created", id=result["id"], name=result["name"])
        return result
    
    async def get_or_create_account(
        self,
        name: str,
        account_type: AccountType,
        currency_code: str = "USD",
    ) -> dict[str, Any]:
        """
        Get account by name or create if not exists.
        
        First checks if account exists (case-insensitive), then creates if needed.
        """
        # Try to find existing (case-insensitive search)
        existing = await self.get_account_by_name(name, account_type)
        if existing:
            logger.debug(
                "firefly_account_exists",
                name=name,
                account_id=existing["id"],
            )
            return existing
        
        # Create new
        if not self.settings.auto_create_accounts:
            raise FireflyNotFoundError(
                f"Account not found and auto-create disabled: {name}",
                details={"name": name, "type": account_type.value},
            )
        
        logger.info(
            "firefly_creating_new_account",
            name=name,
            type=account_type.value,
            currency_code=currency_code,
        )
        
        return await self.create_account(AccountCreate(
            name=name,
            type=account_type,
            currency_code=currency_code,
            notes=f"Auto-created by Finance Analyzer",
        ))
    
    # =========================================================================
    # Category Operations
    # =========================================================================
    
    async def get_categories(self) -> list[dict[str, Any]]:
        """Get all categories."""
        categories = []
        page = 1
        
        while True:
            response = await self._request(
                "GET", "/categories", params={"page": page}
            )
            
            data = response.get("data", [])
            if not data:
                break
            
            for item in data:
                attrs = item.get("attributes", {})
                categories.append({
                    "id": item.get("id"),
                    "name": attrs.get("name"),
                })
            
            meta = response.get("meta", {})
            pagination = meta.get("pagination", {})
            if page >= pagination.get("total_pages", 1):
                break
            page += 1
        
        logger.info("firefly_categories_fetched", count=len(categories))
        return categories
    
    async def get_category_by_name(self, name: str) -> dict[str, Any] | None:
        """Get category by name."""
        categories = await self.get_categories()
        
        for cat in categories:
            if cat["name"].lower() == name.lower():
                return cat
        
        return None
    
    async def create_category(self, category: CategoryCreate) -> dict[str, Any]:
        """Create a new category."""
        payload = {"name": category.name}
        if category.notes:
            payload["notes"] = category.notes
        
        logger.info("firefly_creating_category", name=category.name)
        
        response = await self._request("POST", "/categories", json=payload)
        
        data = response.get("data", {})
        attrs = data.get("attributes", {})
        
        result = {
            "id": data.get("id"),
            "name": attrs.get("name"),
        }
        
        logger.info("firefly_category_created", id=result["id"], name=result["name"])
        return result
    
    async def get_or_create_category(self, name: str) -> dict[str, Any]:
        """Get category by name or create if not exists."""
        existing = await self.get_category_by_name(name)
        if existing:
            return existing
        
        if not self.settings.auto_create_categories:
            raise FireflyNotFoundError(
                f"Category not found and auto-create disabled: {name}",
                details={"name": name},
            )
        
        return await self.create_category(CategoryCreate(
            name=name,
            notes=f"Auto-created by Finance Analyzer",
        ))
    
    # =========================================================================
    # Tag Operations
    # =========================================================================
    
    async def get_tags(self) -> list[dict[str, Any]]:
        """Get all tags."""
        tags = []
        page = 1
        
        while True:
            response = await self._request(
                "GET", "/tags", params={"page": page}
            )
            
            data = response.get("data", [])
            if not data:
                break
            
            for item in data:
                attrs = item.get("attributes", {})
                tags.append({
                    "id": item.get("id"),
                    "tag": attrs.get("tag"),
                })
            
            meta = response.get("meta", {})
            pagination = meta.get("pagination", {})
            if page >= pagination.get("total_pages", 1):
                break
            page += 1
        
        return tags
    
    async def create_tag(self, tag: str) -> dict[str, Any]:
        """Create a new tag."""
        response = await self._request("POST", "/tags", json={"tag": tag})
        
        data = response.get("data", {})
        attrs = data.get("attributes", {})
        
        return {
            "id": data.get("id"),
            "tag": attrs.get("tag"),
        }
    
    async def get_or_create_tag(self, tag: str) -> dict[str, Any]:
        """Get tag or create if not exists."""
        tags = await self.get_tags()
        
        for t in tags:
            if t["tag"].lower() == tag.lower():
                return t
        
        return await self.create_tag(tag)
    
    # =========================================================================
    # Transaction Operations
    # =========================================================================
    
    async def create_transaction(
        self,
        transaction: TransactionCreate,
    ) -> dict[str, Any]:
        """
        Create a new transaction.
        
        Args:
            transaction: Transaction creation data.
            
        Returns:
            Created transaction data.
            
        Raises:
            FireflyDuplicateError: If duplicate detected.
            FireflyValidationError: If validation fails.
        """
        payload = {
            "error_if_duplicate_hash": transaction.error_if_duplicate_hash,
            "apply_rules": transaction.apply_rules,
            "fire_webhooks": transaction.fire_webhooks,
            "transactions": [
                {
                    "type": split.type.value,
                    "date": split.date,
                    "amount": split.amount,
                    "description": split.description,
                    "currency_code": split.currency_code,
                }
                for split in transaction.transactions
            ],
        }
        
        # Add optional fields for each split
        for i, split in enumerate(transaction.transactions):
            tx_payload = payload["transactions"][i]
            
            if split.source_name:
                tx_payload["source_name"] = split.source_name
            if split.source_id:
                tx_payload["source_id"] = split.source_id
            if split.destination_name:
                tx_payload["destination_name"] = split.destination_name
            if split.destination_id:
                tx_payload["destination_id"] = split.destination_id
            if split.category_name:
                tx_payload["category_name"] = split.category_name
            if split.category_id:
                tx_payload["category_id"] = split.category_id
            if split.tags:
                tx_payload["tags"] = split.tags
            if split.notes:
                tx_payload["notes"] = split.notes
            if split.external_id:
                tx_payload["external_id"] = split.external_id
        
        logger.info(
            "firefly_creating_transaction",
            description=transaction.transactions[0].description[:50],
            amount=transaction.transactions[0].amount,
        )
        
        try:
            response = await self._request("POST", "/transactions", json=payload)
        except FireflyDuplicateError:
            logger.warning(
                "firefly_duplicate_transaction",
                description=transaction.transactions[0].description[:50],
            )
            raise
        
        data = response.get("data", {})
        attrs = data.get("attributes", {})
        transactions = attrs.get("transactions", [{}])
        first_tx = transactions[0] if transactions else {}
        
        result = {
            "id": data.get("id"),
            "transaction_journal_id": first_tx.get("transaction_journal_id"),
            "type": first_tx.get("type"),
            "date": first_tx.get("date"),
            "amount": first_tx.get("amount"),
            "description": first_tx.get("description"),
            "source_name": first_tx.get("source_name"),
            "destination_name": first_tx.get("destination_name"),
            "category_name": first_tx.get("category_name"),
        }
        
        logger.info(
            "firefly_transaction_created",
            id=result["id"],
            journal_id=result["transaction_journal_id"],
        )
        
        return result
    
    async def get_transaction(self, transaction_id: str) -> dict[str, Any]:
        """Get transaction by ID."""
        response = await self._request("GET", f"/transactions/{transaction_id}")
        
        data = response.get("data", {})
        attrs = data.get("attributes", {})
        transactions = attrs.get("transactions", [{}])
        first_tx = transactions[0] if transactions else {}
        
        return {
            "id": data.get("id"),
            "transaction_journal_id": first_tx.get("transaction_journal_id"),
            "type": first_tx.get("type"),
            "date": first_tx.get("date"),
            "amount": first_tx.get("amount"),
            "description": first_tx.get("description"),
            "source_name": first_tx.get("source_name"),
            "destination_name": first_tx.get("destination_name"),
            "category_name": first_tx.get("category_name"),
        }
    
    # =========================================================================
    # Utility Methods
    # =========================================================================
    
    async def check_connection(self) -> bool:
        """Check if Firefly III is accessible."""
        try:
            response = await self._request("GET", "/about")
            version = response.get("data", {}).get("version")
            logger.info("firefly_connection_ok", version=version)
            return True
        except Exception as e:
            logger.error("firefly_connection_failed", error=str(e))
            return False
    
    async def get_about(self) -> dict[str, Any]:
        """Get Firefly III server info."""
        response = await self._request("GET", "/about")
        return response.get("data", {})
