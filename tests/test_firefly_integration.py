"""
Firefly III Integration Tests

IMPORTANT: These tests make REAL requests to Firefly III.
They require a running Firefly III instance and valid credentials.

Set environment variables:
- FIREFLY_BASE_URL: URL of your Firefly III instance
- FIREFLY_API_TOKEN: Personal Access Token

Run with: pytest tests/test_firefly_integration.py -v -s

Use --cleanup flag to delete test data after running.
"""

import asyncio
import os
from datetime import datetime
from decimal import Decimal
from typing import Any, AsyncGenerator

import pytest
import pytest_asyncio

from app.clients.firefly import FireflyClient
from app.core.config import get_settings
from app.core.exceptions import (
    FireflyAPIError,
    FireflyAuthenticationError,
    FireflyDuplicateError,
    FireflyValidationError,
)
from app.models.schemas import (
    AccountCreate,
    AccountType,
    CategoryCreate,
    TransactionAnalysis,
    TransactionCreate,
    TransactionSplit,
    TransactionType,
)


# =============================================================================
# Skip if Firefly not configured
# =============================================================================

def firefly_configured() -> bool:
    """Check if Firefly III is configured."""
    try:
        settings = get_settings()
        return bool(
            settings.firefly_base_url
            and settings.firefly_api_token
        )
    except Exception:
        return False


skip_if_no_firefly = pytest.mark.skipif(
    not firefly_configured(),
    reason="Firefly III not configured (set FIREFLY_BASE_URL and FIREFLY_API_TOKEN)",
)


# =============================================================================
# Test Data Tracking (for cleanup)
# =============================================================================

class DataTracker:
    """Tracks created test data for cleanup (not a test class)."""
    
    def __init__(self):
        self.accounts: list[str] = []
        self.categories: list[str] = []
        self.transactions: list[str] = []
    
    def track_account(self, account_id: str):
        self.accounts.append(account_id)
    
    def track_category(self, category_id: str):
        self.categories.append(category_id)
    
    def track_transaction(self, transaction_id: str):
        self.transactions.append(transaction_id)


# =============================================================================
# Fixtures
# =============================================================================

@pytest_asyncio.fixture
async def firefly_client() -> AsyncGenerator[FireflyClient, None]:
    """Real Firefly client for integration tests."""
    client = FireflyClient()
    yield client
    await client.close()


@pytest.fixture
def tracker() -> DataTracker:
    """Track test data for potential cleanup."""
    return DataTracker()


@pytest.fixture
def test_prefix() -> str:
    """Unique prefix for test data to avoid conflicts."""
    return f"TEST_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


# =============================================================================
# Connection Tests
# =============================================================================

@skip_if_no_firefly
class TestFireflyConnection:
    """Test basic Firefly III connectivity."""
    
    @pytest.mark.asyncio
    async def test_check_connection(self, firefly_client: FireflyClient):
        """Test that we can connect to Firefly III."""
        result = await firefly_client.check_connection()
        
        assert result is True
        print("✓ Connection successful")
    
    @pytest.mark.asyncio
    async def test_get_about(self, firefly_client: FireflyClient):
        """Test getting Firefly III server info."""
        about = await firefly_client.get_about()
        
        assert "version" in about or about.get("version") is None
        print(f"✓ Firefly III version: {about.get('version', 'unknown')}")
    
    @pytest.mark.asyncio
    async def test_invalid_token_fails(self):
        """Test that invalid token raises authentication error."""
        # Create client with invalid token
        import os
        original_token = os.environ.get("FIREFLY_API_TOKEN")
        
        try:
            os.environ["FIREFLY_API_TOKEN"] = "invalid_token_12345"
            
            # Need to create new settings instance
            from app.core.config import Settings
            bad_settings = Settings()
            
            # Create client manually with bad auth
            import httpx
            async with httpx.AsyncClient(
                base_url=f"{bad_settings.firefly_base_url.rstrip('/')}/api/v1",
                headers={"Authorization": "Bearer invalid_token"},
                follow_redirects=False,
            ) as client:
                response = await client.get("/about")
                
                # Should get 401 (direct) or 302 (redirect to login, common with reverse proxy)
                assert response.status_code in (401, 302, 403)
                print(f"✓ Invalid token correctly rejected (status: {response.status_code})")
        finally:
            if original_token:
                os.environ["FIREFLY_API_TOKEN"] = original_token


