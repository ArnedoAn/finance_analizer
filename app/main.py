"""FastAPI lifecycle with durable inbox processing and scheduled retries."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import routes
from app.api.dependencies import cleanup_clients, get_firefly, get_llm
from app.api.routers import system
from app.core import error_log
from app.core.config import get_settings
from app.core.exceptions import FinanceAnalyzerError
from app.core.logging import get_logger, setup_logging
from app.db.database import close_db, get_db_session, init_db
from app.services import own_accounts, snapshots
from app.services.processor import Processor
from app.services.scheduler import SchedulerService

logger = get_logger(__name__)

_STATUS_CODES = {
    401: "invalid_key", 403: "insufficient_scope", 404: "not_found", 405: "method_not_allowed",
    409: "conflict", 413: "payload_too_large", 422: "validation_error", 429: "rate_limited",
    502: "upstream_unavailable", 503: "unavailable",
}


@asynccontextmanager
async def _snapshot_resources():
    async with get_db_session() as db:
        yield db, get_firefly(), get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging()
    await init_db()
    error_log.install(get_db_session)
    snapshots.enable(_snapshot_resources)
    # One-time import of OWN_ACCOUNTS into the table (only when it is empty); runs before anything reads the accounts.
    async with get_db_session() as db:
        await own_accounts.seed(db, settings)
    async with get_db_session() as db:
        try:
            await Processor(db, get_firefly(), get_llm()).sync(force=True)
        except FinanceAnalyzerError:
            logger.warning("startup_firefly_sync_unavailable")
    scheduler = SchedulerService(
        routes.run_cycle, routes.learn_senders, routes.run_reconcile,
        routes.run_digest, routes.run_weekly_digest, routes.run_digest_tick,
        routes.run_monthly_narrative, routes.run_weekly_subscriptions, settings=settings,
        # Jobs of the extra modules: {job_id: (callback, cron)}. Add the other executors' jobs here.
        extra_jobs={
            "ai_jobs_tick": (routes.run_ai_jobs_tick, "* * * * *"),  # AI job queue (peak/valley), every minute
            "forgotten_subscriptions": (routes.run_forgotten_subscriptions, "0 10 2 * *"),  # day 2, 10:00 Bogota
            "month_close": (routes.run_agent_month_close, "30 5 1 * *"),
            "weekly_coach": (routes.run_weekly_coach, "0 8 * * 0"),
            "agent_tick": (routes.run_agent_tick, "*/5 * * * *"),  # only cleans orphaned agent tasks
        },
    )
    system._scheduler = scheduler
    if settings.scheduler_enabled:
        await scheduler.start()
    try:
        yield
    finally:
        try:
            await scheduler.shutdown()
        finally:
            system._scheduler = None
            await snapshots.disable()
            await error_log.uninstall()
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

    @application.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        # Contract section 14: every error is {detail, code}. A route may choose its own
        # code by raising HTTPException(status, {"detail": "...", "code": "snake_case"});
        # otherwise the code derives from the status. Headers (Retry-After) are preserved.
        if isinstance(exc.detail, dict) and "code" in exc.detail:
            content = {"detail": exc.detail.get("detail", ""), "code": exc.detail["code"]}
        else:
            content = {"detail": exc.detail, "code": _STATUS_CODES.get(exc.status_code, "server_error" if exc.status_code >= 500 else "bad_request")}
        return JSONResponse(status_code=exc.status_code, content=content, headers=getattr(exc, "headers", None))

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'][1:]) or error['loc'][0]}: {error['msg']}" for error in exc.errors()
        )
        return JSONResponse(status_code=422, content={"detail": problems or "Invalid request", "code": "validation_error"})

    @application.exception_handler(FinanceAnalyzerError)
    async def domain_error(request: Request, exc: FinanceAnalyzerError):
        logger.warning("upstream_failure", error_type=type(exc).__name__)
        return JSONResponse(status_code=502, content={"detail": "External service unavailable", "code": "upstream_unavailable"})

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        # Keep webhook failures JSON for the durable phone outbox. Never expose
        # exception text, credentials, or request content in the response/log.
        logger.error("unhandled_request_failure", error_type=type(exc).__name__)
        error_log.record("api", f"{request.method} {request.url.path}", exc=exc)
        return JSONResponse(status_code=500, content={"detail": "Internal server error", "code": "internal_error"})

    return application


app = create_application()
