"""FastAPI lifecycle with durable inbox processing and scheduled retries."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import routes
from app.api.dependencies import cleanup_clients, get_firefly, get_llm
from app.core.config import get_settings
from app.core.exceptions import FinanceAnalyzerError
from app.core.logging import get_logger, setup_logging
from app.db.database import close_db, get_db_session, init_db
from app.services.processor import Processor
from app.services.scheduler import SchedulerService

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging()
    await init_db()
    async with get_db_session() as db:
        try:
            await Processor(db, get_firefly(), get_llm()).sync(force=True)
        except FinanceAnalyzerError:
            logger.warning("startup_firefly_sync_unavailable")
    scheduler = SchedulerService(
        routes.run_cycle, routes.learn_senders, routes.run_reconcile,
        routes.run_digest, routes.run_weekly_digest, settings=settings,
    )
    routes._scheduler = scheduler
    if settings.scheduler_enabled:
        await scheduler.start()
    try:
        yield
    finally:
        try:
            await scheduler.shutdown()
        finally:
            routes._scheduler = None
            await cleanup_clients()
            await close_db()


def create_application() -> FastAPI:
    # Fail closed at startup/import if either required API credential is invalid.
    settings = get_settings()
    expose_docs = not settings.is_production or settings.expose_docs
    application = FastAPI(
        title="Finance Analyzer", version="3.0.0", lifespan=lifespan,
        docs_url="/docs" if expose_docs else None,
        redoc_url="/redoc" if expose_docs else None,
        openapi_url="/openapi.json" if expose_docs else None,
    )
    application.include_router(routes.router, prefix="/api/v1")

    @application.exception_handler(FinanceAnalyzerError)
    async def domain_error(request: Request, exc: FinanceAnalyzerError):
        logger.warning("upstream_failure", error_type=type(exc).__name__)
        return JSONResponse(status_code=502, content={"detail": "External service unavailable"})

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        # Keep webhook failures JSON for the durable phone outbox. Never expose
        # exception text, credentials, or request content in the response/log.
        logger.error("unhandled_request_failure", error_type=type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    return application


app = create_application()
