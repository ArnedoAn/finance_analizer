"""
Debug script to test Firefly III transaction creation directly.

Run with: python scripts/debug_firefly.py
"""

import asyncio
import json
from datetime import datetime
from decimal import Decimal

import httpx

from app.core.config import get_settings
from app.models.schemas import (
    TransactionAnalysis,
    TransactionCreate,
    TransactionSplit,
    TransactionType,
)


async def test_direct_api():
    """Test creating a transaction directly to Firefly III API."""
    settings = get_settings()
    
    payload = {
        "error_if_duplicate_hash": False,
        "apply_rules": False,
        "fire_webhooks": False,
        "transactions": [
            {
                "type": "withdrawal",
                "date": datetime.now().strftime("%Y-%m-%d"),
                "amount": "15000",
                "description": "Debug test transaction - direct API",
                "currency_code": "COP",
                "source_name": "Bancolombia",
                "destination_name": "Netflix Debug Test",
                "category_name": "Test",
            }
        ],
    }
    
    print("=" * 60)
    print("1. Direct API Test")
    print("=" * 60)
    print(f"\nFirefly URL: {settings.firefly_base_url}")
    print(f"\nPayload:")
    print(json.dumps(payload, indent=2))
    print()
    
    base_url = settings.firefly_base_url.rstrip("/") + "/api/v1"
    
    async with httpx.AsyncClient(
        base_url=base_url,
        headers={
            "Authorization": f"Bearer {settings.firefly_api_token.get_secret_value()}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.api+json",
        },
        timeout=30.0,
    ) as client:
        # Check connection
        print("Checking connection...")
        about_response = await client.get("/about")
        if about_response.status_code == 200:
            print(f"✓ Connected to Firefly III v{about_response.json().get('data', {}).get('version', 'unknown')}")
        else:
            print(f"✗ Connection failed: {about_response.status_code}")
            return
        
        # Create transaction
        print("\nCreating test transaction...")
        response = await client.post("/transactions", json=payload)
        
        print(f"\nResponse Status: {response.status_code}")
        if response.status_code == 200:
            print("✅ SUCCESS: Transaction created!")
        else:
            print("❌ FAILED: Transaction creation failed")
            print(f"Response: {json.dumps(response.json(), indent=2)}")


