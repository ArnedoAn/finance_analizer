#!/usr/bin/env python3
"""List all Firefly III accounts."""

import asyncio
import sys
sys.path.insert(0, ".")

import logging
logging.disable(logging.CRITICAL)  # Disable all logging

from app.clients.firefly import FireflyClient
from app.models.schemas import AccountType


async def main():
    client = FireflyClient()
    
    try:
        print("", flush=True)
        print("=== Asset accounts (your bank accounts) ===", flush=True)
        accounts = await client.get_accounts(AccountType.ASSET)
        print(f"Found {len(accounts)} asset accounts", flush=True)
        if accounts:
            for acc in accounts:
                print(f"  - {acc['name']}", flush=True)
        else:
            print("  (none)", flush=True)
        
        print("", flush=True)
        print("=== Expense accounts (merchants) ===", flush=True)
        accounts = await client.get_accounts(AccountType.EXPENSE)
        print(f"Found {len(accounts)} expense accounts", flush=True)
        if accounts:
            for acc in accounts:
                print(f"  - {acc['name']}", flush=True)
        else:
            print("  (none)", flush=True)
            
    finally:
        await client.close()
    
    print("\nDone!", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
