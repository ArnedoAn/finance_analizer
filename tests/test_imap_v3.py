import ssl
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.clients.imap import IMAPClient


class FakeIMAP:
    def __init__(self):
        self.commands = []
        self.validity = b"123"
        self.uids = b"8 9"
        self.fail_uid = None
        self.failure = None
        self.logged_out = False

    def login(self, username, password):
        if self.failure:
            raise RuntimeError(self.failure)

    def select(self, folder, readonly):
        assert readonly
        return "OK", []

    def response(self, name):
        return name, [self.validity]

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == "search":
            return "OK", [self.uids]
        if args[0] == self.fail_uid:
            raise RuntimeError("bad owner@test.com app-password")
        raw = (b'From: =?utf-8?q?Bancolombia?= <alert@bank.test>\r\n'
               b'Subject: =?utf-8?q?Compra_aprobada?=\r\n'
               b'Message-ID: <ABC@BANK.TEST>\r\n'
               b'Date: Mon, 16 Nov 2026 08:00:00 -0500\r\n'
               b'Authentication-Results: mx.google.com; dkim=pass header.d=bank.test\r\n'
               b'Content-Type: text/plain; charset=utf-8\r\n\r\nCompraste $50.000 en TIENDA')
        return "OK", [(b'9 (INTERNALDATE "16-Nov-2026 13:00:00 +0000")', raw), b')']

    def logout(self):
        self.logged_out = True


@pytest.fixture
def mail(monkeypatch):
    monkeypatch.setattr("app.clients.imap.get_settings", lambda: SimpleNamespace(email_max_body_chars=12000))
    connection = FakeIMAP()
    def connect(host, port, timeout, ssl_context):
        assert host == "imap.gmail.com" and port == 993 and timeout == 30
        assert ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert ssl_context.check_hostname is True
        return connection
    monkeypatch.setattr("app.clients.imap.imaplib.IMAP4_SSL", connect)
    account = SimpleNamespace(host="", provider="gmail", port=993, username="owner@test.com",
                              password=SecretStr("app-password"), folder="INBOX", ssl=True)
    return IMAPClient(account), connection


async def test_headers_first_body_selective_and_normalized_id(mail):
    client, connection = mail
    seen = []
    def want_body(header):
        seen.append(header)
        return header.uid == 9
    validity, cursor, messages = await client.poll(None, 0, 7, want_body)
    assert (validity, cursor) == (123, 9)
    assert messages[0].body is None
    assert messages[1].body == "Compraste $50.000 en TIENDA"
    assert seen[0].message_id == "abc@bank.test"
    assert seen[0].from_address == "alert@bank.test"
    assert seen[0].from_display == "Bancolombia"
    assert seen[0].subject == "Compra aprobada"
    assert seen[0].auth.trusted
    assert seen[0].occurred_at == datetime(2026, 11, 16, 13, tzinfo=UTC)
    fetches = [c for c in connection.commands if c[0] == "fetch"]
    assert "HEADER.FIELDS" in fetches[0][2]
    assert fetches[-1] == ("fetch", "9", "(BODY.PEEK[])")
    assert len(fetches) == 3
    assert connection.logged_out


async def test_resume_and_uidvalidity_reset(mail):
    client, connection = mail
    assert (await client.poll(123, 9, 7, lambda h: False)) == (123, 9, [])
    assert ("search", None, "UID 10:*") in connection.commands
    validity, cursor, messages = await client.poll(999, 999, 7, lambda h: False)
    assert (validity, cursor, len(messages)) == (123, 9, 2)
    assert connection.commands[-3][2].startswith("SINCE ")


async def test_poison_message_isolated_cursor_advances(mail):
    client, connection = mail
    connection.fail_uid = "8"
    _, cursor, messages = await client.poll(None, 0, 7, lambda h: True)
    assert cursor == 9
    assert messages[0].error == "bad *** ***"
    assert messages[1].error is None
    assert messages[1].body


async def test_body_failure_preserves_header(mail):
    client, connection = mail
    def broken(header):
        raise ValueError("owner@test.com app-password")
    _, cursor, messages = await client.poll(None, 0, 7, broken)
    assert cursor == 9 and messages[0].message_id == "abc@bank.test"
    assert messages[0].error == "*** ***"


async def test_connection_errors_redacted_everywhere(mail):
    client, connection = mail
    connection.failure = "owner@test.com app-password"
    assert await client.test() == {"ok": False, "error": "*** ***"}
    with pytest.raises(RuntimeError, match=r"^\*\*\* \*\*\*$"):
        await client.poll(None, 0, 7, lambda h: True)
    assert connection.logged_out


async def test_cleartext_is_rejected(mail):
    client, _ = mail
    client.account.ssl = False
    assert (await client.test())["ok"] is False


def test_invalid_date_is_none_and_internaldate_fallback(mail):
    client, _ = mail
    message = BytesParser(policy=policy.default).parsebytes(b"Date: invalid\r\n\r\n")
    assert client._date(message, b"") is None
    assert client._date(message, b'INTERNALDATE "16-Nov-2026 13:00:00 +0000"') == datetime(2026, 11, 16, 13, tzinfo=UTC)


def test_body_filters_html_attachments_and_limits(mail):
    client, _ = mail
    raw = (b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="x"\r\n\r\n'
           b'--x\r\nContent-Type: text/html\r\n\r\n<p>Hola <b>banco</b></p><script>secret</script>\r\n'
           b'--x\r\nContent-Type: text/plain\r\nContent-Disposition: attachment\r\n\r\nattachment\r\n--x--')
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert client._body(message) == "Hola banco"
    client.max_body_chars = 4
    assert client._body(message) == "Hola"