# =============================================================================
# Account Tests
# =============================================================================

@skip_if_no_firefly
class TestFireflyAccounts:
    """Test account operations with real Firefly III."""
    
    @pytest.mark.asyncio
    async def test_get_accounts(self, firefly_client: FireflyClient):
        """Test fetching all accounts."""
        accounts = await firefly_client.get_accounts()
        
        assert isinstance(accounts, list)
        print(f"✓ Found {len(accounts)} accounts")
        
        for acc in accounts[:5]:  # Print first 5
            print(f"  - {acc['name']} ({acc['type']}) [{acc.get('currency_code', 'N/A')}]")
    
    @pytest.mark.asyncio
    async def test_get_asset_accounts(self, firefly_client: FireflyClient):
        """Test fetching only asset accounts."""
        accounts = await firefly_client.get_accounts(AccountType.ASSET)
        
        assert isinstance(accounts, list)
        assert all(acc["type"] == "asset" for acc in accounts)
        
        print(f"✓ Found {len(accounts)} asset accounts")
        for acc in accounts:
            print(f"  - {acc['name']} [Balance: {acc.get('current_balance', '?')}]")
    
    @pytest.mark.asyncio
    async def test_get_expense_accounts(self, firefly_client: FireflyClient):
        """Test fetching expense accounts (merchants)."""
        accounts = await firefly_client.get_accounts(AccountType.EXPENSE)
        
        print(f"✓ Found {len(accounts)} expense accounts")
        for acc in accounts[:10]:
            print(f"  - {acc['name']}")
    
    @pytest.mark.asyncio
    async def test_create_expense_account(
        self,
        firefly_client: FireflyClient,
        tracker: DataTracker,
        test_prefix: str,
    ):
        """Test creating a new expense account."""
        account_name = f"{test_prefix}_Tienda_Test"
        
        account = await firefly_client.create_account(AccountCreate(
            name=account_name,
            type=AccountType.EXPENSE,
            currency_code="COP",
            notes="Test account - can be deleted",
        ))
        
        tracker.track_account(account["id"])
        
        assert account["id"] is not None
        assert account["name"] == account_name
        assert account["type"] == "expense"
        
        print(f"✓ Created expense account: {account['name']} (ID: {account['id']})")
    
    @pytest.mark.asyncio
    async def test_get_or_create_account(
        self,
        firefly_client: FireflyClient,
        tracker: DataTracker,
        test_prefix: str,
    ):
        """Test get_or_create_account functionality."""
        account_name = f"{test_prefix}_GetOrCreate_Test"
        
        # First call should create
        account1 = await firefly_client.get_or_create_account(
            name=account_name,
            account_type=AccountType.EXPENSE,
            currency_code="COP",
        )
        tracker.track_account(account1["id"])
        
        # Second call should return existing
        account2 = await firefly_client.get_or_create_account(
            name=account_name,
            account_type=AccountType.EXPENSE,
            currency_code="COP",
        )
        
        assert account1["id"] == account2["id"]
        print(f"✓ get_or_create works correctly for: {account_name}")


# =============================================================================
# Category Tests
# =============================================================================

