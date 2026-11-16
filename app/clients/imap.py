"""Generic password-authenticated IMAP, with blocking operations off the event loop."""

import asyncio
import imaplib
import re
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

from app.core.config import get_settings

_PROVIDER_HOSTS = {
    "gmail": "imap.gmail.com", "yahoo": "imap.mail.yahoo.com", "icloud": "imap.mail.me.com",
}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class IMAPClient:
    def __init__(self, account) -> None:
        self.account = account
        self.max_body_chars = get_settings().email_max_body_chars

    def _connect(self):
        account = self.account
        host = account.host or _PROVIDER_HOSTS.get(account.provider, "")
        if not host:
            raise ValueError("An IMAP host is required for a custom provider")
        cls = imaplib.IMAP4_SSL if account.ssl else imaplib.IMAP4
        connection = cls(host, account.port, timeout=30)
        try:
            connection.login(account.username, account.password.get_secret_value())
            status, _ = connection.select(account.folder, readonly=True)
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
        except (OSError, imaplib.IMAP4.error):
            pass

    async def test(self) -> dict:
        try:
            await asyncio.to_thread(self._test)
            return {"ok": True, "error": None}
        except Exception as exc:
            # Server responses can echo the login name; do not return credentials.
            error = str(exc).replace(self.account.password.get_secret_value(), "***") if self.account.password.get_secret_value() else str(exc)
            return {"ok": False, "error": error[:500]}

    def _test(self) -> None:
        connection = self._connect()
        self._logout(connection)

    async def poll(self, uidvalidity: str | None, last_uid: int, lookback_days: int) -> tuple[str, int, list[dict]]:
        return await asyncio.to_thread(self._poll, uidvalidity, last_uid, lookback_days)

    def _poll(self, uidvalidity, last_uid, lookback_days):
        connection = self._connect()
        try:
            _, validity_data = connection.response("UIDVALIDITY")
            if not validity_data or not validity_data[0]:
                raise RuntimeError("IMAP server did not provide UIDVALIDITY")
            validity = validity_data[0].decode("ascii").strip()
            continuing = validity == uidvalidity and last_uid > 0
            if continuing:
                criteria = f"UID {last_uid + 1}:*"
            else:
                since = datetime.now(UTC) - timedelta(days=lookback_days)
                criteria = f'SINCE {since.day:02d}-{_MONTHS[since.month - 1]}-{since.year}'
            status, data = connection.uid("search", None, criteria)
            if status != "OK":
                raise RuntimeError("IMAP UID search failed")
            uids = sorted(int(uid) for uid in (data[0] or b"").split())
            if continuing:
                # IMAP n:* can return the highest existing UID even when n is larger.
                uids = [uid for uid in uids if uid > last_uid]
            highwater = last_uid if continuing else 0
            messages = []
            for uid in uids:
                status, parts = connection.uid("fetch", str(uid), "(BODY.PEEK[] INTERNALDATE)")
                if status != "OK":
                    raise RuntimeError(f"IMAP fetch failed for UID {uid}")
                fetched = next((part for part in parts if isinstance(part, tuple)), None)
                if fetched is None:
                    raise RuntimeError(f"IMAP fetch omitted body for UID {uid}")
                metadata, raw = fetched
                message = BytesParser(policy=policy.default).parsebytes(raw)
                occurred_at = self._date(message, metadata)
                messages.append({
                    "uid": uid, "sender": str(message.get("From", "")),
                    "subject": str(message.get("Subject", "")),
                    "body": self._body(message), "occurred_at": occurred_at,
                })
                highwater = uid
            return validity, highwater, messages
        finally:
            self._logout(connection)

    @staticmethod
    def _date(message, metadata) -> datetime:
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
        raise ValueError("Mail has no valid timezone-aware Date or INTERNALDATE")

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
        """Connections are closed after each worker-thread operation."""
