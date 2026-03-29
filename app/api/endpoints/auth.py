"""
Authentication Endpoints

Handles Gmail OAuth 2.0 authentication flow.
"""

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, SecretStr

from app.api.dependencies import (
    ServicesDep,
    SessionDep,
    apply_session_cookie,
    get_firefly_client,
    get_gmail_client,
)
from app.core.exceptions import FireflyAuthenticationError, GmailAuthenticationError
from app.core.logging import get_logger
from app.core.session import (
    TELEGRAM_SESSION_HEADER_NAME,
    build_telegram_session_id,
    create_pkce_code_verifier,
    create_oauth_state,
    parse_oauth_state_payload,
)

logger = get_logger(__name__)
router = APIRouter()


class AuthStatus(BaseModel):
    """Authentication status response."""
    gmail_authenticated: bool
    firefly_authenticated: bool = False
    firefly_token_source: str | None = None
    email: str | None = None
    message: str


class AuthUrlResponse(BaseModel):
    """OAuth URL response."""
    authorization_url: str
    state: str
    session_id: str
    message: str


class FireflyTokenRequest(BaseModel):
    """Request payload to configure Firefly token for the active session."""
    token: SecretStr


class FireflyTokenResponse(BaseModel):
    """Response with session-scoped Firefly token status."""
    session_id: str
    firefly_authenticated: bool
    token_source: str | None
    message: str


class TelegramFireflyAuthRequest(BaseModel):
    """Telegram payload to authenticate Firefly for a specific Telegram user/chat."""
    telegram_user_id: str
    telegram_chat_id: str | None = None
    token: SecretStr


class TelegramFireflyAuthResponse(BaseModel):
    """Response for Telegram-driven Firefly authentication."""
    session_id: str
    telegram_session_id: str
    firefly_authenticated: bool
    token_source: str | None
    message: str


@router.get(
    "/url",
    response_model=AuthUrlResponse,
    summary="Get Gmail OAuth URL",
    description="Generate OAuth authorization URL to start Gmail authentication flow.",
)
async def get_auth_url(session: SessionDep) -> AuthUrlResponse:
    """
    Get the OAuth authorization URL.
    
    The user should visit this URL to authorize Gmail access,
    then be redirected back to /auth/callback with the authorization code.
    
    Returns:
        Authorization URL and state for CSRF protection.
    """
    try:
        code_verifier = create_pkce_code_verifier()
        oauth_state = create_oauth_state(
            session.session_id,
            code_verifier=code_verifier,
        )
        gmail = get_gmail_client(session.session_id)
        auth_url, state = gmail.get_authorization_url(
            state=oauth_state,
            code_verifier=code_verifier,
        )
        
        logger.info(
            "gmail_auth_url_generated",
            session_id=session.session_id,
            state=state[:8] + "...",
        )
        
        return AuthUrlResponse(
            authorization_url=auth_url,
            state=state,
            session_id=session.session_id,
            message="Visit the authorization_url to grant Gmail access",
        )
    except GmailAuthenticationError as e:
        raise HTTPException(
            status_code=400,
            detail=e.message,
        )
    except Exception as e:
        logger.error("gmail_auth_url_failed", error=str(e))
        raise HTTPException(
            status_code=500,
            detail=f"Failed to generate authorization URL: {str(e)}",
        )


