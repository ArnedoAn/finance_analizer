"""
Processing Endpoints

Main endpoints for processing emails and creating transactions.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.api.dependencies import ServicesDep
from app.core.exceptions import ProcessingError
from app.models.schemas import (
    BatchProcessRequest,
    BatchProcessResponse,
    ProcessingResult,
    ProcessingStatus,
)

router = APIRouter()


class ProcessSingleRequest(BaseModel):
    """Request to process a single email."""
    email_id: str
    dry_run: bool = False


class ProcessSingleResponse(BaseModel):
    """Response for single email processing."""
    email_id: str
    status: ProcessingStatus
    transaction_id: str | None = None
    analysis: dict | None = None
    error: str | None = None
    processing_time_ms: int


@router.post(
    "/batch",
    response_model=BatchProcessResponse,
    summary="Process Email Batch",
    description="Process a batch of emails from Gmail.",
)
async def process_batch(
    services: ServicesDep,
    request: BatchProcessRequest | None = None,
) -> BatchProcessResponse:
    """
    Process a batch of financial emails.
    
    This is the main processing endpoint that:
    1. Fetches emails from Gmail matching filters
    2. Analyzes each with DeepSeek AI
    3. Creates transactions in Firefly III
    
    Args:
        request: Batch processing configuration.
        
    Returns:
        BatchProcessResponse with results for each email.
    """
    request = request or BatchProcessRequest()
    
    try:
        result = await services.email_processor.process_batch(request)
        return result
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Batch processing failed: {str(e)}",
        )


@router.post(
    "/single",
    response_model=ProcessSingleResponse,
    summary="Process Single Email",
    description="Process a specific email by ID.",
)
async def process_single(
    request: ProcessSingleRequest,
    services: ServicesDep,
) -> ProcessSingleResponse:
    """
    Process a single email by ID.
    
    Use this to manually trigger processing of a specific email.
    
    Args:
        request: Email ID and processing options.
        
    Returns:
        Processing result with transaction details.
    """
    try:
        # Fetch the email
        email = await services.gmail.get_message_by_id(request.email_id)
        
        if not email:
            raise HTTPException(
                status_code=404,
                detail=f"Email not found: {request.email_id}",
            )
        
        # Process it
        result = await services.email_processor.process_single_email(
            email,
            dry_run=request.dry_run,
        )
        
        return ProcessSingleResponse(
            email_id=result.email_id,
            status=result.status,
            transaction_id=result.transaction_id,
            analysis=result.analysis.model_dump(mode="json") if result.analysis else None,
            error=result.error_message,
            processing_time_ms=result.processing_time_ms,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Processing failed: {str(e)}",
        )


@router.post(
    "/retry-failed",
    response_model=BatchProcessResponse,
    summary="Retry Failed Emails",
    description="Reprocess previously failed emails.",
)
async def retry_failed(
    services: ServicesDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> BatchProcessResponse:
    """
    Retry processing of previously failed emails.
    
    Args:
        limit: Maximum number of failed emails to retry.
        
    Returns:
        BatchProcessResponse with retry results.
    """
    try:
        result = await services.email_processor.reprocess_failed(limit)
        return result
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Retry failed: {str(e)}",
        )


@router.get(
    "/audit",
    summary="Get Audit Logs",
    description="Get processing audit logs.",
)
async def get_audit_logs(
    services: ServicesDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    status: Annotated[ProcessingStatus | None, Query()] = None,
) -> dict:
    """
    Get recent audit logs.
    
    Args:
        limit: Maximum number of logs to return.
        status: Filter by processing status.
        
    Returns:
        List of audit log entries.
    """
    try:
        logs = await services.email_processor.get_audit_logs(limit, status)
        return {
            "total": len(logs),
            "logs": logs,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get audit logs: {str(e)}",
        )


@router.get(
    "/statistics",
    summary="Get Processing Statistics",
    description="Get processing statistics and metrics.",
)
async def get_statistics(services: ServicesDep) -> dict:
    """
    Get processing statistics.
    
    Returns:
        Statistics grouped by status.
    """
    try:
        stats = await services.email_processor.get_statistics()
        return stats
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get statistics: {str(e)}",
        )


@router.post(
    "/dry-run",
    response_model=BatchProcessResponse,
    summary="Dry Run Processing",
    description="Process emails without creating transactions (preview mode).",
)
async def dry_run(
    services: ServicesDep,
    max_emails: Annotated[int, Query(ge=1, le=50)] = 10,
) -> BatchProcessResponse:
    """
    Process emails in dry-run mode.
    
    This analyzes emails and shows what transactions would be created,
    without actually creating them in Firefly III.
    
    Args:
        max_emails: Maximum number of emails to process.
        
    Returns:
        BatchProcessResponse with analysis results.
    """
    request = BatchProcessRequest(
        max_emails=max_emails,
        dry_run=True,
    )
    
    try:
        result = await services.email_processor.process_batch(request)
        return result
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Dry run failed: {str(e)}",
        )


class DebugTransactionRequest(BaseModel):
    """Request for debug transaction creation."""
    source_account: str = "Bancolombia"
    destination_account: str = "Test Merchant"
    amount: str = "10000"
    description: str = "Debug test transaction"
    category: str = "Test"


@router.post(
    "/debug/test-transaction",
    summary="Debug: Test Transaction Creation",
    description="Create a test transaction directly to debug Firefly III integration.",
)
async def debug_test_transaction(
    services: ServicesDep,
    request: DebugTransactionRequest,
) -> dict:
    """
    Create a test transaction directly to Firefly III.
    
    This bypasses the AI analysis to test the Firefly integration directly.
    """
    from datetime import datetime
    from app.models.schemas import TransactionCreate, TransactionSplit, TransactionType
    import httpx
    from app.core.config import get_settings
    from app.api.dependencies import get_firefly_client
    
    settings = get_settings()
    firefly = get_firefly_client(services.session_id)
    if not await firefly.has_session_token():
        raise HTTPException(
            status_code=401,
            detail=(
                "No Firefly token configured for this session. "
                "Use /api/v1/auth/firefly/token first."
            ),
        )
    
    # Build the exact payload
    payload = {
        "error_if_duplicate_hash": False,
        "apply_rules": False,
        "fire_webhooks": False,
        "transactions": [
            {
                "type": "withdrawal",
                "date": datetime.now().strftime("%Y-%m-%d"),
                "amount": request.amount,
                "description": request.description,
                "currency_code": settings.default_currency,
                "source_name": request.source_account,
                "destination_name": request.destination_account,
                "category_name": request.category,
            }
        ],
    }
    
    # Make raw request to see exact response
    async with httpx.AsyncClient(
        base_url=f"{settings.firefly_base_url.rstrip('/')}/api/v1",
        headers={
            "Authorization": f"Bearer {await firefly.get_active_token()}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.api+json",
        },
        timeout=30.0,
    ) as client:
        response = await client.post("/transactions", json=payload)
        
        return {
            "request_payload": payload,
            "response_status": response.status_code,
            "response_body": response.json(),
            "success": response.status_code == 200,
        }
