import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from app.clients.firefly import FireflyClient
from app.clients.imap import IMAPClient
from app.clients.llm import LLMClient


def settings(**overrides):
    values = dict(
        firefly_base_url="https://firefly.test", firefly_api_token=SecretStr("firefly-secret"),
        llm_base_url="https://llm.test/v1", llm_api_key=SecretStr("primary-secret"),
        llm_models="cheap-a,cheap-b", llm_user_agent="finance-analyzer/2.0",
        llm_fallback_base_url="https://fallback.test/v1", llm_fallback_api_key=SecretStr("fallback-secret"),
        llm_fallback_model="fallback", email_max_body_chars=12000,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


async def mock_http(client, handler):
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def completion(value):
    return httpx.Response(200, json={"choices": [{"message": {"content": value}}]})


async def test_llm_order_json_validation_and_session_headers():
    client = LLMClient(settings())
    requests = []

    def handler(request):
        requests.append(request)
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        assert request.headers["x-opencode-session"] == "cycle-123"
        assert request.headers["user-agent"] == "finance-analyzer/2.0"
        if payload["model"] == "cheap-a":
            return completion('{"amount":-2}')
        if payload["model"] == "cheap-b":
            return completion("not json")
        assert request.headers["authorization"] == "Bearer fallback-secret"
        return completion('```json\n{"amount":12500,"merchant":"TIENDA","category":"Compras","asset_account":"Banco"}\n```')

    await mock_http(client, handler)
    try:
        result = await client.analyze("Compra", ["Compras"], ["Banco"], "cycle-123")
        assert result.amount == Decimal("12500")
        assert [json.loads(request.content)["model"] for request in requests] == ["cheap-a", "cheap-b", "fallback"]
        assert requests[-1].url == "https://fallback.test/v1/chat/completions"
    finally:
        await client.close()


async def test_llm_http_failure_and_unknown_categories_fall_through():
    client = LLMClient(settings())
    models = []

    def handler(request):
        model = json.loads(request.content)["model"]
        models.append(model)
        if model == "cheap-a":
            return httpx.Response(429)
        if model == "cheap-b":
            return completion('{"amount":10,"category":"Inventada"}')
        return completion('{"is_transaction":false}')

    await mock_http(client, handler)
    try:
        assert not (await client.analyze("texto", [], [], "cycle")).is_transaction
        assert models == ["cheap-a", "cheap-b", "fallback"]
    finally:
        await client.close()


async def test_llm_exhaustion_does_not_leak_provider_body():
    client = LLMClient(settings())
    await mock_http(client, lambda request: httpx.Response(503, text="sensitive provider text"))
    try:
        with pytest.raises(RuntimeError) as caught:
            await client.analyze("private message", [], [], "cycle")
        assert "sensitive" not in str(caught.value)
        assert "primary-secret" not in str(caught.value)
    finally:
        await client.close()


async def test_sender_learning_rejects_hallucinations():
    client = LLMClient(settings())
    models = []

    def handler(request):
        model = json.loads(request.content)["model"]
        models.append(model)
        return completion('{"senders":["invented"]}' if model == "cheap-a" else '{"senders":["Banco", "banco"]}')

    await mock_http(client, handler)
    try:
        assert await client.learn_senders(["Banco <alertas@banco.test>"], "cycle") == ["banco"]
        assert models == ["cheap-a", "cheap-b"]
    finally:
        await client.close()


async def test_health_uses_models_without_inference():
    client = LLMClient(settings())
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    await mock_http(client, handler)
    try:
        assert await client.check_connection()
        assert requests[0].url.path == "/v1/models"
        assert requests[0].method == "GET"
    finally:
        await client.close()


async def test_unconfigured_llm_is_unhealthy_without_network():
    client = LLMClient(settings(llm_api_key=SecretStr(""), llm_fallback_api_key=SecretStr("")))
    await mock_http(client, lambda request: pytest.fail("Network must not be used"))
    try:
        assert not await client.check_connection()
        with pytest.raises(RuntimeError, match="no configured models"):
            await client.analyze("text", [], [], "cycle")
    finally:
        await client.close()


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
        with pytest.raises(httpx.HTTPStatusError):
            await client.create_transaction({"transactions": []})
        assert not await client.check_connection()
    finally:
        await client.close()


class FakeIMAP:
    def __init__(self, host, port, timeout):
        self.commands = []
        self.logged_out = False
        self.validity = b"123"
        self.search_result = b"8 9"

    def login(self, username, password):
        self.commands.append(("login", username, password))
        return "OK", []

    def select(self, folder, readonly):
        assert readonly is True
        self.commands.append(("select", folder, readonly))
        return "OK", []

    def response(self, name):
        assert name == "UIDVALIDITY"
        return name, [self.validity]

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == "search":
            return "OK", [self.search_result]
        assert args[1] == "(BODY.PEEK[] INTERNALDATE)"
        raw = (b"From: =?utf-8?q?Banco?= <bank@test.com>\r\n"
               b"Subject: =?utf-8?q?Compra_aprobada?=\r\n"
               b"Date: Mon, 16 Nov 2026 08:00:00 -0500\r\n"
               b"Content-Type: text/plain; charset=utf-8\r\n\r\nCompraste $50.000 en TIENDA")
        return "OK", [(b'9 (INTERNALDATE "16-Nov-2026 13:00:00 +0000")', raw), b")"]

    def logout(self):
        self.logged_out = True


@pytest.fixture
def mail(monkeypatch):
    monkeypatch.setattr("app.clients.imap.get_settings", lambda: settings())
    connection = FakeIMAP("", 993, 30)
    monkeypatch.setattr("app.clients.imap.imaplib.IMAP4_SSL", lambda *args, **kwargs: connection)
    account = SimpleNamespace(name="personal", host="", provider="gmail", port=993, username="owner@test.com", password=SecretStr("app-password"), folder="INBOX", ssl=True)
    return IMAPClient(account), connection


async def test_imap_readonly_uid_resume_and_decoded_message(mail):
    client, connection = mail
    validity, highwater, messages = await client.poll("123", 8, 7)
    assert validity == "123"
    assert highwater == 9
    assert len(messages) == 1
    assert messages[0]["uid"] == 9
    assert messages[0]["subject"] == "Compra aprobada"
    assert messages[0]["occurred_at"] == datetime(2026, 11, 16, 13, tzinfo=UTC)
    assert ("search", None, "UID 9:*") in connection.commands
    assert connection.logged_out


async def test_imap_uidvalidity_reset_uses_lookback_and_all_highwater(mail):
    client, connection = mail
    validity, highwater, messages = await client.poll("old", 999, 7)
    assert validity == "123"
    assert highwater == 9
    assert len(messages) == 2
    assert any(command[0] == "search" and command[2].startswith("SINCE ") for command in connection.commands)


async def test_imap_empty_poll_preserves_cursor(mail):
    client, connection = mail
    connection.search_result = b"8"
    assert await client.poll("123", 8, 7) == ("123", 8, [])


async def test_imap_fetch_failure_does_not_advance_cursor(mail, monkeypatch):
    client, connection = mail
    original = connection.uid
    monkeypatch.setattr(connection, "uid", lambda command, *args: ("NO", []) if command == "fetch" else original(command, *args))
    with pytest.raises(RuntimeError, match="fetch failed"):
        await client.poll("123", 8, 7)
    assert connection.logged_out


async def test_imap_connection_test(mail):
    client, connection = mail
    assert await client.test() == {"ok": True, "error": None}
    assert connection.logged_out


def test_imap_html_fallback_ignores_attachments_and_truncates(mail):
    from email import policy
    from email.parser import BytesParser

    client, _ = mail
    raw = (b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="x"\r\n\r\n'
           b'--x\r\nContent-Type: text/html\r\n\r\n<p>Hola <b>banco</b></p><script>secret</script>\r\n'
           b'--x\r\nContent-Type: text/plain\r\nContent-Disposition: attachment\r\n\r\nattachment\r\n--x--')
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert client._body(message) == "Hola banco"
    client.max_body_chars = 4
    assert client._body(message) == "Hola"


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


async def test_llm_rejects_unknown_assets_and_invented_currency():
    client = LLMClient(settings())
    models = []

    def handler(request):
        model = json.loads(request.content)["model"]
        models.append(model)
        if model == "cheap-a":
            return completion('{"amount":10,"asset_account":"Imaginary"}')
        if model == "cheap-b":
            return completion('{"amount":10,"currency":"EUR"}')
        return completion('{"amount":10,"currency":"COP"}')

    await mock_http(client, handler)
    try:
        assert (await client.analyze("Pagaste $10", [], ["Banco"], "cycle")).currency == "COP"
        assert models == ["cheap-a", "cheap-b", "fallback"]
    finally:
        await client.close()


def test_imap_prefers_plain_text_and_falls_back_to_internal_date(mail):
    from email import policy
    from email.parser import BytesParser

    client, _ = mail
    raw = (b'MIME-Version: 1.0\r\nDate: invalid\r\nContent-Type: multipart/alternative; boundary="x"\r\n\r\n'
           b'--x\r\nContent-Type: text/plain\r\n\r\nTexto plano\r\n'
           b'--x\r\nContent-Type: text/html\r\n\r\n<p>Texto HTML</p>\r\n--x--')
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert client._body(message) == "Texto plano"
    assert client._date(message, b'INTERNALDATE "16-Nov-2026 13:00:00 +0000"') == datetime(2026, 11, 16, 13, tzinfo=UTC)


async def test_imap_non_ssl_custom_host_and_login_failure_cleanup(mail, monkeypatch):
    client, connection = mail
    client.account.ssl = False
    client.account.provider = "custom"
    client.account.host = "imap.private.test"
    calls = []

    def connect(host, port, timeout):
        calls.append((host, port, timeout))
        return connection

    def failed_login(*args):
        raise RuntimeError("login failed app-password")

    monkeypatch.setattr("app.clients.imap.imaplib.IMAP4", connect)
    monkeypatch.setattr(connection, "login", failed_login)
    result = await client.test()
    assert calls == [("imap.private.test", 993, 30)]
    assert result == {"ok": False, "error": "login failed ***"}
    assert connection.logged_out


async def test_llm_cop_response_accepts_usd_in_informative_footer():
    client = LLMClient(settings())
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return completion('{"amount":20000,"currency":"COP","merchant":"DIDI"}')

    await mock_http(client, handler)
    try:
        result = await client.analyze("Compra $20.000 en DIDI. Consulta tarifas USD en nuestra web.", [], [], "cycle")
        assert result.currency == "COP"
        assert len(calls) == 1
    finally:
        await client.close()


def test_llm_text_is_limited_to_four_thousand_characters():
    from app.clients.llm import _compact

    assert _compact("x" * 12000) == "x" * 4000
