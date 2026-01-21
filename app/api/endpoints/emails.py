"""
Email Endpoints

Provides access to Gmail emails for preview and manual processing.
"""

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.api.dependencies import ServicesDep
from app.core.config import get_settings
from app.core.exceptions import GmailError, ProcessingError
from app.models.schemas import EmailFilter

router = APIRouter()


class EmailPreview(BaseModel):
    """Email preview response."""
    id: str
    subject: str
    sender: str
    date: str
    snippet: str
    labels: list[str]


class EmailListResponse(BaseModel):
    """Email list response."""
    total: int
    emails: list[EmailPreview]


class EmailDetailResponse(BaseModel):
    """Detailed email response."""
    id: str
    message_id: str
    thread_id: str
    subject: str
    sender: str
    recipient: str
    date: str
    body_text: str
    body_html: str
    labels: list[str]


@router.get(
    "",
    response_model=EmailListResponse,
    summary="List Emails",
    description="List emails matching configured filters.",
)
async def list_emails(
    services: ServicesDep,
    max_results: Annotated[int, Query(ge=1, le=100)] = 20,
    days_back: Annotated[int, Query(ge=1, le=365)] = 7,
    subjects: Annotated[str | None, Query(description="Comma-separated subjects")] = None,
) -> EmailListResponse:
    """
    List emails from Gmail matching filters.
    
    Args:
        max_results: Maximum number of emails to return.
        days_back: Number of days to look back.
        subjects: Custom subject filters (comma-separated).
        
    Returns:
        List of email previews.
    """
    settings = get_settings()
    
    # Build filter
    subject_list = (
        [s.strip() for s in subjects.split(",")]
        if subjects
        else settings.gmail_subjects_list
    )
    
    filter_config = EmailFilter(
        subjects=subject_list,
        max_results=max_results,
        after_date=datetime.utcnow() - timedelta(days=days_back),
    )
    
    try:
        emails = await services.gmail.fetch_emails(filter_config=filter_config)
        
        return EmailListResponse(
            total=len(emails),
            emails=[
                EmailPreview(
                    id=email.internal_id,
                    subject=email.subject,
                    sender=email.sender,
                    date=email.date.isoformat(),
                    snippet=email.snippet,
                    labels=email.labels,
                )
                for email in emails
            ],
        )
    except GmailError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Gmail error: {e.message}",
        )


@router.get(
    "/{email_id}",
    response_model=EmailDetailResponse,
    summary="Get Email Details",
    description="Get full details of a specific email.",
)
async def get_email(
    email_id: str,
    services: ServicesDep,
) -> EmailDetailResponse:
    """
    Get detailed email content by ID.
    
    Args:
        email_id: Gmail internal message ID.
        
    Returns:
        Full email details including body.
    """
    try:
        email = await services.gmail.get_message_by_id(email_id)
        
        if not email:
            raise HTTPException(
                status_code=404,
                detail=f"Email not found: {email_id}",
            )
        
        return EmailDetailResponse(
            id=email.internal_id,
            message_id=email.message_id,
            thread_id=email.thread_id,
            subject=email.subject,
            sender=email.sender,
            recipient=email.recipient,
            date=email.date.isoformat(),
            body_text=email.body_text,
            body_html=email.body_html,
            labels=email.labels,
        )
    except GmailError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Gmail error: {e.message}",
        )


@router.get(
    "/{email_id}/analyze",
    summary="Analyze Email",
    description="Analyze email with AI without creating transaction.",
)
async def analyze_email(
    email_id: str,
    services: ServicesDep,
) -> dict:
    """
    Analyze a specific email with DeepSeek AI.
    
    This is a preview/dry-run that shows what the AI extracts
    without creating any transaction.
    
    Args:
        email_id: Gmail internal message ID.
        
    Returns:
        Email details and AI analysis results.
    """
    try:
        result = await services.email_processor.analyze_email_preview(email_id)
        return result
    except ProcessingError as e:
        raise HTTPException(
            status_code=404,
            detail=e.message,
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Analysis failed: {str(e)}",
        )