@router.get(
    "/callback",
    summary="Gmail OAuth Callback",
    description="Handle OAuth callback from Google after user authorization.",
)
async def oauth_callback(
    response: Response,
    code: str = Query(..., description="Authorization code from Google"),
    state: str | None = Query(None, description="State parameter for CSRF protection"),
    error: str | None = Query(None, description="Error from OAuth flow"),
) -> dict:
    """
    Handle the OAuth callback from Google.
    
    This endpoint receives the authorization code after the user
    authorizes the application in their browser.
    
    Args:
        code: Authorization code from Google
        state: State parameter (for CSRF protection)
        error: Error message if authorization failed
        
    Returns:
        Success message if authentication completed.
    """
    if error:
        logger.warning("gmail_oauth_callback_error", error=error)
        raise HTTPException(
            status_code=400,
            detail=f"OAuth authorization failed: {error}",
        )
    
    try:
        provided_state = state or ""
        state_payload = parse_oauth_state_payload(provided_state)
        session_id = state_payload["session_id"]
        code_verifier = state_payload.get("code_verifier") or None
        gmail = get_gmail_client(session_id)
        success = await gmail.handle_oauth_callback(
            code=code,
            state=provided_state,
            expected_state=provided_state,
            code_verifier=code_verifier,
        )
        
        if success:
            apply_session_cookie(response, session_id)
            logger.info("gmail_oauth_callback_success", session_id=session_id)
            return {
                "status": "success",
                "session_id": session_id,
                "message": "Gmail authentication completed successfully! You can close this window.",
            }
        else:
            raise HTTPException(
                status_code=500,
                detail="Authentication failed unexpectedly",
            )
            
    except GmailAuthenticationError as e:
        logger.error("gmail_oauth_callback_failed", error=e.message)
        raise HTTPException(
            status_code=400,
            detail=e.message,
        )
    except ValueError as e:
        logger.warning("gmail_oauth_callback_invalid_state", error=str(e))
        raise HTTPException(
            status_code=400,
            detail=f"Invalid OAuth state: {str(e)}",
        )
    except Exception as e:
        logger.error("gmail_oauth_callback_error", error=str(e))
        raise HTTPException(
            status_code=500,
            detail=f"Callback processing failed: {str(e)}",
        )


@router.get(
    "/status",
    response_model=AuthStatus,
    summary="Authentication Status",
    description="Check Gmail OAuth authentication status.",
)
async def auth_status(services: ServicesDep) -> AuthStatus:
    """
    Check if Gmail is authenticated.
    
    Returns:
        Authentication status and connected email if authenticated.
    """
    try:
        connected = await services.gmail.check_connection()
        firefly_authenticated = await services.firefly.has_session_token()
        firefly_token_source = await services.firefly.get_token_source()
        
        if connected:
            return AuthStatus(
                gmail_authenticated=True,
                firefly_authenticated=firefly_authenticated,
                firefly_token_source=firefly_token_source,
                message="Gmail is authenticated and connected",
            )
        else:
            return AuthStatus(
                gmail_authenticated=False,
                firefly_authenticated=firefly_authenticated,
                firefly_token_source=firefly_token_source,
                message="Gmail authentication required",
            )
    except Exception as e:
        return AuthStatus(
            gmail_authenticated=False,
            firefly_authenticated=False,
            message=f"Authentication check failed: {str(e)}",
        )


@router.post(
    "/gmail/init",
    response_model=AuthStatus,
    summary="Initialize Gmail Authentication",
    description="Start Gmail OAuth 2.0 authentication flow.",
)
async def init_gmail_auth(services: ServicesDep) -> AuthStatus:
    """
    Initialize Gmail OAuth authentication.
    
    This will:
    1. Check for existing valid credentials
    2. Refresh expired credentials if possible
    3. Start new OAuth flow if needed (opens browser)
    
    Returns:
        Authentication status after flow completion.
        
    Note:
        This endpoint will block until the OAuth flow completes.
        The user must authorize in their browser.
    """
    try:
        success = await services.gmail.authenticate()
        
        if success:
            return AuthStatus(
                gmail_authenticated=True,
                message="Gmail authentication successful",
            )
        else:
            return AuthStatus(
                gmail_authenticated=False,
                message="Gmail authentication failed",
            )
            
    except GmailAuthenticationError as e:
        raise HTTPException(
            status_code=401,
            detail=f"Gmail authentication failed: {e.message}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Authentication error: {str(e)}",
        )


