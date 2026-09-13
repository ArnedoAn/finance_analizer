"""
Senders API Endpoints

Endpoints for managing known financial email senders.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.api.dependencies import SenderLearningDep
from app.models.schemas import (
    KnownSenderBulkCreate,
    KnownSenderCreate,
    KnownSenderResponse,
    SenderLearningRequest,
    SenderLearningResponse,
)

router = APIRouter(prefix="/senders", tags=["Senders"])


@router.get(
    "/",
    response_model=list[KnownSenderResponse],
    summary="List known senders",
    description="Get all known financial email senders.",
)
async def list_senders(
    service: SenderLearningDep,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    """List all known senders."""
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
    service: SenderLearningDep,
) -> dict[str, Any]:
    """Add a new known sender."""
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
    service: SenderLearningDep,
) -> dict[str, Any]:
    """Bulk add known senders."""
    return await service.bulk_add_senders(
        senders=[s.model_dump() for s in request.senders]
    )


@router.delete(
    "/{keyword}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deactivate sender",
    description="Deactivate a known sender by keyword.",
)
async def deactivate_sender(keyword: str, service: SenderLearningDep) -> None:
    """Deactivate a sender."""
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
    service: SenderLearningDep,
    request: SenderLearningRequest | None = None,
) -> dict[str, Any]:
    """Run sender learning from recent emails."""
    request = request or SenderLearningRequest()

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
async def seed_senders(service: SenderLearningDep) -> dict[str, Any]:
    """Seed database with example senders."""
    return await service.bulk_add_senders(EXAMPLE_SENDERS)
