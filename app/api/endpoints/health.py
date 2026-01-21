"""
Health Check Endpoints

Provides health status and connectivity checks for all external services.
"""

from fastapi import APIRouter, HTTPException

from app import __version__
from app.api.dependencies import ServicesDep
from app.core.config import get_settings
from app.models.schemas import HealthCheck

router = APIRouter()


@router.get(
    "",
    response_model=HealthCheck,
    summary="Health Check",
    description="Check application health and service connectivity.",
)
async def health_check(services: ServicesDep) -> HealthCheck:
    """
    Perform health check on all services.
    
    Returns:
        HealthCheck with status of each service.
    """
    settings = get_settings()
    
    # Check service connectivity
    gmail_ok = False
    deepseek_ok = False
    firefly_ok = False
    
    try:
        gmail_ok = await services.gmail.check_connection()
    except Exception:
        pass
    
    try:
        deepseek_ok = await services.deepseek.check_connection()
    except Exception:
        pass
    
    try:
        firefly_ok = await services.firefly.check_connection()
    except Exception:
        pass
    
    # Determine overall status
    all_services_ok = gmail_ok and deepseek_ok and firefly_ok
    status = "healthy" if all_services_ok else "degraded"
    
    return HealthCheck(
        status=status,
        version=__version__,
        environment=settings.app_env,
        services={
            "gmail": gmail_ok,
            "deepseek": deepseek_ok,
            "firefly": firefly_ok,
        },
    )


@router.get(
    "/live",
    summary="Liveness Check",
    description="Simple liveness probe for container orchestration.",
)
async def liveness() -> dict[str, str]:
    """Simple liveness check."""
    return {"status": "alive"}


@router.get(
    "/ready",
    summary="Readiness Check",
    description="Check if the application is ready to accept traffic.",
)
async def readiness(services: ServicesDep) -> dict[str, str]:
    """
    Check if all required services are connected.
    
    Raises:
        HTTPException: If any service is not ready.
    """
    # Check Gmail authentication
    try:
        gmail_ok = await services.gmail.check_connection()
        if not gmail_ok:
            raise HTTPException(503, "Gmail not connected")
    except Exception as e:
        raise HTTPException(503, f"Gmail error: {str(e)}")
    
    # Check Firefly
    try:
        firefly_ok = await services.firefly.check_connection()
        if not firefly_ok:
            raise HTTPException(503, "Firefly III not connected")
    except Exception as e:
        raise HTTPException(503, f"Firefly error: {str(e)}")
    
    return {"status": "ready"}
