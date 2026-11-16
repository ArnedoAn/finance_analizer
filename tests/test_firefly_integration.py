"""Read-only live Firefly check; opt in explicitly, never use local .env secrets."""

import os

import httpx
import pytest

from app.clients.firefly import FireflyClient
from app.core.config import Settings
from app.core.exceptions import FireflyError, PermanentError


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("RUN_FIREFLY_INTEGRATION") != "1"
    or not os.getenv("FIREFLY_BASE_URL")
    or not os.getenv("FIREFLY_API_TOKEN"),
    reason="Set RUN_FIREFLY_INTEGRATION=1 and explicit Firefly credentials",
)
async def test_live_firefly_read_only():
    config = Settings(
        finance_api_key="test-admin-key-01234567890123456789",
        phone_api_key="test-phone-key-01234567890123456789",
        firefly_base_url=os.environ["FIREFLY_BASE_URL"],
        firefly_api_token=os.environ["FIREFLY_API_TOKEN"],
        _env_file=None,
    )
    client = FireflyClient(config)
    try:
        assert await client.check_connection()
        assert isinstance(await client.accounts(), list)
        assert isinstance(await client.categories(), list)
        from uuid import uuid4
        assert await client.find_by_external_id("fa:integration-probe:" + uuid4().hex) is None
        assert isinstance(await client.list_auto_transactions(), list)
    finally:
        await client.close()


@pytest.fixture
def firefly_http(settings):
    settings.firefly_base_url = "https://firefly.invalid"
    return FireflyClient(settings)


async def test_find_external_id_checks_exact_split_and_returns_id(firefly_http):
    client = firefly_http
    requests = []

    def respond(request):
        requests.append(request)
        page = int(request.url.params["page"])
        external_id = "fa:mail:42" if page == 2 else "fa:mail:42-extra"
        return httpx.Response(200, json={"data": [{"id": str(page), "attributes": {"transactions": [{"external_id": external_id}]}}], "meta": {"pagination": {"total_pages": 2}}})

    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url="https://firefly.invalid/api/v1/", transport=httpx.MockTransport(respond))
    try:
        assert await client.find_by_external_id("fa:mail:42") == "2"
        assert len(requests) == 2
        assert requests[0].url.params["query"] == 'external_id_is:"fa:mail:42"'
    finally:
        await client.close()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_firefly_4xx_is_permanent(firefly_http, status):
    client = firefly_http
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url="https://firefly.invalid/api/v1/", transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"message": "invalid account"})))
    try:
        with pytest.raises(PermanentError):
            await client.create_transaction({"transactions": []})
    finally:
        await client.close()


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_firefly_retryable_status_is_not_permanent(firefly_http, status):
    client = firefly_http
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url="https://firefly.invalid/api/v1/", transport=httpx.MockTransport(lambda request: httpx.Response(status)))
    try:
        with pytest.raises(FireflyError) as failure:
            await client.create_transaction({"transactions": []})
        assert not isinstance(failure.value, PermanentError)
    finally:
        await client.close()


async def test_firefly_create_update_delete_contract(firefly_http):
    client = firefly_http
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(204) if request.method == "DELETE" else httpx.Response(200, json={"data": {"id": "99"}})

    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url="https://firefly.invalid/api/v1/", transport=httpx.MockTransport(respond))
    try:
        assert await client.create_transaction({"transactions": []}) == "99"
        assert await client.update_transaction("99", {"transactions": []}) == "99"
        assert await client.delete_transaction("99") is None
        assert [(request.method, request.url.path) for request in requests] == [
            ("POST", "/api/v1/transactions"), ("PUT", "/api/v1/transactions/99"), ("DELETE", "/api/v1/transactions/99"),
        ]
    finally:
        await client.close()
