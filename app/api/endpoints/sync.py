"""
Synchronization Endpoints

Endpoints for syncing data between local cache and Firefly III.
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from app.api.dependencies import ServicesDep
from app.models.schemas import AccountType

router = APIRouter()


@router.post(
    "/all",
    summary="Sync All Data",
    description="Synchronize all accounts and categories from Firefly III.",
)
async def sync_all(services: ServicesDep) -> dict:
    """
    Sync all data from Firefly III to local cache.
    
    This updates the local cache with:
    - All accounts (asset, expense, revenue, liability)
    - All categories
    
    Returns:
        Sync results with counts.
    """
    try:
        result = await services.sync_service.sync_all()
        return {
            "status": "completed",
            "synced": result,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Sync failed: {str(e)}",
        )


@router.post(
    "/accounts",
    summary="Sync Accounts",
    description="Synchronize accounts from Firefly III.",
)
async def sync_accounts(services: ServicesDep) -> dict:
    """
    Sync accounts from Firefly III to local cache.
    
    Returns:
        Number of accounts synced.
    """
    try:
        count = await services.sync_service.sync_accounts()
        return {
            "status": "completed",
            "accounts_synced": count,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Account sync failed: {str(e)}",
        )


@router.post(
    "/categories",
    summary="Sync Categories",
    description="Synchronize categories from Firefly III.",
)
async def sync_categories(services: ServicesDep) -> dict:
    """
    Sync categories from Firefly III to local cache.
    
    Returns:
        Number of categories synced.
    """
    try:
        count = await services.sync_service.sync_categories()
        return {
            "status": "completed",
            "categories_synced": count,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Category sync failed: {str(e)}",
        )


@router.get(
    "/accounts",
    summary="Get Cached Accounts",
    description="Get accounts from local cache.",
)
async def get_cached_accounts(
    services: ServicesDep,
    account_type: Annotated[AccountType | None, Query()] = None,
) -> dict:
    """
    Get cached accounts.
    
    Args:
        account_type: Filter by account type.
        
    Returns:
        List of cached accounts.
    """
    try:
        accounts = await services.sync_service.get_cached_accounts(
            account_type.value if account_type else None
        )
        return {
            "total": len(accounts),
            "accounts": accounts,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get accounts: {str(e)}",
        )


@router.get(
    "/categories",
    summary="Get Cached Categories",
    description="Get categories from local cache.",
)
async def get_cached_categories(services: ServicesDep) -> dict:
    """
    Get cached categories.
    
    Returns:
        List of cached categories.
    """
    try:
        categories = await services.sync_service.get_cached_categories()
        return {
            "total": len(categories),
            "categories": categories,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get categories: {str(e)}",
        )


@router.get(
    "/firefly/accounts",
    summary="Get Firefly Accounts",
    description="Get accounts directly from Firefly III API.",
)
async def get_firefly_accounts(
    services: ServicesDep,
    account_type: Annotated[AccountType | None, Query()] = None,
) -> dict:
    """
    Get accounts directly from Firefly III.
    
    Args:
        account_type: Filter by account type.
        
    Returns:
        List of accounts from Firefly III.
    """
    try:
        accounts = await services.firefly.get_accounts(account_type)
        return {
            "total": len(accounts),
            "accounts": accounts,
        }
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Firefly API error: {str(e)}",
        )


@router.get(
    "/firefly/categories",
    summary="Get Firefly Categories",
    description="Get categories directly from Firefly III API.",
)
async def get_firefly_categories(services: ServicesDep) -> dict:
    """
    Get categories directly from Firefly III.
    
    Returns:
        List of categories from Firefly III.
    """
    try:
        categories = await services.firefly.get_categories()
        return {
            "total": len(categories),
            "categories": categories,
        }
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Firefly API error: {str(e)}",
        )
