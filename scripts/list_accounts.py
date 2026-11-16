#!/usr/bin/env python3
"""List Firefly accounts with the same credentials as the server."""
import asyncio

from app.clients.firefly import FireflyClient


async def main():
    client = FireflyClient()
    try:
        for account in await client.accounts():
            print(f"{account['type']}: {account['name']}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
