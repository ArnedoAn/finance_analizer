"""Read-only, verified-TLS IMAP ingestion with per-message failure isolation."""
import asyncio
import imaplib
import re
import ssl
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from urllib.parse import quote

from bs4 import BeautifulSoup

from app.core.config import get_settings
from app.security.sender_trust import PROVIDER_AUTHSERV_IDS, AuthResult, authentication_result

_PROVIDER_HOSTS = {"gmail": "imap.gmail.com", "yahoo": "imap.mail.yahoo.com", "icloud": "imap.mail.me.com"}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_HEADER_QUERY = "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID AUTHENTICATION-RESULTS ARC-AUTHENTICATION-RESULTS)] INTERNALDATE)"


def _imap_date(value: date) -> str:
    return f"{value.day:02d}-{_MONTHS[value.month - 1]}-{value.year}"


@dataclass(frozen=True)
class MailHeader:
    uid: int
    message_id: str | None = None
    from_address: str = ""
    from_display: str = ""
    subject: str = ""
    occurred_at: datetime | None = None
    auth: AuthResult = AuthResult()


@dataclass(frozen=True)
class MailMessage(MailHeader):
    body: str | None = None
    error: str | None = None


class IMAPClient:
    def __init__(self, account) -> None:
        self.account = account
        self.max_body_chars = get_settings().email_max_body_chars

    def _safe_error(self, exc: Exception) -> str:
        error = str(exc)
        secrets = (self.account.username, self.account.password.get_secret_value())
        for secret in sorted((v for v in secrets if v), key=len, reverse=True):
            for value in {secret, quote(secret, safe=""), repr(secret)[1:-1]}:
                error = error.replace(value, "***")
        return error[:500]

    def _connect(self, history: bool = False):
        account = self.account
        host = account.host or _PROVIDER_HOSTS.get(account.provider, "")
        if not host:
            raise ValueError("An IMAP host is required for a custom provider")
        if not account.ssl:
            raise ValueError("IMAP requires verified TLS (ssl=true)")
        connection = imaplib.IMAP4_SSL(host, account.port, timeout=30, ssl_context=ssl.create_default_context())
        try:
            connection.login(account.username, account.password.get_secret_value())
            folder = (self._all_mail_folder(connection) if history else None) or account.folder
            status, _ = connection.select(folder, readonly=True)
            if status != "OK":
                raise RuntimeError("Cannot examine IMAP folder")
            return connection
        except Exception:
            self._logout(connection)
            raise

    @staticmethod
    def _logout(connection) -> None:
        try:
            connection.logout()
        except Exception:
            pass

    async def test(self) -> dict:
        try:
            await asyncio.to_thread(self._test)
            return {"ok": True, "error": None}
        except Exception as exc:
            return {"ok": False, "error": self._safe_error(exc)}

    def _test(self) -> None:
        self._logout(self._connect())

    async def poll(self, uidvalidity: int | None, last_uid: int, lookback_days: int,
                   want_body: Callable[[MailHeader], bool]) -> tuple[int, int, list[MailMessage]]:
        try:
            return await asyncio.to_thread(self._poll, uidvalidity, last_uid, lookback_days, want_body)
        except Exception as exc:
            raise RuntimeError(self._safe_error(exc)) from None

    @staticmethod
    def _fetch(connection, uid, query):
        status, parts = connection.uid("fetch", str(uid), query)
        if status != "OK":
            raise RuntimeError(f"IMAP fetch failed for UID {uid}")
        fetched = next((part for part in parts or [] if isinstance(part, tuple) and isinstance(part[1], bytes)), None)
        if fetched is None:
            raise RuntimeError(f"IMAP fetch omitted content for UID {uid}")
        return fetched[0], BytesParser(policy=policy.default).parsebytes(fetched[1])

    def _poll(self, uidvalidity, last_uid, lookback_days, want_body):
        connection = self._connect()
        try:
            validity = self._validity(connection)
            continuing = validity == uidvalidity and last_uid > 0
            since = datetime.now(UTC) - timedelta(days=lookback_days)
            criteria = f"UID {last_uid + 1}:*" if continuing else f"SINCE {_imap_date(since)}"
            uids = self._search(connection, criteria)
            if continuing:
                uids = [uid for uid in uids if uid > last_uid]
            messages = self._read(connection, uids, want_body)
            highwater = uids[-1] if uids else (last_uid if continuing else 0)
            return validity, highwater, messages
        finally:
            self._logout(connection)

    async def history(self, since: date, until: date, senders: list[str],
                      want_body: Callable[[MailHeader], bool]) -> tuple[int, list[MailMessage]]:
        """Messages from ``senders`` (exact addresses or domains) between two dates, both inclusive.

        Read-only and cursor-free: the live poll's UID highwater is never touched. Gmail is read from
        its All Mail folder (special-use ``\\All``) so archived alerts are found; other providers use
        the configured folder. The server matches FROM by substring and its dates are coarse, so the
        caller re-checks trust and the exact date of every message.
        """
        try:
            return await asyncio.to_thread(self._history, since, until, senders, want_body)
        except Exception as exc:
            raise RuntimeError(self._safe_error(exc)) from None

    def _history(self, since, until, senders, want_body):
        if not senders:
            return 0, []
        connection = self._connect(history=True)
        try:
            validity = self._validity(connection)
            # One spare day on each side: IMAP compares dates in the server's own zone.
            window = f"SINCE {_imap_date(since - timedelta(days=1))} BEFORE {_imap_date(until + timedelta(days=2))}"
            senders_query = "OR " * (len(senders) - 1) + " ".join(f'FROM "{sender}"' for sender in senders)
            return validity, self._read(connection, self._search(connection, f"{window} {senders_query}"), want_body)
        finally:
            self._logout(connection)

    def _all_mail_folder(self, connection) -> str | None:
        if self.account.provider != "gmail":
            return None
        status, rows = connection.list()
        if status != "OK":
            return None
        for row in rows or []:
            line = row.decode("utf-8", errors="replace") if isinstance(row, bytes) else str(row)
            match = re.match(r'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$', line)
            if match and "\\all" in match.group("flags").casefold().split():
                return match.group("name").strip()
        return None

    @staticmethod
    def _validity(connection) -> int:
        _, data = connection.response("UIDVALIDITY")
        if not data or not data[0]:
            raise RuntimeError("IMAP server did not provide UIDVALIDITY")
        return int(data[0])

    @staticmethod
    def _search(connection, criteria: str) -> list[int]:
        status, data = connection.uid("search", None, criteria)
        if status != "OK":
            raise RuntimeError("IMAP UID search failed")
        return sorted(int(uid) for uid in (data[0] or b"").split())

    def _read(self, connection, uids, want_body) -> list[MailMessage]:
        messages = []
        authserv_ids = getattr(self.account, "authserv_ids", None)
        if authserv_ids is None:
            authserv_ids = PROVIDER_AUTHSERV_IDS.get(self.account.provider, ())
        for uid in uids:
            item = MailMessage(uid)
            try:
                metadata, header = self._fetch(connection, uid, _HEADER_QUERY)
                display, address = parseaddr(str(header.get("From", "")))
                message_id = str(header.get("Message-ID", "")).strip().strip("<>").strip().lower() or None
                item = MailMessage(uid, message_id, address.lower(), display, str(header.get("Subject", "")),
                                   self._date(header, metadata), authentication_result(header, address, authserv_ids))
                if want_body(item):
                    _, message = self._fetch(connection, uid, "(BODY.PEEK[])")
                    item = replace(item, body=self._body(message))
            except Exception as exc:
                item = replace(item, error=self._safe_error(exc))
            messages.append(item)
        return messages

    @staticmethod
    def _date(message, metadata) -> datetime | None:
        candidates = [str(message.get("Date", ""))]
        internal = re.search(rb'INTERNALDATE "([^"]+)"', metadata)
        if internal:
            candidates.append(internal.group(1).decode("ascii"))
        for value in candidates:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is not None:
                    return date.astimezone(UTC)
            except (TypeError, ValueError, OverflowError):
                continue
        return None

    def _body(self, message) -> str:
        plain, html = [], []
        for part in message.walk():
            if part.is_multipart() or part.get_content_disposition() == "attachment":
                continue
            content_type = part.get_content_type()
            if content_type not in {"text/plain", "text/html"}:
                continue
            raw = part.get_payload(decode=True) or b""
            try:
                text = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            (plain if content_type == "text/plain" else html).append(text)
        if plain:
            body = "\n".join(plain)
        else:
            soup = BeautifulSoup("\n".join(html), "html.parser")
            for node in soup(["script", "style"]):
                node.decompose()
            body = soup.get_text(" ", strip=True)
        return body.strip()[:self.max_body_chars]

    async def close(self) -> None:
        """Connections are closed after every operation."""