@router.get(
    "/firefly/status",
    summary="Firefly III Connection Status",
    description="Check Firefly III API connectivity.",
)
async def firefly_status(session: SessionDep) -> dict:
    """
    Check Firefly III connection and get server info.
    
    Returns:
        Server information if connected.
    """
    firefly = get_firefly_client(session.session_id)
    token_source = await firefly.get_token_source()
    if token_source is None:
        return {
            "connected": False,
            "session_id": session.session_id,
            "authenticated": False,
            "message": "No Firefly token configured for this session",
        }

    try:
        connected = await firefly.check_connection()

        if connected:
            about = await firefly.get_about()
            return {
                "connected": True,
                "session_id": session.session_id,
                "authenticated": True,
                "token_source": token_source,
                "version": about.get("version"),
                "api_version": about.get("api_version"),
                "os": about.get("os"),
            }
        return {
            "connected": False,
            "session_id": session.session_id,
            "authenticated": True,
            "token_source": token_source,
            "message": "Could not connect to Firefly III",
        }
    except Exception as e:
        return {
            "connected": False,
            "session_id": session.session_id,
            "authenticated": True,
            "token_source": token_source,
            "error": str(e),
        }


@router.put(
    "/firefly/token",
    response_model=FireflyTokenResponse,
    summary="Set Firefly token for session",
    description="Persist a Firefly API token for the active session.",
)
async def set_firefly_token(
    payload: FireflyTokenRequest,
    session: SessionDep,
) -> FireflyTokenResponse:
    """Set session-scoped Firefly token."""
    firefly = get_firefly_client(session.session_id)
    try:
        await firefly.set_session_token(payload.token.get_secret_value())
        return FireflyTokenResponse(
            session_id=session.session_id,
            firefly_authenticated=True,
            token_source="session",
            message="Firefly token configured for this session",
        )
    except FireflyAuthenticationError as e:
        raise HTTPException(status_code=400, detail=e.message) from e


@router.delete(
    "/firefly/token",
    response_model=FireflyTokenResponse,
    summary="Clear Firefly token for session",
    description="Remove the Firefly API token for the active session.",
)
async def clear_firefly_token(session: SessionDep) -> FireflyTokenResponse:
    """Remove session-scoped Firefly token."""
    firefly = get_firefly_client(session.session_id)
    await firefly.clear_session_token()
    return FireflyTokenResponse(
        session_id=session.session_id,
        firefly_authenticated=False,
        token_source=None,
        message="Firefly token cleared for this session",
    )


@router.post(
    "/telegram/firefly",
    response_model=TelegramFireflyAuthResponse,
    summary="Authenticate Firefly via Telegram",
    description=(
        "Resolve a deterministic session from Telegram user/chat identifiers "
        "and persist the Firefly token for that session."
    ),
)
async def telegram_firefly_auth(
    payload: TelegramFireflyAuthRequest,
    response: Response,
) -> TelegramFireflyAuthResponse:
    """
    Authenticate Firefly in one step for Telegram bot flows.

    The bot sends Telegram identifiers + user Firefly token, and this endpoint
    returns the stable session_id to reuse in future API calls.
    """
    session_id = build_telegram_session_id(
        telegram_user_id=payload.telegram_user_id,
        telegram_chat_id=payload.telegram_chat_id,
    )
    if session_id is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid telegram_user_id or telegram_chat_id",
        )

    firefly = get_firefly_client(session_id)
    try:
        await firefly.set_session_token(payload.token.get_secret_value())
    except FireflyAuthenticationError as e:
        raise HTTPException(status_code=400, detail=e.message) from e
    except Exception as e:
        logger.error(
            "telegram_firefly_auth_failed",
            session_id=session_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=500,
            detail=f"Could not save Firefly token: {str(e)}",
        ) from e

    apply_session_cookie(response, session_id)
    response.headers[TELEGRAM_SESSION_HEADER_NAME] = session_id

    return TelegramFireflyAuthResponse(
        session_id=session_id,
        telegram_session_id=session_id,
        firefly_authenticated=True,
        token_source=await firefly.get_token_source(),
        message="Firefly token configured for Telegram session",
    )


@router.get(
    "/deepseek/status",
    summary="DeepSeek AI Connection Status",
    description="Check DeepSeek API connectivity.",
)
async def deepseek_status(services: ServicesDep) -> dict:
    """
    Check DeepSeek AI API connection.
    
    Returns:
        Connection status.
    """
    try:
        connected = await services.deepseek.check_connection()
        
        return {
            "connected": connected,
            "message": "DeepSeek API is accessible" if connected else "Cannot reach DeepSeek API",
        }
    except Exception as e:
        return {
            "connected": False,
            "error": str(e),
        }
