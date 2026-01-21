"""
Senders API Endpoints

Endpoints for managing known financial email senders.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    get_db_session,
    get_deepseek_client,
    get_gmail_client,
)
from app.clients.deepseek import DeepSeekClient
from app.clients.gmail import GmailClient
from app.core.logging import get_logger
from app.models.schemas import (
    KnownSenderBulkCreate,
    KnownSenderCreate,
    KnownSenderResponse,
    SenderLearningRequest,
    SenderLearningResponse,
)
from app.services.sender_learning import SenderLearningService

logger = get_logger(__name__)
router = APIRouter(prefix="/senders", tags=["Senders"])


@router.get(
    "/",
    response_model=list[KnownSenderResponse],
    summary="List known senders",
    description="Get all known financial email senders.",
)
async def list_senders(
    include_inactive: bool = False,
    session: AsyncSession = Depends(get_db_session),
    gmail: GmailClient = Depends(get_gmail_client),
    deepseek: DeepSeekClient = Depends(get_deepseek_client),
) -> list[dict[str, Any]]:
    """List all known senders."""
    service = SenderLearningService(session, gmail, deepseek)
    return await service.get_all_senders(include_inactive=include_inactive)


@router.post(
    "/",
    response_model=dict[str, Any],
    status_code=status.HTTP_201_CREATED,
    summary="Add known sender",
    description="Manually add a new known financial sender.",
)
async def add_sender(
    sender: KnownSenderCreate,
    session: AsyncSession = Depends(get_db_session),
    gmail: GmailClient = Depends(get_gmail_client),
    deepseek: DeepSeekClient = Depends(get_deepseek_client),
) -> dict[str, Any]:
    """Add a new known sender."""
    service = SenderLearningService(session, gmail, deepseek)
    result = await service.add_sender_manually(
        keyword=sender.keyword,
        sender_name=sender.sender_name,
        sender_type=sender.sender_type,
    )
    
    if "error" in result:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result["error"],
        )
    
    return result


@router.post(
    "/bulk",
    response_model=dict[str, Any],
    status_code=status.HTTP_201_CREATED,
    summary="Bulk add senders",
    description="Add multiple known senders at once.",
)
async def bulk_add_senders(
    request: KnownSenderBulkCreate,
    session: AsyncSession = Depends(get_db_session),
    gmail: GmailClient = Depends(get_gmail_client),
    deepseek: DeepSeekClient = Depends(get_deepseek_client),
) -> dict[str, Any]:
    """Bulk add known senders."""
    service = SenderLearningService(session, gmail, deepseek)
    return await service.bulk_add_senders(
        senders=[s.model_dump() for s in request.senders]
    )


@router.delete(
    "/{keyword}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deactivate sender",
    description="Deactivate a known sender by keyword.",
)
async def deactivate_sender(
    keyword: str,
    session: AsyncSession = Depends(get_db_session),
    gmail: GmailClient = Depends(get_gmail_client),
    deepseek: DeepSeekClient = Depends(get_deepseek_client),
) -> None:
    """Deactivate a sender."""
    service = SenderLearningService(session, gmail, deepseek)
    await service.deactivate_sender(keyword)


@router.post(
    "/learn",
    response_model=SenderLearningResponse,
    summary="Learn senders from emails",
    description="""
    Analyze recent emails to automatically discover new financial senders.
    
    This uses AI to identify patterns in email senders and adds them
    to the known senders dictionary.
    """,
)
async def learn_senders(
    request: SenderLearningRequest | None = None,
    session: AsyncSession = Depends(get_db_session),
    gmail: GmailClient = Depends(get_gmail_client),
    deepseek: DeepSeekClient = Depends(get_deepseek_client),
) -> dict[str, Any]:
    """Run sender learning from recent emails."""
    request = request or SenderLearningRequest()
    
    service = SenderLearningService(session, gmail, deepseek)
    return await service.learn_from_recent_emails(
        email_count=request.email_count,
        days_back=request.days_back,
    )


# Example senders for initial setup
EXAMPLE_SENDERS = [
    {"keyword": "bancolombia", "sender_name": "Bancolombia", "sender_type": "bank"},
    {"keyword": "davivienda", "sender_name": "Davivienda", "sender_type": "bank"},
    {"keyword": "nequi", "sender_name": "Nequi", "sender_type": "payment"},
    {"keyword": "daviplata", "sender_name": "Daviplata", "sender_type": "payment"},
    {"keyword": "rappipay", "sender_name": "RappiPay", "sender_type": "payment"},
    {"keyword": "paypal", "sender_name": "PayPal", "sender_type": "payment"},
    {"keyword": "stripe", "sender_name": "Stripe", "sender_type": "payment"},
    {"keyword": "amazon", "sender_name": "Amazon", "sender_type": "store"},
    {"keyword": "mercadolibre", "sender_name": "MercadoLibre", "sender_type": "store"},
    {"keyword": "rappi", "sender_name": "Rappi", "sender_type": "store"},
    {"keyword": "netflix", "sender_name": "Netflix", "sender_type": "subscription"},
    {"keyword": "spotify", "sender_name": "Spotify", "sender_type": "subscription"},
    {"keyword": "lulo", "sender_name": "Lulo Bank", "sender_type": "bank"},
    {"keyword": "nubank", "sender_name": "Nu Bank", "sender_type": "bank"},
]


@router.post(
    "/seed",
    response_model=dict[str, Any],
    summary="Seed initial senders",
    description="Add common Colombian financial senders as initial data.",
)
async def seed_senders(
    session: AsyncSession = Depends(get_db_session),
    gmail: GmailClient = Depends(get_gmail_client),
    deepseek: DeepSeekClient = Depends(get_deepseek_client),
) -> dict[str, Any]:
    """Seed database with example senders."""
    service = SenderLearningService(session, gmail, deepseek)
    return await service.bulk_add_senders(EXAMPLE_SENDERS)
