"""AES-256-GCM column encryption for ``inbox_items.raw_text`` (architecture: Seguridad).

Stored format: ``enc:v1:<kid>:`` + base64(nonce[12] | ciphertext | tag[16]). ``kid`` is the first 8 hex
characters of the SHA-256 of the key that sealed the value, so a reader can pick the right key after a
rotation. Every value gets a fresh random nonce; the associated data is fixed to the column name so a
ciphertext cannot be replayed into another column. The prefix ``enc:v1:`` is deliberately unlikely in
real SMS or e-mail text; a legacy plaintext row that merely starts with ``v1:`` is still plaintext.

Keys (base64 of 32 random bytes, read once, cached, never logged):

* Active key: ``FIELD_ENCRYPTION_KEY`` / ``FIELD_ENCRYPTION_KEY_FILE``. Seals every new value.
* Old keys (optional): ``FIELD_ENCRYPTION_OLD_KEYS_FILE`` (or ``FIELD_ENCRYPTION_OLD_KEYS``), a comma
  separated list of base64 keys. They are only used to decrypt.

Rotation: generate a new key, set it as the active key, and put the previous active key in the old-keys
list; restart. Rows sealed with the old key stay readable and are re-sealed with the new key whenever
they are written again. ``raw_text`` is purged after 30 days (``raw_text_retention_days``), so once that
time has passed since the rotation the old key can be removed from the list.

Reading never poisons the caller: a stored value that cannot be decrypted (no matching key, wrong key
after a restore, corrupted data) is logged as ``raw_text_undecryptable`` (without any content) and
surfaces as ``UNDECRYPTABLE_TEXT``, so listings and the processing cycle (retention purge included) keep
working. Writing without an active key still fails fast: an ephemeral key is deliberately not generated
(restarts would silently make every stored message undecryptable). Null values need no key.

A value without the ``enc:v1:`` prefix is a legacy plaintext row and is returned unchanged. The
``20261122_02`` migration encrypts all such rows before the application starts; the pass-through only
keeps databases that have not been migrated yet (for example ``create_all`` test schemas) readable.
"""
import base64
import hashlib
import os
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.secrets import read_secret

logger = get_logger(__name__)

VERSION_PREFIX = "enc:v1:"
ASSOCIATED_DATA = b"inbox_items.raw_text"
KEY_BYTES = 32
KEY_ID_CHARS = 8
NONCE_BYTES = 12
TAG_BYTES = 16
OLD_KEYS_SECRET = "FIELD_ENCRYPTION_OLD_KEYS"
UNDECRYPTABLE_TEXT = "[texto cifrado no legible]"

_KEY_FORMAT = "FIELD_ENCRYPTION_KEY must be the base64 encoding of exactly 32 random bytes (e.g. `openssl rand -base64 32`)"


class CryptoError(Exception):
    """Base class for field-encryption failures; messages never contain keys or plaintext."""


class KeyNotConfiguredError(CryptoError):
    """No master key is configured, so raw text cannot be stored or read."""


class DecryptionError(CryptoError):
    """The value is corrupted or was encrypted with a key that is not configured."""


def parse_key(value: str) -> bytes:
    """Decode a base64 master key; raise ValueError (without echoing the value) when malformed."""
    try:
        key = base64.b64decode(value.strip(), validate=True)
    except ValueError:
        raise ValueError(_KEY_FORMAT) from None
    if len(key) != KEY_BYTES:
        raise ValueError(_KEY_FORMAT)
    return key


def parse_old_keys(value: str | None) -> tuple[bytes, ...]:
    """Decode a comma-separated list of base64 keys; the error names the entry position, never its value."""
    if not value:
        return ()
    keys = []
    for position, entry in enumerate(value.split(","), start=1):
        if not entry.strip():
            continue
        try:
            keys.append(parse_key(entry))
        except ValueError:
            raise ValueError(
                f"{OLD_KEYS_SECRET} entry {position} must be the base64 encoding of exactly 32 random bytes"
            ) from None
    return tuple(keys)


def key_id(key: bytes) -> str:
    """Stable, non-reversible identifier of a key: 8 hex characters of its SHA-256."""
    return hashlib.sha256(key).hexdigest()[:KEY_ID_CHARS]


def is_encrypted(value: str) -> bool:
    return value.startswith(VERSION_PREFIX)


def token_key_id(token: str) -> str | None:
    """The ``kid`` written in an encrypted token, or None when the token is malformed."""
    kid, separator, _ = token[len(VERSION_PREFIX):].partition(":")
    return kid if is_encrypted(token) and separator and len(kid) == KEY_ID_CHARS else None


def _cipher(key: bytes) -> AESGCM:
    if len(key) != KEY_BYTES:
        raise ValueError("AES-256-GCM requires a 32-byte key")
    return AESGCM(key)


def encrypt(plaintext: str, key: bytes) -> str:
    nonce = os.urandom(NONCE_BYTES)
    sealed = _cipher(key).encrypt(nonce, plaintext.encode("utf-8"), ASSOCIATED_DATA)
    return f"{VERSION_PREFIX}{key_id(key)}:" + base64.b64encode(nonce + sealed).decode("ascii")


def decrypt(token: str, key: bytes, retired: tuple[bytes, ...] = ()) -> str:
    """Decrypt with the key whose id the token carries, searching ``key`` first and then ``retired``."""
    kid = token_key_id(token)
    if kid is None:
        raise DecryptionError("Unsupported encrypted value format")
    try:
        blob = base64.b64decode(token[len(VERSION_PREFIX) + KEY_ID_CHARS + 1:], validate=True)
    except ValueError:
        raise DecryptionError("Encrypted value is corrupted") from None
    if len(blob) < NONCE_BYTES + TAG_BYTES:
        raise DecryptionError("Encrypted value is corrupted")
    candidates = [candidate for candidate in (key, *retired) if key_id(candidate) == kid]
    if not candidates:
        raise DecryptionError("Encrypted value was sealed with a key that is not configured")
    for candidate in candidates:
        try:
            return _cipher(candidate).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], ASSOCIATED_DATA).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError):
            continue
    raise DecryptionError("Cannot decrypt value: wrong key or corrupted data")


@lru_cache(maxsize=1)
def master_key() -> bytes:
    """Active key from settings, read once. Missing key is not cached, so a fixed config recovers."""
    value = get_settings().field_encryption_key.get_secret_value()
    if not value:
        raise KeyNotConfiguredError(
            "FIELD_ENCRYPTION_KEY (or FIELD_ENCRYPTION_KEY_FILE) is not configured; "
            "refusing to store or read encrypted raw text without a key"
        )
    return parse_key(value)


@lru_cache(maxsize=1)
def old_keys() -> tuple[bytes, ...]:
    """Retired keys, decrypt-only, from ``FIELD_ENCRYPTION_OLD_KEYS_FILE`` / ``FIELD_ENCRYPTION_OLD_KEYS``.

    A malformed list raises ``ValueError`` (not cached), so a typo is loud instead of silently
    turning rotated rows into undecryptable ones.
    """
    return parse_old_keys(read_secret(OLD_KEYS_SECRET))


class EncryptedText(TypeDecorator):
    """Text column that encrypts on write and decrypts on read (``None`` passes through).

    A value that cannot be decrypted reads as ``UNDECRYPTABLE_TEXT`` instead of raising.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt(value, master_key())

    def process_result_value(self, value, dialect):
        if value is None or not is_encrypted(value):
            return value
        try:
            return decrypt(value, master_key(), old_keys())
        except DecryptionError:
            logger.warning("raw_text_undecryptable", key_id=token_key_id(value))
            return UNDECRYPTABLE_TEXT
