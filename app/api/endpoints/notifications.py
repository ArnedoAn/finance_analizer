"""
Notification Webhook Endpoints

Receives push notifications/SMS from phone apps and processes them
as financial transactions.
"""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import ServicesDep
from app.core.logging import get_logger
from app.models.schemas import (
    NotificationPayload,
    NotificationProcessingResult,
    NotificationWebhookResponse,
    ProcessingStatus,
)

logger = get_logger(__name__)

router = APIRouter()


@router.post(
    "/webhook",
    response_model=NotificationWebhookResponse,
    status_code=202,
    summary="Receive Notification Webhook",
    description="Receives push notification/SMS from phone app, processes asynchronously.",
)
async def webhook_receive(
    payload: NotificationPayload,
    background_tasks: BackgroundTasks,
    services: ServicesDep,
) -> NotificationWebhookResponse:
    """
    Webhook endpoint for phone notification app.
    
    Accepts the notification, returns 202 immediately,
    and processes in the background.
    """
    notification_hash = payload.notification_hash
    
    logger.info(
        "webhook_received",
        hash=notification_hash[:12],
        app=payload.app,
        title=payload.title[:50] if payload.title else "N/A",
    )
    
    # Quick idempotency check before queuing
    already_processed = await services.notification_processor._notification_repo.exists(
        notification_hash
    )
    if already_processed:
        return NotificationWebhookResponse(
            accepted=False,
            notification_hash=notification_hash,
            message="Notification already processed",
        )
    
    # Quick known-app check
    if not services.notification_processor.is_known_app(payload.app):
        return NotificationWebhookResponse(
            accepted=False,
            notification_hash=notification_hash,
            message=f"Unknown app: {payload.app}",
        )
    
    # Process in background
    background_tasks.add_task(
        _process_notification_background,
        services.notification_processor,
        payload,
    )
    
    return NotificationWebhookResponse(
        accepted=True,
        notification_hash=notification_hash,
        message="Notification accepted for processing",
    )


async def _process_notification_background(
    processor: Any,
    payload: NotificationPayload,
) -> None:
    """Background task to process a notification."""
    try:
        result = await processor.process_notification(payload)
        logger.info(
            "webhook_background_completed",
            hash=payload.notification_hash[:12],
            status=result.status.value,
            transaction_id=result.transaction_id,
        )
    except Exception as e:
        logger.error(
            "webhook_background_failed",
            hash=payload.notification_hash[:12],
            error=str(e),
        )


@router.post(
    "/process",
    response_model=NotificationProcessingResult,
    summary="Process Notification Synchronously",
    description="Process a notification synchronously (useful for testing/debugging).",
)
async def process_sync(
    payload: NotificationPayload,
    services: ServicesDep,
    dry_run: Annotated[bool, Query(description="Preview mode")] = False,
) -> NotificationProcessingResult:
    """Process a notification synchronously and return the result."""
    try:
        result = await services.notification_processor.process_notification(
            payload, dry_run=dry_run
        )
        return result
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Notification processing failed: {str(e)}",
        )


@router.get(
    "/history",
    summary="Get Notification History",
    description="Query processed notifications.",
)
async def get_history(
    services: ServicesDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    app: Annotated[str | None, Query(description="Filter by app package")] = None,
) -> dict:
    """Get recent notification processing history."""
    try:
        history = await services.notification_processor.get_history(limit, app)
        return {
            "total": len(history),
            "notifications": history,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get history: {str(e)}",
        )


@router.get(
    "/stats",
    summary="Get Notification Statistics",
    description="Get notification processing statistics.",
)
async def get_stats(services: ServicesDep) -> dict:
    """Get notification processing statistics."""
    try:
        stats = await services.notification_processor.get_statistics()
        return stats
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get statistics: {str(e)}",
        )
