"""
API Routes Configuration

Combines all route modules into a single API router.
"""

from fastapi import APIRouter

from app.api.endpoints import (
    auth,
    emails,
    health,
    notifications,
    processing,
    scheduler,
    senders,
    sync,
)

api_router = APIRouter()

# Include all endpoint routers
api_router.include_router(
    health.router,
    prefix="/health",
    tags=["Health"],
)

api_router.include_router(
    auth.router,
    prefix="/auth",
    tags=["Authentication"],
)

api_router.include_router(
    emails.router,
    prefix="/emails",
    tags=["Emails"],
)

api_router.include_router(
    processing.router,
    prefix="/processing",
    tags=["Processing"],
)

api_router.include_router(
    sync.router,
    prefix="/sync",
    tags=["Synchronization"],
)

api_router.include_router(
    senders.router,
    tags=["Senders"],
)

api_router.include_router(
    scheduler.router,
    tags=["Scheduler"],
)

api_router.include_router(
    notifications.router,
    prefix="/notifications",
    tags=["Notifications"],
)