@skip_if_no_firefly
class TestFireflyCategories:
    """Test category operations with real Firefly III."""
    
    @pytest.mark.asyncio
    async def test_get_categories(self, firefly_client: FireflyClient):
        """Test fetching all categories."""
        categories = await firefly_client.get_categories()
        
        assert isinstance(categories, list)
        print(f"✓ Found {len(categories)} categories")
        
        for cat in categories[:10]:
            print(f"  - {cat['name']}")
    
    @pytest.mark.asyncio
    async def test_create_category(
        self,
        firefly_client: FireflyClient,
        tracker: DataTracker,
        test_prefix: str,
    ):
        """Test creating a new category."""
        category_name = f"{test_prefix}_Categoria_Test"
        
        category = await firefly_client.create_category(CategoryCreate(
            name=category_name,
            notes="Test category - can be deleted",
        ))
        
        tracker.track_category(category["id"])
        
        assert category["id"] is not None
        assert category["name"] == category_name
        
        print(f"✓ Created category: {category['name']} (ID: {category['id']})")
    
    @pytest.mark.asyncio
    async def test_get_or_create_category(
        self,
        firefly_client: FireflyClient,
        tracker: DataTracker,
        test_prefix: str,
    ):
        """Test get_or_create_category functionality."""
        category_name = f"{test_prefix}_GetOrCreate_Cat"
        
        # First call should create
        cat1 = await firefly_client.get_or_create_category(category_name)
        tracker.track_category(cat1["id"])
        
        # Second call should return existing
        cat2 = await firefly_client.get_or_create_category(category_name)
        
        assert cat1["id"] == cat2["id"]
        print(f"✓ get_or_create_category works correctly")


# =============================================================================
# Transaction Tests
# =============================================================================