async def test_via_firefly_client():
    """Test using our FireflyClient."""
    from app.clients.firefly import FireflyClient
    
    print("\n" + "=" * 60)
    print("2. FireflyClient Test")
    print("=" * 60)
    
    client = FireflyClient()
    
    try:
        # Check connection
        print("\nChecking connection via client...")
        connected = await client.check_connection()
        print(f"✓ Connected: {connected}")
        
        # Create transaction via client
        print("\nCreating transaction via FireflyClient...")
        
        transaction = TransactionCreate(
            error_if_duplicate_hash=False,
            apply_rules=False,
            fire_webhooks=False,
            transactions=[
                TransactionSplit(
                    type=TransactionType.WITHDRAWAL,
                    date=datetime.now().strftime("%Y-%m-%d"),
                    amount="20000",
                    description="Debug test via FireflyClient",
                    source_name="Bancolombia",
                    destination_name="Test Store Client",
                    category_name="Test",
                    currency_code="COP",
                )
            ],
        )
        
        result = await client.create_transaction(transaction)
        print(f"✅ SUCCESS: Transaction created!")
        print(f"   ID: {result['id']}")
        print(f"   Amount: {result['amount']}")
        
    except Exception as e:
        print(f"❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await client.close()


async def test_via_transaction_service():
    """Test using the full TransactionService (simulating AI output)."""
    from app.clients.firefly import FireflyClient
    from app.db.database import get_db
    from app.services.sync_service import SyncService
    from app.services.transaction_service import TransactionService
    
    print("\n" + "=" * 60)
    print("3. TransactionService Test (simulating AI analysis)")
    print("=" * 60)
    
    firefly_client = FireflyClient()
    
    try:
        # Get a database session
        async for session in get_db():
            sync_service = SyncService(session, firefly_client)
            tx_service = TransactionService(session, firefly_client, sync_service)
            
            # Simulate what the AI would return
            fake_analysis = TransactionAnalysis(
                amount=Decimal("39900"),
                currency="COP",
                date=datetime.now(),
                description="Pago Netflix Colombia - Simulado",
                merchant="Netflix",
                suggested_category="Suscripciones",
                suggested_account_name="Bancolombia",  # This should exist!
                transaction_type=TransactionType.WITHDRAWAL,
                confidence_score=0.95,
            )
            
            print("\nSimulated AI Analysis:")
            print(f"  Amount: {fake_analysis.amount}")
            print(f"  Merchant: {fake_analysis.merchant}")
            print(f"  Account: {fake_analysis.suggested_account_name}")
            print(f"  Type: {fake_analysis.transaction_type}")
            
            print("\nCreating transaction via TransactionService...")
            
            # First, sync to get accounts
            print("  - Syncing accounts from Firefly...")
            await sync_service.sync_all()
            
            # Create with dry_run first
            print("  - Testing dry run...")
            dry_result = await tx_service.create_from_analysis(
                fake_analysis,
                external_id="debug-test-123",
                dry_run=True,
            )
            print(f"  ✓ Dry run successful")
            print(f"    Source: {dry_result.get('source_name')}")
            print(f"    Destination: {dry_result.get('destination_name')}")
            
            # Now create for real
            print("\n  - Creating real transaction...")
            result = await tx_service.create_from_analysis(
                fake_analysis,
                external_id=f"debug-test-{datetime.now().timestamp()}",
                dry_run=False,
            )
            
            print(f"\n✅ SUCCESS: Transaction created!")
            print(f"   ID: {result['id']}")
            print(f"   Amount: {result.get('amount')}")
            
            break  # Only need one iteration
            
    except Exception as e:
        print(f"❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await firefly_client.close()


async def test_with_bad_account_name():
    """Test with the exact AI output that was failing."""
    from app.clients.firefly import FireflyClient
    from app.db.database import get_db
    from app.services.sync_service import SyncService
    from app.services.transaction_service import TransactionService
    
    print("\n" + "=" * 60)
    print("4. Test with bad account name (from AI)")
    print("=" * 60)
    
    firefly_client = FireflyClient()
    
    try:
        # Get a database session
        async for session in get_db():
            sync_service = SyncService(session, firefly_client)
            tx_service = TransactionService(session, firefly_client, sync_service)
            
            # Simulate the EXACT data that was failing
            fake_analysis = TransactionAnalysis(
                amount=Decimal("12900"),
                currency="COP",
                date=datetime.now(),
                description="Compra en DLO*Didi",
                merchant="DLO*Didi",
                suggested_category="Transporte",
                # THIS IS THE PROBLEM - AI suggests a non-existent account name
                suggested_account_name="Tarjeta débito 7556 Lulo bank",
                transaction_type=TransactionType.WITHDRAWAL,
                confidence_score=0.90,
            )
            
            print("\nSimulated AI Analysis (with bad account name):")
            print(f"  Amount: {fake_analysis.amount}")
            print(f"  Merchant: {fake_analysis.merchant}")
            print(f"  Account: {fake_analysis.suggested_account_name}")
            print(f"  Type: {fake_analysis.transaction_type}")
            
            print("\nThis should now fallback to the default account...")
            
            # First, sync to get accounts
            print("  - Syncing accounts from Firefly...")
            await sync_service.sync_all()
            
            # Create with dry_run first
            print("  - Testing dry run...")
            dry_result = await tx_service.create_from_analysis(
                fake_analysis,
                external_id="debug-bad-account-test",
                dry_run=True,
            )
            print(f"  ✓ Dry run successful!")
            print(f"    Source: {dry_result.get('source_name')} (should be Bancolombia)")
            print(f"    Destination: {dry_result.get('destination_name')}")
            
            # Now create for real
            print("\n  - Creating real transaction...")
            result = await tx_service.create_from_analysis(
                fake_analysis,
                external_id=f"debug-bad-account-{datetime.now().timestamp()}",
                dry_run=False,
            )
            
            print(f"\n✅ SUCCESS: Transaction created!")
            print(f"   ID: {result['id']}")
            print(f"   Source used: {result.get('source_name')}")
            
            break  # Only need one iteration
            
    except Exception as e:
        print(f"❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await firefly_client.close()


async def main():
    print("\n🔍 FIREFLY III DEBUG SCRIPT")
    print("=" * 60)
    
    # Test 1: Direct API
    await test_direct_api()
    
    # Test 2: Via FireflyClient
    await test_via_firefly_client()
    
    # Test 3: Via TransactionService
    await test_via_transaction_service()
    
    # Test 4: With bad account name
    await test_with_bad_account_name()
    
    print("\n" + "=" * 60)
    print("Debug complete!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
