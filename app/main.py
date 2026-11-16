"""FastAPI lifecycle with durable inbox processing and scheduled retries."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import routes
from app.api.dependencies import cleanup_clients
from app.core.config import get_settings
from app.core.exceptions import FinanceAnalyzerError
from app.core.logging import get_logger, setup_logging
from app.db.database import close_db, get_db_session, init_db
from app.db.repositories import KnownSenderRepository
from app.services.scheduler import SchedulerService

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging()
    await init_db()
    async with get_db_session() as db:
        await KnownSenderRepository(db).seed()
    scheduler = SchedulerService(routes.run_cycle, routes.learn_senders, settings=settings)
    routes._scheduler = scheduler
    if settings.scheduler_enabled:
        scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()
        routes._scheduler = None
        await cleanup_clients()
        await close_db()


def create_application() -> FastAPI:
    # Fail closed at startup/import if the owner's API key is absent.
    get_settings()
    application = FastAPI(title="Finance Analyzer", version="2.0.0", lifespan=lifespan)
    application.include_router(routes.router, prefix="/api/v1")

    @application.exception_handler(FinanceAnalyzerError)
    async def domain_error(request: Request, exc: FinanceAnalyzerError):
        logger.warning("upstream_failure", error_type=type(exc).__name__)
        return JSONResponse(status_code=502, content={"detail": "External service unavailable"})

    return application


app = create_application()
