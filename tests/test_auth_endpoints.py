"""
Tests for authentication endpoints focused on Telegram + Firefly session flow.
"""

import pytest
from fastapi import HTTPException, Response

from app.api.dependencies import RequestSessionContext, get_firefly_client
from app.api.endpoints.auth import (
    TelegramFireflyAuthRequest,
    firefly_status,
    telegram_firefly_auth,
)
from app.core.session import build_telegram_session_id


@pytest.mark.asyncio
class TestTelegramFireflyAuthEndpoint:
    """Tests for /api/v1/auth/telegram/firefly endpoint."""

    async def test_telegram_firefly_auth_invalid_telegram_id(self) -> None:
        payload = TelegramFireflyAuthRequest(
            telegram_user_id="bad-id",
            token="any-token",
        )
        response = Response()

        with pytest.raises(HTTPException) as exc:
            await telegram_firefly_auth(payload, response)

        assert exc.value.status_code == 400
        assert "Invalid telegram_user_id" in str(exc.value.detail)

    async def test_telegram_firefly_auth_session_header_and_token_source(
        self,
    ) -> None:
        test_token = "telegram-firefly-test-token"
        expected_session_id = build_telegram_session_id("123456789", "-100123456")
        assert expected_session_id is not None

        firefly = get_firefly_client(expected_session_id)
        await firefly.clear_session_token()

        payload = TelegramFireflyAuthRequest(
            telegram_user_id="123456789",
            telegram_chat_id="-100123456",
            token=test_token,
        )
        response = Response()
        result = await telegram_firefly_auth(payload, response)

        assert result.session_id == expected_session_id
        assert result.telegram_session_id == expected_session_id
        assert result.firefly_authenticated is True
        assert result.token_source == "session"
        assert response.headers.get("X-Telegram-Session") == expected_session_id
        assert response.headers.get("X-Session-ID") == expected_session_id

        # Cleanup persisted token file to avoid polluting workspace.
        firefly = get_firefly_client(expected_session_id)
        await firefly.clear_session_token()


@pytest.mark.asyncio
class TestFireflyStatusPerSession:
    """Validate /api/v1/auth/firefly/status behavior per session."""

    async def test_firefly_status_without_token_returns_authenticated_false(self) -> None:
        session_id = build_telegram_session_id("987654321", "-100987654")
        assert session_id is not None

        firefly = get_firefly_client(session_id)
        await firefly.clear_session_token()

        session = RequestSessionContext(
            session_id=session_id,
            is_new=False,
        )

        data = await firefly_status(session)

        assert data["session_id"] == session_id
        assert data["authenticated"] is False
        assert data["connected"] is False
        assert "No Firefly token configured" in data["message"]