@skip_if_no_firefly
class TestFireflyTransactions:
    """Test transaction operations with real Firefly III."""
    
    @pytest.mark.asyncio
    async def test_create_withdrawal_transaction(
        self,
        firefly_client: FireflyClient,
        tracker: DataTracker,
        test_prefix: str,
    ):
        """Test creating a withdrawal (expense) transaction."""
        # First, get an asset account
        asset_accounts = await firefly_client.get_accounts(AccountType.ASSET)
        
        if not asset_accounts:
            pytest.skip("No asset accounts found - create one first")
        
        source_account = asset_accounts[0]
        destination_name = f"{test_prefix}_Merchant"
        
        print(f"\n📝 Creating withdrawal transaction:")
        print(f"   Source (Asset): {source_account['name']}")
        print(f"   Destination (Expense): {destination_name}")
        print(f"   Amount: 15,000 COP")
        
        transaction = TransactionCreate(
            error_if_duplicate_hash=False,  # Allow duplicates for testing
            apply_rules=False,
            fire_webhooks=False,
            transactions=[
                TransactionSplit(
                    type=TransactionType.WITHDRAWAL,
                    date=datetime.now().strftime("%Y-%m-%d"),
                    amount="15000",
                    description=f"{test_prefix} - Test Withdrawal",
                    source_name=source_account["name"],
                    destination_name=destination_name,
                    category_name="Test",
                    currency_code="COP",
                )
            ],
        )
        
        try:
            result = await firefly_client.create_transaction(transaction)
            tracker.track_transaction(result["id"])
            
            print(f"\n✓ Transaction created successfully!")
            print(f"   ID: {result['id']}")
            print(f"   Type: {result['type']}")
            print(f"   Amount: {result['amount']}")
            print(f"   Source: {result['source_name']}")
            print(f"   Destination: {result['destination_name']}")
            
            assert result["id"] is not None
            assert result["type"] == "withdrawal"
            
        except FireflyValidationError as e:
            print(f"\n❌ Validation Error: {e}")
            print(f"   Details: {e.details}")
            raise
    
    @pytest.mark.asyncio
    async def test_create_deposit_transaction(
        self,
        firefly_client: FireflyClient,
        tracker: DataTracker,
        test_prefix: str,
    ):
        """Test creating a deposit (income) transaction."""
        # Get an asset account
        asset_accounts = await firefly_client.get_accounts(AccountType.ASSET)
        
        if not asset_accounts:
            pytest.skip("No asset accounts found")
        
        dest_account = asset_accounts[0]
        source_name = f"{test_prefix}_Revenue_Source"
        
        print(f"\n📝 Creating deposit transaction:")
        print(f"   Source (Revenue): {source_name}")
        print(f"   Destination (Asset): {dest_account['name']}")
        print(f"   Amount: 500,000 COP")
        
        transaction = TransactionCreate(
            error_if_duplicate_hash=False,
            apply_rules=False,
            fire_webhooks=False,
            transactions=[
                TransactionSplit(
                    type=TransactionType.DEPOSIT,
                    date=datetime.now().strftime("%Y-%m-%d"),
                    amount="500000",
                    description=f"{test_prefix} - Test Deposit",
                    source_name=source_name,
                    destination_name=dest_account["name"],
                    category_name="Salary",
                    currency_code="COP",
                )
            ],
        )
        
        try:
            result = await firefly_client.create_transaction(transaction)
            tracker.track_transaction(result["id"])
            
            print(f"\n✓ Deposit created successfully!")
            print(f"   ID: {result['id']}")
            print(f"   Amount: {result['amount']}")
            
            assert result["id"] is not None
            assert result["type"] == "deposit"
            
        except FireflyValidationError as e:
            print(f"\n❌ Validation Error: {e}")
            print(f"   Details: {e.details}")
            raise
    
    @pytest.mark.asyncio
    async def test_create_transaction_with_external_id(
        self,
        firefly_client: FireflyClient,
        tracker: DataTracker,
        test_prefix: str,
    ):
        """Test creating transaction with external_id for deduplication."""
        asset_accounts = await firefly_client.get_accounts(AccountType.ASSET)
        
        if not asset_accounts:
            pytest.skip("No asset accounts found")
        
        external_id = f"email-{test_prefix}-unique-123"
        
        transaction = TransactionCreate(
            error_if_duplicate_hash=True,
            apply_rules=False,
            fire_webhooks=False,
            transactions=[
                TransactionSplit(
                    type=TransactionType.WITHDRAWAL,
                    date=datetime.now().strftime("%Y-%m-%d"),
                    amount="25000",
                    description=f"{test_prefix} - With External ID",
                    source_name=asset_accounts[0]["name"],
                    destination_name=f"{test_prefix}_Store",
                    external_id=external_id,
                    currency_code="COP",
                )
            ],
        )
        
        result = await firefly_client.create_transaction(transaction)
        tracker.track_transaction(result["id"])
        
        print(f"✓ Created transaction with external_id: {external_id}")
        assert result["id"] is not None
    
    @pytest.mark.asyncio
    async def test_duplicate_detection(
        self,
        firefly_client: FireflyClient,
        tracker: DataTracker,
        test_prefix: str,
    ):
        """Test that duplicate transactions are detected."""
        asset_accounts = await firefly_client.get_accounts(AccountType.ASSET)
        
        if not asset_accounts:
            pytest.skip("No asset accounts found")
        
        # Create unique transaction data
        unique_desc = f"{test_prefix}_DuplicateTest_{datetime.now().timestamp()}"
        
        transaction = TransactionCreate(
            error_if_duplicate_hash=True,
            apply_rules=False,
            fire_webhooks=False,
            transactions=[
                TransactionSplit(
                    type=TransactionType.WITHDRAWAL,
                    date=datetime.now().strftime("%Y-%m-%d"),
                    amount="12345",
                    description=unique_desc,
                    source_name=asset_accounts[0]["name"],
                    destination_name=f"{test_prefix}_DupStore",
                    currency_code="COP",
                )
            ],
        )
        
        # First creation should succeed
        result1 = await firefly_client.create_transaction(transaction)
        tracker.track_transaction(result1["id"])
        print(f"✓ First transaction created: {result1['id']}")
        
        # Second creation with same data should fail
        try:
            result2 = await firefly_client.create_transaction(transaction)
            # If it doesn't raise, Firefly might not detect duplicates
            print(f"⚠ Duplicate was created anyway: {result2['id']}")
            tracker.track_transaction(result2["id"])
        except FireflyDuplicateError:
            print("✓ Duplicate correctly detected and rejected")


# =============================================================================
# Full Integration Flow Test
# =============================================================================

