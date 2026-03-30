"""
Notification Webhook Endpoints

Receives push notifications/SMS from phone apps and processes them
as financial transactions.
"""

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    ServicesDep,
    async_session_factory,
    get_db_session,
    get_deepseek_client,
    get_firefly_client,
    SessionDep,
)
from app.core.logging import get_logger
from app.core.session import resolve_webhook_session_id
from app.services.notification_processor import NotificationProcessorService
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
    session_ctx: SessionDep,
) -> NotificationWebhookResponse:
    """
    Webhook endpoint for phone notification app.
    
    Accepts the notification, returns 202 immediately,
    and processes in the background.

    Multi-user: include ``user_id`` in the JSON body (same value as ``X-User-Id``)
    so the webhook uses that user's Firefly/tokens when the app cannot send headers.
    """
    target_session_id = resolve_webhook_session_id(
        payload.user_id,
        session_ctx.session_id,
    )
    notification_hash = payload.notification_hash

    logger.info(
        "webhook_received",
        hash=notification_hash[:12],
        app=payload.app,
        title=payload.title[:50] if payload.title else "N/A",
        session_id=target_session_id[:16],
        source_channel=payload.source_channel,
    )

    async with async_session_factory() as db:
        firefly = get_firefly_client(target_session_id)
        deepseek = get_deepseek_client()
        processor = NotificationProcessorService(
            db,
            deepseek,
            firefly,
            session_id=target_session_id,
        )

        already_processed = await processor._notification_repo.exists(
            notification_hash
        )
        if already_processed:
            return NotificationWebhookResponse(
                accepted=False,
                notification_hash=notification_hash,
                message="Notification already processed",
            )

        if not processor.is_known_app(payload.app):
            return NotificationWebhookResponse(
                accepted=False,
                notification_hash=notification_hash,
                message=f"Unknown app: {payload.app}",
            )

    background_tasks.add_task(
        _process_notification_background,
        target_session_id,
        payload.model_dump(mode="json"),
    )

    return NotificationWebhookResponse(
        accepted=True,
        notification_hash=notification_hash,
        message="Notification accepted for processing",
    )


async def _process_notification_background(
    session_id: str,
    payload_dict: dict[str, Any],
) -> None:
    """Background task to process a notification with a fresh DB session."""
    try:
        payload = NotificationPayload.model_validate(payload_dict)
    except Exception as e:
        logger.error("webhook_background_invalid_payload", error=str(e))
        return
    try:
        async with async_session_factory() as db:
            deepseek = get_deepseek_client()
            firefly = get_firefly_client(session_id)
            processor = NotificationProcessorService(
                db,
                deepseek,
                firefly,
                session_id=session_id,
            )
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
    session_ctx: SessionDep,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    dry_run: Annotated[bool, Query(description="Preview mode")] = False,
) -> NotificationProcessingResult:
    """Process a notification synchronously and return the result."""
    target_session_id = resolve_webhook_session_id(
        payload.user_id,
        session_ctx.session_id,
    )
    try:
        firefly = get_firefly_client(target_session_id)
        deepseek = get_deepseek_client()
        processor = NotificationProcessorService(
            db,
            deepseek,
            firefly,
            session_id=target_session_id,
        )
        return await processor.process_notification(payload, dry_run=dry_run)
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
