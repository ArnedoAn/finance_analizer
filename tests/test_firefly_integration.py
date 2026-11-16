"""Read-only live Firefly check; opt in explicitly, never use local .env secrets."""

import os

import pytest

from app.clients.firefly import FireflyClient
from app.core.config import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_FIREFLY_INTEGRATION") != "1"
        or not os.getenv("FIREFLY_BASE_URL")
        or not os.getenv("FIREFLY_API_TOKEN"),
        reason="Set RUN_FIREFLY_INTEGRATION=1 and explicit Firefly credentials",
    ),
]


async def test_live_firefly_read_only():
    config = Settings(
        finance_api_key="test-key",
        firefly_base_url=os.environ["FIREFLY_BASE_URL"],
        firefly_api_token=os.environ["FIREFLY_API_TOKEN"],
        _env_file=None,
    )
    client = FireflyClient(config)
    try:
        assert await client.check_connection()
        assert isinstance(await client.accounts(), list)
        assert isinstance(await client.categories(), list)
    finally:
        await client.close()