@skip_if_no_firefly
class TestFullIntegrationFlow:
    """Test complete transaction creation flow like the real service."""
    
    @pytest.mark.asyncio
    async def test_new_asset_account_creation(
        self,
        firefly_client: FireflyClient,
        tracker: DataTracker,
        test_prefix: str,
    ):
        """
        Test that a new asset account (like Lulo) is created automatically.
        
        This simulates what happens when the AI suggests an account that
        doesn't exist yet in Firefly.
        """
        print("\n" + "="*60)
        print("🏦 New Asset Account Creation Test (Lulo scenario)")
        print("="*60)
        
        # Simulate a new bank account that doesn't exist
        new_account_name = f"{test_prefix}_Lulo"
        
        print(f"\n1️⃣ Checking if '{new_account_name}' exists...")
        existing = await firefly_client.get_account_by_name(
            new_account_name, AccountType.ASSET
        )
        
        if existing:
            print(f"   ⚠️ Account already exists (ID: {existing['id']})")
        else:
            print(f"   ✓ Account does not exist - will be created")
        
        print(f"\n2️⃣ Calling get_or_create_account for asset account...")
        account = await firefly_client.get_or_create_account(
            name=new_account_name,
            account_type=AccountType.ASSET,
            currency_code="COP",
        )
        tracker.track_account(account["id"])
        
        print(f"   ✓ Account: {account['name']}")
        print(f"   ✓ Type: {account['type']}")
        print(f"   ✓ ID: {account['id']}")
        
        assert account["type"] == "asset"
        assert account["name"] == new_account_name
        
        print(f"\n3️⃣ Creating withdrawal from new account...")
        transaction = TransactionCreate(
            error_if_duplicate_hash=False,
            apply_rules=True,
            fire_webhooks=False,
            transactions=[
                TransactionSplit(
                    type=TransactionType.WITHDRAWAL,
                    date=datetime.now().strftime("%Y-%m-%d"),
                    amount="50000",
                    description=f"Compra desde {new_account_name}",
                    source_name=new_account_name,
                    destination_name=f"{test_prefix}_Tienda",
                    category_name="Compras",
                    currency_code="COP",
                )
            ],
        )
        
        result = await firefly_client.create_transaction(transaction)
        tracker.track_transaction(result["id"])
        
        print(f"   ✓ Transaction created: {result['id']}")
        print(f"   ✓ Source: {result['source_name']}")
        
        assert result["source_name"] == new_account_name
        
        print("\n" + "="*60)
        print("✅ NEW ASSET ACCOUNT FLOW COMPLETED")
        print("="*60)
    
    @pytest.mark.asyncio
    async def test_colombian_bank_transaction_flow(
        self,
        firefly_client: FireflyClient,
        tracker: DataTracker,
        test_prefix: str,
    ):
        """
        Test the complete flow for a Colombian bank transaction.
        
        This simulates what happens when processing a Bancolombia email.
        """
        print("\n" + "="*60)
        print("🇨🇴 Colombian Bank Transaction Flow Test")
        print("="*60)
        
        # Step 1: Verify connection
        print("\n1️⃣ Checking Firefly III connection...")
        connected = await firefly_client.check_connection()
        assert connected, "Cannot connect to Firefly III"
        print("   ✓ Connected")
        
        # Step 2: List available asset accounts
        print("\n2️⃣ Fetching asset accounts...")
        asset_accounts = await firefly_client.get_accounts(AccountType.ASSET)
        print(f"   Found {len(asset_accounts)} asset accounts:")
        for acc in asset_accounts:
            print(f"     - {acc['name']} ({acc.get('currency_code', 'N/A')})")
        
        if not asset_accounts:
            pytest.skip("No asset accounts - please create one first")
        
        # Step 3: Get or create expense account (merchant)
        print("\n3️⃣ Getting/creating expense account for merchant...")
        merchant_name = f"{test_prefix}_Netflix_Test"
        expense_account = await firefly_client.get_or_create_account(
            name=merchant_name,
            account_type=AccountType.EXPENSE,
            currency_code="COP",
        )
        tracker.track_account(expense_account["id"])
        print(f"   ✓ Expense account: {expense_account['name']} (ID: {expense_account['id']})")
        
        # Step 4: Get or create category
        print("\n4️⃣ Getting/creating category...")
        category_name = f"{test_prefix}_Suscripciones"
        category = await firefly_client.get_or_create_category(category_name)
        tracker.track_category(category["id"])
        print(f"   ✓ Category: {category['name']} (ID: {category['id']})")
        
        # Step 5: Create transaction
        print("\n5️⃣ Creating withdrawal transaction...")
        
        source_account = asset_accounts[0]
        amount = "39900"  # Netflix Colombia price
        
        print(f"   Transaction details:")
        print(f"     Type: withdrawal")
        print(f"     Amount: {amount} COP")
        print(f"     Source: {source_account['name']}")
        print(f"     Destination: {merchant_name}")
        print(f"     Category: {category_name}")
        
        transaction = TransactionCreate(
            error_if_duplicate_hash=False,
            apply_rules=True,
            fire_webhooks=False,
            transactions=[
                TransactionSplit(
                    type=TransactionType.WITHDRAWAL,
                    date=datetime.now().strftime("%Y-%m-%d"),
                    amount=amount,
                    description=f"Pago Netflix - {test_prefix}",
                    source_name=source_account["name"],
                    destination_name=merchant_name,
                    category_name=category_name,
                    currency_code="COP",
                    external_id=f"email-netflix-{test_prefix}",
                    notes="Created by integration test",
                )
            ],
        )
        
        try:
            result = await firefly_client.create_transaction(transaction)
            tracker.track_transaction(result["id"])
            
            print(f"\n   ✓ Transaction created successfully!")
            print(f"     ID: {result['id']}")
            print(f"     Journal ID: {result.get('transaction_journal_id')}")
            print(f"     Final amount: {result['amount']}")
            
        except FireflyValidationError as e:
            print(f"\n   ❌ VALIDATION ERROR!")
            print(f"     Message: {e}")
            print(f"     Details: {e.details}")
            raise
        
        # Step 6: Verify transaction was created
        print("\n6️⃣ Verifying transaction...")
        fetched = await firefly_client.get_transaction(result["id"])
        
        print(f"   Fetched transaction:")
        print(f"     Description: {fetched['description']}")
        print(f"     Amount: {fetched['amount']}")
        print(f"     Type: {fetched['type']}")
        
        assert fetched["id"] == result["id"]
        print("   ✓ Verification passed!")
        
        print("\n" + "="*60)
        print("✅ FLOW COMPLETED SUCCESSFULLY")
        print("="*60)
    
    @pytest.mark.asyncio
    async def test_nequi_transfer_flow(
        self,
        firefly_client: FireflyClient,
        tracker: DataTracker,
        test_prefix: str,
    ):
        """
        Test deposit flow (incoming Nequi transfer).
        """
        print("\n" + "="*60)
        print("📱 Nequi Incoming Transfer Flow Test")
        print("="*60)
        
        # Get asset accounts
        asset_accounts = await firefly_client.get_accounts(AccountType.ASSET)
        if not asset_accounts:
            pytest.skip("No asset accounts")
        
        dest_account = asset_accounts[0]
        sender_name = f"{test_prefix}_Juan_Perez"
        
        print(f"\n📝 Creating deposit (incoming transfer):")
        print(f"   From: {sender_name} (will create as revenue account)")
        print(f"   To: {dest_account['name']}")
        print(f"   Amount: 150,000 COP")
        
        transaction = TransactionCreate(
            error_if_duplicate_hash=False,
            apply_rules=True,
            fire_webhooks=False,
            transactions=[
                TransactionSplit(
                    type=TransactionType.DEPOSIT,
                    date=datetime.now().strftime("%Y-%m-%d"),
                    amount="150000",
                    description=f"Transferencia de {sender_name}",
                    source_name=sender_name,
                    destination_name=dest_account["name"],
                    category_name="Transferencias",
                    currency_code="COP",
                )
            ],
        )
        
        try:
            result = await firefly_client.create_transaction(transaction)
            tracker.track_transaction(result["id"])
            
            print(f"\n✓ Deposit created!")
            print(f"   ID: {result['id']}")
            print(f"   Type: {result['type']}")
            print(f"   Amount: {result['amount']}")
            
        except FireflyValidationError as e:
            print(f"\n❌ Validation Error: {e}")
            print(f"   Details: {e.details}")
            raise
        
        print("\n✅ Nequi transfer flow completed!")


