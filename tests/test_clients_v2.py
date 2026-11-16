import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from app.clients.firefly import FireflyClient
from app.core.exceptions import FireflyError


def settings(**overrides):
    values = dict(
        firefly_base_url="https://firefly.test", firefly_api_token=SecretStr("firefly-secret"),
    )
    values.update(overrides)
    return SimpleNamespace(**values)






async def test_firefly_pagination_flattening_and_transaction_rules():
    client = FireflyClient(settings())
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer firefly-secret"
        if request.url.path.endswith("/accounts"):
            page = int(request.url.params.get("page", 1))
            return httpx.Response(200, json={"data": [{"id": str(page), "attributes": {"name": f"Banco {page}", "type": "asset"}}], "meta": {"pagination": {"total_pages": 2}}})
        if request.url.path.endswith("/categories"):
            return httpx.Response(200, json={"data": [{"id": "1", "attributes": {"name": "Comida"}}]})
        payload = json.loads(request.content)
        assert payload["apply_rules"] is True
        assert payload["transactions"][0]["external_id"] == "fa:mail:123:456"
        return httpx.Response(200, json={"data": {"id": "42"}})

    # Preserve base URL and auth while replacing only the transport.
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url="https://firefly.test/api/v1/", headers={"Authorization": "Bearer firefly-secret"}, transport=httpx.MockTransport(handler))
    try:
        assert await client.accounts() == [{"id": "1", "name": "Banco 1", "type": "asset"}, {"id": "2", "name": "Banco 2", "type": "asset"}]
        assert await client.categories() == ["Comida"]
        payload = {"apply_rules": False, "transactions": [{"external_id": "fa:mail:123:456"}]}
        assert await client.create_transaction(payload) == "42"
        assert payload["apply_rules"] is False
    finally:
        await client.close()


async def test_firefly_refuses_asset_creation():
    client = FireflyClient(settings())
    try:
        with pytest.raises(ValueError, match="merchant"):
            await client.create_account("Missing asset", "asset")
    finally:
        await client.close()


async def test_firefly_failures_propagate_for_durable_retry():
    client = FireflyClient(settings())
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url="https://firefly.test/api/v1/", transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    try:
        with pytest.raises(FireflyError, match="503"):
            await client.create_transaction({"transactions": []})
        assert not await client.check_connection()
    finally:
        await client.close()




async def test_firefly_creates_only_merchant_accounts():
    client = FireflyClient(settings())
    await client._client.aclose()

    def handler(request):
        assert request.url.path == "/api/v1/accounts"
        assert json.loads(request.content) == {"name": "TIENDA", "type": "expense", "currency_code": "COP"}
        return httpx.Response(200, json={"data": {"id": "7", "attributes": {"name": "TIENDA", "type": "expense"}}})

    client._client = httpx.AsyncClient(base_url="https://firefly.test/api/v1/", transport=httpx.MockTransport(handler))
    try:
        assert await client.create_account("TIENDA", "expense") == {"id": "7", "name": "TIENDA", "type": "expense"}
    finally:
        await client.close()


