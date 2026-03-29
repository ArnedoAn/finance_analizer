"""
Gmail API Client

Handles OAuth 2.0 authentication and email fetching from Gmail.
Uses the Google API Python client with async wrappers.
"""

import asyncio
import base64
import hashlib
import json
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import partial
from pathlib import Path
from typing import Any


from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build, Resource

from app.core.config import get_settings
from app.core.exceptions import (
    GmailAuthenticationError,
    GmailFetchError,
    GmailParseError,
)
from app.core.logging import get_logger
from app.core.security import get_token_encryption
from app.core.session import DEFAULT_SESSION_ID, normalize_session_id
from app.models.schemas import EmailFilter, EmailMessage

logger = get_logger(__name__)

# Gmail API scopes (read-only)
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    """
    Client for Gmail API operations.
    
    Provides OAuth 2.0 authentication and email fetching with
    filtering and deduplication support.
    """
    
    def __init__(self, session_id: str = DEFAULT_SESSION_ID) -> None:
        self.settings = get_settings()
        normalized_session_id = normalize_session_id(session_id)
        if normalized_session_id is None:
            raise ValueError(f"Invalid session id: {session_id}")
        self.session_id = normalized_session_id
        self._token_path = self._resolve_token_path(normalized_session_id)
        self._service: Resource | None = None
        self._credentials: Credentials | None = None
    
    def _resolve_token_path(self, session_id: str) -> Path:
        """
        Resolve token path for a session.
        
        Uses the legacy token file for default session and a session-suffixed
        filename for all other sessions.
        """
        base = self.settings.google_token_path
        if session_id == DEFAULT_SESSION_ID:
            return base
        return base.with_name(f"{base.stem}_{session_id}{base.suffix}")
    
    async def _run_sync(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Run synchronous function in executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, partial(func, *args, **kwargs)
        )
    
    async def authenticate(self) -> bool:
        """
        Authenticate with Gmail API using OAuth 2.0.
        
        Loads existing token if valid, otherwise initiates OAuth flow.
        
        Returns:
            True if authentication successful.
            
        Raises:
            GmailAuthenticationError: If authentication fails.
        """
        try:
            creds = await self._load_credentials()
            
            logger.debug(
                "gmail_auth_creds_status",
                has_creds=creds is not None,
                valid=creds.valid if creds else None,
                expired=creds.expired if creds else None,
                has_refresh_token=bool(creds.refresh_token) if creds else None,
            )
            
            if not creds:
                # No credentials at all, need to authenticate
                logger.info("gmail_starting_oauth_flow")
                creds = await self._run_oauth_flow()
                await self._save_credentials(creds)
            elif not creds.valid:
                if creds.expired and creds.refresh_token:
                    # Token expired but we have refresh token
                    logger.info("gmail_refreshing_token")
                    await self._run_sync(creds.refresh, Request())
                    await self._save_credentials(creds)
                elif creds.token:
                    # Token might still work, let's try it
                    logger.info("gmail_trying_existing_token")
                else:
                    # No valid token and can't refresh
                    logger.info("gmail_starting_oauth_flow")
                    creds = await self._run_oauth_flow()
                    await self._save_credentials(creds)
            
            self._credentials = creds
            self._service = await self._run_sync(
                build, "gmail", "v1", credentials=creds
            )
            
            logger.info("gmail_authenticated")
            return True
            
        except Exception as e:
            logger.error("gmail_authentication_failed", error=str(e))
            raise GmailAuthenticationError(
                "Failed to authenticate with Gmail",
                original_error=e,
            ) from e
    
    async def _load_credentials(self) -> Credentials | None:
        """Load credentials from encrypted storage."""
        token_path = self._token_path
        
        logger.debug(
            "gmail_loading_credentials",
            session_id=self.session_id,
            token_path=str(token_path),
            exists=token_path.exists(),
        )
        
        if not token_path.exists():
            logger.warning(
                "gmail_token_not_found",
                session_id=self.session_id,
                token_path=str(token_path),
            )
            return None
        
        try:
            encryption = get_token_encryption()
            encrypted_data = token_path.read_text()
            token_data = encryption.decrypt_dict(encrypted_data)
            
            logger.info(
                "gmail_credentials_loaded_successfully",
                session_id=self.session_id,
            )
            return Credentials.from_authorized_user_info(token_data, SCOPES)
        except Exception as e:
            logger.warning(
                "gmail_failed_load_credentials",
                session_id=self.session_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            return None
    
    async def _save_credentials(self, creds: Credentials) -> None:
        """Save credentials to encrypted storage."""
        try:
            token_path = self._token_path
            token_path.parent.mkdir(parents=True, exist_ok=True)
            
            encryption = get_token_encryption()
            token_data = {
                "token": creds.token,
                "refresh_token": creds.refresh_token,
                "token_uri": creds.token_uri,
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "scopes": creds.scopes,
            }
            
            encrypted = encryption.encrypt_dict(token_data)
            token_path.write_text(encrypted)
            
            logger.debug(
                "gmail_credentials_saved",
                session_id=self.session_id,
                token_path=str(token_path),
            )
        except Exception as e:
            logger.error(
                "gmail_failed_save_credentials",
                session_id=self.session_id,
                error=str(e),
            )
            raise
    
    async def _run_oauth_flow(self) -> Credentials:
        """Run the OAuth 2.0 authorization flow (requires manual URL/callback)."""
        raise GmailAuthenticationError(
            "No valid credentials. Use /api/v1/auth/url to get authorization URL, "
            "then complete the flow via /api/v1/auth/callback",
            details={"action": "visit_auth_url"},
        )
    
    @staticmethod
    def _pkce_code_challenge(code_verifier: str) -> str:
        """Generate S256 PKCE code challenge from code verifier."""
        digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")

    def get_authorization_url(
        self,
        state: str | None = None,
        code_verifier: str | None = None,
    ) -> tuple[str, str]:
        """
        Generate OAuth authorization URL for web-based flow.
        
        Returns:
            Tuple of (authorization_url, state)
        """
        creds_path = self.settings.google_credentials_path
        
        if not creds_path.exists():
            raise GmailAuthenticationError(
                f"Google credentials file not found: {creds_path}",
                details={"path": str(creds_path)},
            )
        
        flow = Flow.from_client_secrets_file(
            str(creds_path),
            scopes=SCOPES,
            redirect_uri=self.settings.gmail_redirect_uri,
        )
        
        auth_kwargs: dict[str, Any] = {
            "access_type": "offline",
            "prompt": "consent",  # Force consent to get refresh_token
            "state": state,
        }
        if code_verifier:
            auth_kwargs["code_challenge"] = self._pkce_code_challenge(code_verifier)
            auth_kwargs["code_challenge_method"] = "S256"

        authorization_url, returned_state = flow.authorization_url(**auth_kwargs)
        
        return authorization_url, returned_state
    
    async def handle_oauth_callback(
        self,
        code: str,
        state: str | None = None,
        expected_state: str | None = None,
        code_verifier: str | None = None,
    ) -> bool:
        """
        Handle OAuth callback with authorization code.
        
        Args:
            code: Authorization code from Google
            state: State parameter for CSRF protection (optional)
            expected_state: Expected state value to validate.
            
        Returns:
            True if authentication successful
        """
        creds_path = self.settings.google_credentials_path
        
        if not creds_path.exists():
            raise GmailAuthenticationError(
                f"Google credentials file not found: {creds_path}",
            )
        
        try:
            if expected_state is not None and state != expected_state:
                raise GmailAuthenticationError(
                    "OAuth state mismatch",
                    details={"reason": "state_mismatch"},
                )
            
            flow = Flow.from_client_secrets_file(
                str(creds_path),
                scopes=SCOPES,
                redirect_uri=self.settings.gmail_redirect_uri,
                state=expected_state,
            )
            
            # Exchange code for credentials
            token_kwargs: dict[str, Any] = {"code": code}
            if code_verifier:
                token_kwargs["code_verifier"] = code_verifier
            await self._run_sync(flow.fetch_token, **token_kwargs)
            creds = flow.credentials
            
            # Save credentials
            await self._save_credentials(creds)
            
            # Initialize service
            self._credentials = creds
            self._service = await self._run_sync(
                build, "gmail", "v1", credentials=creds
            )
            
            logger.info("gmail_oauth_callback_success", session_id=self.session_id)
            return True
            
        except Exception as e:
            logger.error(
                "gmail_oauth_callback_failed",
                session_id=self.session_id,
                error=str(e),
            )
            raise GmailAuthenticationError(
                f"OAuth callback failed: {str(e)}",
                original_error=e,
            ) from e
    
    def _ensure_authenticated(self) -> None:
        """Ensure client is authenticated."""
        if self._service is None:
            raise GmailAuthenticationError(
                "Gmail client not authenticated. Call authenticate() first."
            )
    
    def _build_query(self, filter_config: EmailFilter) -> str:
        """Build Gmail search query from filter configuration."""
        query_parts: list[str] = []
        
        # Subject filters
        if filter_config.subjects:
            subject_query = " OR ".join(
                f'subject:"{s}"' for s in filter_config.subjects
            )
            query_parts.append(f"({subject_query})")
        
        # Sender filters
        # Now treated as exact From addresses for more precise matching.
        # Each sender value should ideally be a full email address,
        # e.g. "alertas@bancolombia.com" → from:"alertas@bancolombia.com"
        if filter_config.senders:
            sender_query = " OR ".join(
                f'from:"{s}"' for s in filter_config.senders
            )
            query_parts.append(f"({sender_query})")
        
        # Date filter
        if filter_config.after_date:
            date_str = filter_config.after_date.strftime("%Y/%m/%d")
            query_parts.append(f"after:{date_str}")
        
        # Only inbox emails (not spam/trash)
        query_parts.append("in:inbox")
        
        return " ".join(query_parts)
    
    async def fetch_emails(
        self,
        filter_config: EmailFilter | None = None,
        exclude_ids: set[str] | None = None,
    ) -> list[EmailMessage]:
        """
        Fetch emails matching the filter criteria.
        
        Args:
            filter_config: Filter configuration. Uses defaults if None.
            exclude_ids: Set of internal IDs to exclude (for deduplication).
            
        Returns:
            List of parsed email messages.
            
        Raises:
            GmailFetchError: If fetching fails.
        """
        self._ensure_authenticated()
        exclude_ids = exclude_ids or set()
        
        # Build default filter if not provided
        if filter_config is None:
            filter_config = EmailFilter(
                subjects=self.settings.gmail_subjects_list
                if self.settings.gmail_use_subject_filters
                else [],
                max_results=self.settings.gmail_max_results,
                after_date=datetime.utcnow() - timedelta(
                    days=self.settings.email_lookback_days
                ),
            )
        
        try:
            query = self._build_query(filter_config)
            logger.info("gmail_fetching_emails", query=query)
            
            # Fetch message list
            result = await self._run_sync(
                self._service.users().messages().list(
                    userId="me",
                    q=query,
                    maxResults=filter_config.max_results,
                ).execute
            )
            
            messages = result.get("messages", [])
            logger.info("gmail_found_messages", count=len(messages))
            
            # Filter out already processed
            if exclude_ids:
                messages = [m for m in messages if m["id"] not in exclude_ids]
                logger.info(
                    "gmail_after_dedup",
                    count=len(messages),
                    excluded=len(exclude_ids),
                )
            
            # Fetch full message details
            email_messages: list[EmailMessage] = []
            
            for msg_info in messages:
                try:
                    email = await self._fetch_message_details(msg_info["id"])
                    if email:
                        email_messages.append(email)
                except GmailParseError as e:
                    logger.warning(
                        "gmail_parse_error",
                        message_id=msg_info["id"],
                        error=str(e),
                    )
                    continue
            
            logger.info("gmail_emails_fetched", count=len(email_messages))
            return email_messages
            
        except Exception as e:
            logger.error("gmail_fetch_failed", error=str(e))
            raise GmailFetchError(
                "Failed to fetch emails from Gmail",
                original_error=e,
            ) from e
    
    async def _fetch_message_details(self, message_id: str) -> EmailMessage | None:
        """Fetch and parse a single message."""
        try:
            message = await self._run_sync(
                self._service.users().messages().get(
                    userId="me",
                    id=message_id,
                    format="full",
                ).execute
            )
            
            return self._parse_message(message)
            
        except Exception as e:
            raise GmailParseError(
                f"Failed to parse message {message_id}",
                original_error=e,
            ) from e
    
    def _parse_message(self, message: dict[str, Any]) -> EmailMessage:
        """Parse Gmail API message into EmailMessage model."""
        headers = {
            h["name"].lower(): h["value"]
            for h in message.get("payload", {}).get("headers", [])
        }
        
        # Extract Message-ID
        message_id = headers.get("message-id", f"<{message['id']}@gmail>")
        
        # Parse date
        date_str = headers.get("date", "")
        try:
            email_date = parsedate_to_datetime(date_str)
        except Exception:
            # Fallback to internalDate (milliseconds timestamp)
            internal_ts = int(message.get("internalDate", 0)) / 1000
            email_date = datetime.fromtimestamp(internal_ts)
        
        # Extract body
        body_text, body_html = self._extract_body(message.get("payload", {}))
        
        return EmailMessage(
            message_id=message_id,
            internal_id=message["id"],
            thread_id=message.get("threadId", ""),
            subject=headers.get("subject", ""),
            sender=headers.get("from", ""),
            recipient=headers.get("to", ""),
            date=email_date,
            body_text=body_text,
            body_html=body_html,
            snippet=message.get("snippet", ""),
            labels=message.get("labelIds", []),
        )
    
    def _extract_body(self, payload: dict[str, Any]) -> tuple[str, str]:
        """Extract text and HTML body from message payload."""
        body_text = ""
        body_html = ""
        
        def process_part(part: dict[str, Any]) -> None:
            nonlocal body_text, body_html
            
            mime_type = part.get("mimeType", "")
            body_data = part.get("body", {}).get("data", "")
            
            if body_data:
                decoded = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore")
                
                if mime_type == "text/plain":
                    body_text = decoded
                elif mime_type == "text/html":
                    body_html = decoded
            
            # Process nested parts
            for subpart in part.get("parts", []):
                process_part(subpart)
        
        process_part(payload)
        
        # If only HTML, extract text from it
        if body_html and not body_text:
            body_text = self._html_to_text(body_html)
        
        return body_text, body_html
    
    def _html_to_text(self, html: str) -> str:
        """Convert HTML to plain text."""
        try:
            soup = BeautifulSoup(html, "html.parser")
            
            # Remove script and style elements
            for element in soup(["script", "style", "head", "meta"]):
                element.decompose()
            
            # Get text
            text = soup.get_text(separator="\n", strip=True)
            
            # Clean up whitespace
            lines = (line.strip() for line in text.splitlines())
            return "\n".join(line for line in lines if line)
            
        except Exception:
            return html
    
    async def get_message_by_id(self, internal_id: str) -> EmailMessage | None:
        """Fetch a specific message by its internal ID."""
        self._ensure_authenticated()
        return await self._fetch_message_details(internal_id)
    
    async def check_connection(self) -> bool:
        """Check if Gmail API is accessible."""
        try:
            self._ensure_authenticated()
            
            # Try to get profile
            profile = await self._run_sync(
                self._service.users().getProfile(userId="me").execute
            )
            
            logger.info("gmail_connection_ok", email=profile.get("emailAddress"))
            return True
            
        except Exception as e:
            logger.error("gmail_connection_failed", error=str(e))
            return False
    
    async def fetch_email_summaries(
        self,
        filter_config: EmailFilter | None = None,
    ) -> list[dict[str, str]]:
        """
        Fetch email summaries (sender + subject only) for learning.
        
        This is a lightweight fetch that only gets headers,
        not the full message body.
        
        Args:
            filter_config: Filter configuration.
            
        Returns:
            List of dicts with 'sender' and 'subject' keys.
        """
        self._ensure_authenticated()
        
        # Build default filter if not provided
        if filter_config is None:
            filter_config = EmailFilter(
                max_results=100,
                after_date=datetime.utcnow() - timedelta(days=30),
            )
        
        try:
            # Build simple query without subject filters (for learning)
            query_parts = ["in:inbox"]
            if filter_config.after_date:
                date_str = filter_config.after_date.strftime("%Y/%m/%d")
                query_parts.append(f"after:{date_str}")
            
            query = " ".join(query_parts)
            logger.info("gmail_fetching_summaries", query=query)
            
            # Fetch message list
            result = await self._run_sync(
                self._service.users().messages().list(
                    userId="me",
                    q=query,
                    maxResults=filter_config.max_results,
                ).execute
            )
            
            messages = result.get("messages", [])
            summaries: list[dict[str, str]] = []
            
            # Fetch only headers (metadata format)
            for msg_info in messages:
                try:
                    message = await self._run_sync(
                        self._service.users().messages().get(
                            userId="me",
                            id=msg_info["id"],
                            format="metadata",
                            metadataHeaders=["From", "Subject"],
                        ).execute
                    )
                    
                    headers = {
                        h["name"].lower(): h["value"]
                        for h in message.get("payload", {}).get("headers", [])
                    }
                    
                    summaries.append({
                        "sender": headers.get("from", ""),
                        "subject": headers.get("subject", ""),
                        "internal_id": msg_info["id"],
                    })
                    
                except Exception as e:
                    logger.warning(
                        "gmail_summary_fetch_error",
                        message_id=msg_info["id"],
                        error=str(e),
                    )
                    continue
            
            logger.info("gmail_summaries_fetched", count=len(summaries))
            return summaries
            
        except Exception as e:
            logger.error("gmail_fetch_summaries_failed", error=str(e))
            return []
    
    async def fetch_emails_by_senders(
        self,
        sender_keywords: set[str],
        filter_config: EmailFilter | None = None,
        exclude_ids: set[str] | None = None,
    ) -> list[EmailMessage]:
        """
        Fetch emails from known senders (by keyword matching).
        
        Args:
            sender_keywords: Set of keywords to match in sender email.
            filter_config: Additional filter configuration.
            exclude_ids: Set of internal IDs to exclude.
            
        Returns:
            List of parsed email messages.
        """
        self._ensure_authenticated()
        exclude_ids = exclude_ids or set()
        
        if not sender_keywords:
            logger.warning("gmail_no_sender_keywords")
            return []
        
        # Build default filter if not provided
        if filter_config is None:
            filter_config = EmailFilter(
                max_results=self.settings.gmail_max_results,
                after_date=datetime.utcnow() - timedelta(
                    days=self.settings.email_lookback_days
                ),
            )
        
        # Override senders with keywords
        filter_config.senders = list(sender_keywords)
        
        try:
            query = self._build_query(filter_config)
            logger.info(
                "gmail_fetching_by_senders",
                query=query,
                keyword_count=len(sender_keywords),
            )
            
            # Fetch message list
            result = await self._run_sync(
                self._service.users().messages().list(
                    userId="me",
                    q=query,
                    maxResults=filter_config.max_results,
                ).execute
            )
            
            messages = result.get("messages", [])
            logger.info("gmail_found_by_senders", count=len(messages))
            
            # Filter out already processed
            if exclude_ids:
                messages = [m for m in messages if m["id"] not in exclude_ids]
            
            # Fetch full message details
            email_messages: list[EmailMessage] = []
            
            for msg_info in messages:
                try:
                    email = await self._fetch_message_details(msg_info["id"])
                    if email:
                        email_messages.append(email)
                except GmailParseError as e:
                    logger.warning(
                        "gmail_parse_error",
                        message_id=msg_info["id"],
                        error=str(e),
                    )
                    continue
            
            return email_messages
            
        except Exception as e:
            logger.error("gmail_fetch_by_senders_failed", error=str(e))
            raise GmailFetchError(
                "Failed to fetch emails by senders",
                original_error=e,
            ) from e
