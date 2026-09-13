"""Focused tests for ``app.core.config.Settings`` resolution rules.

Covers the runtime DNS contract:

* ``GMAIL_REDIRECT_URI`` is the explicit override and beats ``PUBLIC_BASE_URL``.
* Empty / whitespace ``GMAIL_REDIRECT_URI`` is treated as absent.
* ``PUBLIC_BASE_URL`` derives the callback as ``<base>/api/v1/auth/callback``
  with the trailing slash stripped from the base.
* With neither set, the localhost default applies.
* External ``FIREFLY_BASE_URL`` / ``DEEPSEEK_API_URL`` are independent of the
  Gmail derivation.

Tests instantiate ``Settings`` directly with ``_env_file=None`` so no host
environment or ``.env`` file can leak values into the assertions. Required
secrets are passed explicitly.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings

_REQUIRED_SECRETS: dict[str, Any] = {
    "token_encryption_key": "a" * 64,
    "deepseek_api_key": "sk-test-deepseek",
    "firefly_base_url": "https://firefly.test.example.com",
    "firefly_api_token": "ff-test-token",
}


def _settings(**overrides: Any) -> Settings:
    """Build Settings with required secrets plus per-test overrides."""
    merged: dict[str, Any] = {**_REQUIRED_SECRETS, **overrides}
    return Settings(_env_file=None, **merged)


class TestGmailRedirectUriResolution:
    """Verify the explicit-override > PUBLIC_BASE_URL > localhost precedence."""

    def test_public_base_url_derives_callback(self) -> None:
        # Given a public HTTPS origin and no explicit override
        # When Settings resolves
        # Then the callback URL is base + path
        settings = _settings(public_base_url="https://finance.example.com")

        assert settings.public_base_url == "https://finance.example.com"
        assert settings.gmail_redirect_uri == "https://finance.example.com/api/v1/auth/callback"

    def test_public_base_url_trailing_slash_is_stripped(self) -> None:
        # Given a base URL with a trailing slash
        # When Settings resolves
        # Then no double slash appears between base and callback path
        settings = _settings(public_base_url="https://finance.example.com/")

        assert settings.gmail_redirect_uri == "https://finance.example.com/api/v1/auth/callback"

    def test_public_base_url_trailing_slashes_are_stripped(self) -> None:
        # Given a base URL with multiple trailing slashes
        # When Settings resolves
        # Then all trailing slashes are removed
        settings = _settings(public_base_url="https://finance.example.com///")

        assert settings.gmail_redirect_uri == "https://finance.example.com/api/v1/auth/callback"

    def test_explicit_redirect_uri_overrides_public_base_url(self) -> None:
        # Given both PUBLIC_BASE_URL and GMAIL_REDIRECT_URI are set
        # When Settings resolves
        # Then the explicit value wins
        settings = _settings(
            public_base_url="https://finance.example.com",
            gmail_redirect_uri="https://override.example.com/api/v1/auth/callback",
        )

        assert settings.gmail_redirect_uri == "https://override.example.com/api/v1/auth/callback"

    def test_blank_redirect_uri_falls_back_to_public_base_url(self) -> None:
        # Given an empty string override and PUBLIC_BASE_URL is set
        # When Settings resolves
        # Then the empty string is treated as absent
        settings = _settings(
            public_base_url="https://finance.example.com",
            gmail_redirect_uri="",
        )

        assert settings.gmail_redirect_uri == "https://finance.example.com/api/v1/auth/callback"

    def test_whitespace_redirect_uri_falls_back_to_public_base_url(self) -> None:
        # Given a whitespace-only override and PUBLIC_BASE_URL is set
        # When Settings resolves
        # Then the whitespace is treated as absent
        settings = _settings(
            public_base_url="https://finance.example.com",
            gmail_redirect_uri="   ",
        )

        assert settings.gmail_redirect_uri == "https://finance.example.com/api/v1/auth/callback"

    def test_neither_set_uses_localhost_default(self) -> None:
        # Given neither PUBLIC_BASE_URL nor GMAIL_REDIRECT_URI is set
        # When Settings resolves
        # Then the localhost default applies
        settings = _settings()

        assert settings.public_base_url is None
        assert settings.gmail_redirect_uri == "http://localhost:8000/api/v1/auth/callback"

    def test_blank_with_no_public_base_url_uses_localhost_default(self) -> None:
        # Given an empty override and no PUBLIC_BASE_URL
        # When Settings resolves
        # Then the empty value falls through to the localhost default
        settings = _settings(gmail_redirect_uri="")

        assert settings.gmail_redirect_uri == "http://localhost:8000/api/v1/auth/callback"

    def test_public_base_url_with_surrounding_whitespace_is_stripped(self) -> None:
        # Given a PUBLIC_BASE_URL with surrounding whitespace
        # When Settings resolves
        # Then the resolved callback has no whitespace leakage
        settings = _settings(public_base_url="  https://finance.example.com  ")

        assert settings.gmail_redirect_uri == "https://finance.example.com/api/v1/auth/callback"


class TestExternalUrlsAreIndependent:
    """Confirm the resolver does not touch unrelated external URLs."""

    def test_firefly_base_url_is_required_and_unchanged(self) -> None:
        # Given a Firefly base URL
        # When Settings resolves
        # Then it is preserved verbatim regardless of PUBLIC_BASE_URL
        settings = _settings(
            public_base_url="https://finance.example.com",
            firefly_base_url="https://firefly.internal.example.com",
        )

        assert settings.firefly_base_url == "https://firefly.internal.example.com"
        assert settings.gmail_redirect_uri.startswith("https://finance.example.com/")

    def test_deepseek_api_url_is_independent_of_public_base_url(self) -> None:
        # Given a DeepSeek API URL
        # When Settings resolves
        # Then it remains the DeepSeek upstream (not rewritten to PUBLIC_BASE_URL)
        settings = _settings(
            public_base_url="https://finance.example.com",
            deepseek_api_url="https://api.deepseek.com/v1/chat/completions",
        )

        assert settings.deepseek_api_url == "https://api.deepseek.com/v1/chat/completions"
        assert "deepseek" not in settings.gmail_redirect_uri

    def test_custom_deepseek_url_is_preserved(self) -> None:
        # Given a custom DeepSeek endpoint
        # When Settings resolves
        # Then it is not touched by the Gmail resolver
        settings = _settings(
            public_base_url="https://finance.example.com",
            deepseek_api_url="https://proxy.internal/deepseek/v1/chat/completions",
        )

        assert settings.deepseek_api_url == "https://proxy.internal/deepseek/v1/chat/completions"


class TestRequiredSecrets:
    """Missing required secrets must still fail validation."""

    def test_missing_token_encryption_key_raises(self) -> None:
        with pytest.raises(ValidationError):
            Settings(
                _env_file=None,
                deepseek_api_key="sk-test",
                firefly_api_token="ff-test",
                firefly_base_url="https://firefly.example.com",
            )

    def test_missing_deepseek_api_key_raises(self) -> None:
        with pytest.raises(ValidationError):
            Settings(
                _env_file=None,
                token_encryption_key="a" * 64,
                firefly_api_token="ff-test",
                firefly_base_url="https://firefly.example.com",
            )

    def test_missing_firefly_api_token_raises(self) -> None:
        with pytest.raises(ValidationError):
            Settings(
                _env_file=None,
                token_encryption_key="a" * 64,
                deepseek_api_key="sk-test",
                firefly_base_url="https://firefly.example.com",
            )

    def test_missing_firefly_base_url_raises(self) -> None:
        with pytest.raises(ValidationError):
            Settings(
                _env_file=None,
                token_encryption_key="a" * 64,
                deepseek_api_key="sk-test",
                firefly_api_token="ff-test",
            )


class TestFieldTypeStability:
    """``gmail_redirect_uri`` must remain a plain ``str`` for callers."""

    def test_gmail_redirect_uri_is_str_type(self) -> None:
        settings = _settings(public_base_url="https://finance.example.com")

        assert isinstance(settings.gmail_redirect_uri, str)
