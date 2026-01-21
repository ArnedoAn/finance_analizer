"""
Authentication Endpoints

Handles Gmail OAuth 2.0 authentication flow.
"""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.api.dependencies import ServicesDep, get_gmail_client
from app.core.exceptions import GmailAuthenticationError
from app.core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()


class AuthStatus(BaseModel):
    """Authentication status response."""
    gmail_authenticated: bool
    email: str | None = None
    message: str


class AuthUrlResponse(BaseModel):
    """OAuth URL response."""
    authorization_url: str
    state: str
    message: str


@router.get(
    "/url",
    response_model=AuthUrlResponse,
    summary="Get Gmail OAuth URL",
    description="Generate OAuth authorization URL to start Gmail authentication flow.",
)
async def get_auth_url() -> AuthUrlResponse:
    """
    Get the OAuth authorization URL.
    
    The user should visit this URL to authorize Gmail access,
    then be redirected back to /auth/callback with the authorization code.
    
    Returns:
        Authorization URL and state for CSRF protection.
    """
    try:
        gmail = get_gmail_client()
        auth_url, state = gmail.get_authorization_url()
        
        logger.info("gmail_auth_url_generated", state=state[:8] + "...")
        
        return AuthUrlResponse(
            authorization_url=auth_url,
            state=state,
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
        gmail = get_gmail_client()
        success = await gmail.handle_oauth_callback(code, state)
        
        if success:
            logger.info("gmail_oauth_callback_success")
            return {
                "status": "success",
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
        
        if connected:
            return AuthStatus(
                gmail_authenticated=True,
                message="Gmail is authenticated and connected",
            )
        else:
            return AuthStatus(
                gmail_authenticated=False,
                message="Gmail authentication required",
            )
    except Exception as e:
        return AuthStatus(
            gmail_authenticated=False,
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
async def firefly_status(services: ServicesDep) -> dict:
    """
    Check Firefly III connection and get server info.
    
    Returns:
        Server information if connected.
    """
    try:
        connected = await services.firefly.check_connection()
        
        if connected:
            about = await services.firefly.get_about()
            return {
                "connected": True,
                "version": about.get("version"),
                "api_version": about.get("api_version"),
                "os": about.get("os"),
            }
        else:
            return {
                "connected": False,
                "message": "Could not connect to Firefly III",
            }
    except Exception as e:
        return {
            "connected": False,
            "error": str(e),
        }


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
