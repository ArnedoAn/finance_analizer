"""AES-256-GCM column encryption for ``inbox_items.raw_text`` (architecture: Seguridad).

Stored format: ``v1:`` + base64(nonce[12] | ciphertext | tag[16]). Every value gets a fresh random
nonce; the associated data is fixed to the column name so a ciphertext cannot be replayed into another
column. The master key is a base64 32-byte value from ``FIELD_ENCRYPTION_KEY`` /
``FIELD_ENCRYPTION_KEY_FILE`` (read once, cached, never logged).

Behaviour without a key (development only; ``Settings`` makes the key mandatory in production):
writing a non-null value raises ``KeyNotConfiguredError``. An ephemeral key is deliberately not
generated: restarts would silently make every stored message undecryptable, so failing fast with a
clear message is safer. Null values need no key.

A value without the ``v1:`` prefix is a legacy plaintext row and is returned unchanged. The
``20261122_02`` migration encrypts all such rows before the application starts; the pass-through only
keeps databases that have not been migrated yet (for example ``create_all`` test schemas) readable.
"""
import base64
import os
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.core.config import get_settings

VERSION_PREFIX = "v1:"
ASSOCIATED_DATA = b"inbox_items.raw_text"
KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16

_KEY_FORMAT = "FIELD_ENCRYPTION_KEY must be the base64 encoding of exactly 32 random bytes (e.g. `openssl rand -base64 32`)"


class CryptoError(Exception):
    """Base class for field-encryption failures; messages never contain keys or plaintext."""


class KeyNotConfiguredError(CryptoError):
    """No master key is configured, so raw text cannot be stored or read."""


class DecryptionError(CryptoError):
    """The value is corrupted or was encrypted with a different key."""


def parse_key(value: str) -> bytes:
    """Decode a base64 master key; raise ValueError (without echoing the value) when malformed."""
    try:
        key = base64.b64decode(value.strip(), validate=True)
    except ValueError:
        raise ValueError(_KEY_FORMAT) from None
    if len(key) != KEY_BYTES:
        raise ValueError(_KEY_FORMAT)
    return key


def is_encrypted(value: str) -> bool:
    return value.startswith(VERSION_PREFIX)


def _cipher(key: bytes) -> AESGCM:
    if len(key) != KEY_BYTES:
        raise ValueError("AES-256-GCM requires a 32-byte key")
    return AESGCM(key)


def encrypt(plaintext: str, key: bytes) -> str:
    nonce = os.urandom(NONCE_BYTES)
    sealed = _cipher(key).encrypt(nonce, plaintext.encode("utf-8"), ASSOCIATED_DATA)
    return VERSION_PREFIX + base64.b64encode(nonce + sealed).decode("ascii")


def decrypt(token: str, key: bytes) -> str:
    if not is_encrypted(token):
        raise DecryptionError("Unsupported encrypted value format")
    try:
        blob = base64.b64decode(token[len(VERSION_PREFIX):], validate=True)
    except ValueError:
        raise DecryptionError("Encrypted value is corrupted") from None
    if len(blob) < NONCE_BYTES + TAG_BYTES:
        raise DecryptionError("Encrypted value is corrupted")
    try:
        return _cipher(key).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], ASSOCIATED_DATA).decode("utf-8")
    except (InvalidTag, UnicodeDecodeError):
        raise DecryptionError("Cannot decrypt value: wrong key or corrupted data") from None


@lru_cache(maxsize=1)
def master_key() -> bytes:
    """Master key from settings, read once. Missing key is not cached, so a fixed config recovers."""
    value = get_settings().field_encryption_key.get_secret_value()
    if not value:
        raise KeyNotConfiguredError(
            "FIELD_ENCRYPTION_KEY (or FIELD_ENCRYPTION_KEY_FILE) is not configured; "
            "refusing to store or read encrypted raw text without a key"
        )
    return parse_key(value)


class EncryptedText(TypeDecorator):
    """Text column that encrypts on write and decrypts on read (``None`` passes through)."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt(value, master_key())

    def process_result_value(self, value, dialect):
        if value is None or not is_encrypted(value):
            return value
        return decrypt(value, master_key())
