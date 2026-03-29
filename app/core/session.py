"""
Session Management Utilities

Provides request session identifiers and signed OAuth state helpers
for multi-user isolation.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from re import Pattern
from re import compile as re_compile

from app.core.config import get_settings

DEFAULT_SESSION_ID = "default"
SESSION_HEADER_NAME = "X-Session-ID"
USER_ID_HEADER_NAME = "X-User-Id"
SESSION_COOKIE_NAME = "finance_session_id"
TELEGRAM_SESSION_HEADER_NAME = "X-Telegram-Session"
TELEGRAM_USER_ID_HEADER_NAME = "X-Telegram-User-ID"
TELEGRAM_CHAT_ID_HEADER_NAME = "X-Telegram-Chat-ID"

_SESSION_ID_PATTERN: Pattern[str] = re_compile(r"^[a-zA-Z0-9_-]{8,64}$")
_TELEGRAM_ID_PATTERN: Pattern[str] = re_compile(r"^-?[0-9]{4,24}$")


def normalize_session_id(raw_value: str | None) -> str | None:
    """Validate and normalize a session identifier."""
    if not raw_value:
        return None
    candidate = raw_value.strip()
    if candidate == DEFAULT_SESSION_ID:
        return candidate
    if not _SESSION_ID_PATTERN.fullmatch(candidate):
        return None
    return candidate


def generate_session_id() -> str:
    """Generate a new cryptographically-safe session identifier."""
    return uuid.uuid4().hex


def normalize_telegram_id(raw_value: str | None) -> str | None:
    """Validate and normalize Telegram user/chat id values."""
    if not raw_value:
        return None
    candidate = raw_value.strip()
    if not _TELEGRAM_ID_PATTERN.fullmatch(candidate):
        return None
    return candidate


def build_telegram_session_id(
    telegram_user_id: str | None,
    telegram_chat_id: str | None = None,
) -> str | None:
    """
    Build a deterministic app session id from Telegram identifiers.

    The resulting value is stable and does not expose raw Telegram IDs.
    """
    user_id = normalize_telegram_id(telegram_user_id)
    if user_id is None:
        return None

    chat_id = normalize_telegram_id(telegram_chat_id) or user_id
    raw_key = f"{user_id}:{chat_id}"
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:40]
    return f"tg_{digest}"


def build_user_session_id(user_id: str | None) -> str | None:
    """
    Build a deterministic app session id from a generic user identifier.

    Used to support X-User-Id header from frontend clients.
    """
    if user_id is None:
        return None
    candidate = user_id.strip()
    if not candidate:
        return None
    if len(candidate) > 128:
        return None
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:40]
    return f"usr_{digest}"


def resolve_or_create_session_id(
    header_session_id: str | None,
    cookie_session_id: str | None,
    user_id: str | None = None,
    telegram_session_id: str | None = None,
    telegram_user_id: str | None = None,
    telegram_chat_id: str | None = None,
) -> tuple[str, bool]:
    """
    Resolve session_id from request values or generate a new one.

    Resolution priority:
    1. X-Telegram-Session header
    2. Derived from X-Telegram-User-ID (+ optional X-Telegram-Chat-ID)
    3. Derived from X-User-Id
    4. X-Session-ID header
    5. finance_session_id cookie
    6. Newly generated session id
    """
    resolved_telegram = normalize_session_id(telegram_session_id)
    if resolved_telegram:
        return resolved_telegram, False

    derived_telegram = build_telegram_session_id(
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
    )
    if derived_telegram:
        return derived_telegram, False

    derived_user = build_user_session_id(user_id)
    if derived_user:
        return derived_user, False

    resolved_header = normalize_session_id(header_session_id)
    if resolved_header:
        return resolved_header, False

    resolved_cookie = normalize_session_id(cookie_session_id)
    if resolved_cookie:
        return resolved_cookie, False

    return generate_session_id(), True


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    padding = "=" * ((4 - len(raw) % 4) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def _oauth_state_key() -> bytes:
    secret = get_settings().token_encryption_key.get_secret_value().encode("utf-8")
    return hashlib.sha256(secret + b":oauth_state").digest()


def _sign_oauth_payload(payload_json: str) -> str:
    digest = hmac.new(
        _oauth_state_key(),
        payload_json.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return _b64url_encode(digest)


def create_oauth_state(session_id: str, issued_at: int | None = None) -> str:
    """
    Create a signed OAuth state payload that carries session_id.

    The state is self-contained and signed to prevent tampering.
    """
    normalized = normalize_session_id(session_id)
    if normalized is None:
        raise ValueError("Invalid session id for OAuth state")

    payload = {
        "sid": normalized,
        "iat": issued_at if issued_at is not None else int(time.time()),
        "nonce": secrets.token_urlsafe(12),
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    envelope = {
        "v": 1,
        "p": payload,
        "s": _sign_oauth_payload(payload_json),
    }
    encoded = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _b64url_encode(encoded)


def parse_oauth_state(state: str) -> str:
    """
    Validate and decode OAuth state, returning the embedded session_id.

    Raises:
        ValueError: If state is malformed, expired, or tampered with.
    """
    if not state:
        raise ValueError("Missing OAuth state")

    try:
        decoded = _b64url_decode(state)
        envelope = json.loads(decoded.decode("utf-8"))
        version = envelope["v"]
        payload = envelope["p"]
        signature = envelope["s"]
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Malformed OAuth state") from exc

    if version != 1:
        raise ValueError("Unsupported OAuth state version")

    if not isinstance(payload, dict):
        raise ValueError("Invalid OAuth state payload")

    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    expected_signature = _sign_oauth_payload(payload_json)
    if not hmac.compare_digest(signature, expected_signature):
        raise ValueError("Invalid OAuth state signature")

    session_id = normalize_session_id(payload.get("sid"))
    if session_id is None:
        raise ValueError("Invalid session id in OAuth state")

    issued_at = payload.get("iat")
    if not isinstance(issued_at, int):
        raise ValueError("Invalid OAuth state timestamp")

    now = int(time.time())
    max_age = get_settings().oauth_state_max_age_seconds
    age = now - issued_at
    if age < 0 or age > max_age:
        raise ValueError("OAuth state expired")

    return session_id