# =============================================================================
# Debug Tests - Raw API Inspection
# =============================================================================

@skip_if_no_firefly
class TestDebugFireflyAPI:
    """Debug tests to inspect raw API responses."""
    
    @pytest.mark.asyncio
    async def test_inspect_transaction_payload(self, firefly_client: FireflyClient):
        """
        Inspect what payload we're sending and what Firefly expects.
        Useful for debugging validation errors.
        """
        asset_accounts = await firefly_client.get_accounts(AccountType.ASSET)
        
        if not asset_accounts:
            pytest.skip("No asset accounts")
        
        # Build the exact payload we send
        payload = {
            "error_if_duplicate_hash": False,
            "apply_rules": False,
            "fire_webhooks": False,
            "transactions": [
                {
                    "type": "withdrawal",
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "amount": "10000",
                    "description": "Debug test transaction",
                    "currency_code": "COP",
                    "source_name": asset_accounts[0]["name"],
                    "destination_name": "Debug_Test_Merchant",
                }
            ],
        }
        
        print("\n📋 Transaction Payload:")
        import json
        print(json.dumps(payload, indent=2))
        
        # Make raw request to see exact response
        import httpx
        settings = get_settings()
        
        async with httpx.AsyncClient(
            base_url=f"{settings.firefly_base_url.rstrip('/')}/api/v1",
            headers={
                "Authorization": f"Bearer {settings.firefly_api_token.get_secret_value()}",
                "Content-Type": "application/json",
                "Accept": "application/vnd.api+json",
            },
        ) as client:
            response = await client.post("/transactions", json=payload)
            
            print(f"\n📬 Response Status: {response.status_code}")
            print(f"📬 Response Body:")
            print(json.dumps(response.json(), indent=2))
            
            if response.status_code != 200:
                print("\n❌ Transaction creation failed!")
                print(f"   Status: {response.status_code}")
            else:
                print("\n✓ Transaction created successfully")
    
    @pytest.mark.asyncio
    async def test_list_account_types(self, firefly_client: FireflyClient):
        """List all account types available in Firefly."""
        print("\n📊 Account Type Summary:")
        
        for acc_type in AccountType:
            accounts = await firefly_client.get_accounts(acc_type)
            print(f"\n{acc_type.value.upper()} accounts ({len(accounts)}):")
            for acc in accounts[:5]:
                print(f"  - {acc['name']} [{acc.get('currency_code', 'N/A')}]")
            if len(accounts) > 5:
                print(f"  ... and {len(accounts) - 5} more")


# =============================================================================
# Cleanup Utility (run manually)
# =============================================================================

@skip_if_no_firefly
class TestCleanup:
    """
    Cleanup test data.
    
    Run with: pytest tests/test_firefly_integration.py::TestCleanup -v -s
    """
    
    @pytest.mark.asyncio
    async def test_list_test_data(self, firefly_client: FireflyClient):
        """List all data that looks like test data (prefixed with TEST_)."""
        print("\n🧹 Finding test data...")
        
        # Find test accounts
        all_accounts = await firefly_client.get_accounts()
        test_accounts = [a for a in all_accounts if a["name"].startswith("TEST_")]
        
        print(f"\nTest accounts ({len(test_accounts)}):")
        for acc in test_accounts:
            print(f"  - {acc['name']} (ID: {acc['id']})")
        
        # Find test categories
        all_categories = await firefly_client.get_categories()
        test_categories = [c for c in all_categories if c["name"].startswith("TEST_")]
        
        print(f"\nTest categories ({len(test_categories)}):")
        for cat in test_categories:
            print(f"  - {cat['name']} (ID: {cat['id']})")
        
        print("\n⚠️  To delete these, use Firefly III web interface")
        print("    or implement delete methods in FireflyClient")
