"""
Finance Analyzer - Main Application

FastAPI application for automated financial transaction processing.
Processes emails from Gmail, analyzes with AI, and creates transactions in Firefly III.
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api.dependencies import cleanup_clients, get_gmail_client
from app.api.routes import api_router
from app.core.config import get_settings
from app.core.exceptions import FinanceAnalyzerError
from app.core.logging import get_logger, setup_logging
from app.db.database import close_db, init_db
from app.services.scheduler import SchedulerService

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan manager.
    
    Handles startup and shutdown events.
    """
    # Startup
    setup_logging()
    logger.info("application_starting", version=__version__)
    
    # Initialize database
    await init_db()
    logger.info("database_initialized")
    
    # Pre-authenticate Gmail if credentials exist
    settings = get_settings()
    if settings.google_token_path.exists():
        try:
            gmail = get_gmail_client()
            await gmail.authenticate()
            logger.info("gmail_pre_authenticated")
        except Exception as e:
            logger.warning("gmail_pre_auth_failed", error=str(e))
    
    # Start scheduler if enabled
    scheduler_service = SchedulerService()
    if settings.scheduler_enabled:
        scheduler_service.start()
        logger.info(
            "scheduler_started",
            processing_cron=settings.scheduler_processing_cron,
            learning_cron=settings.scheduler_learning_cron,
        )
    
    yield
    
    # Shutdown
    logger.info("application_shutting_down")
    scheduler_service.stop()
    await cleanup_clients()
    await close_db()
    logger.info("application_stopped")


def create_application() -> FastAPI:
    """
    Create and configure the FastAPI application.
    
    Returns:
        Configured FastAPI instance.
    """
    settings = get_settings()
    
    app = FastAPI(
        title="Finance Analyzer",
        description="""
        Automated Financial Transaction Processing System
        
        This API automates the creation of transactions in Firefly III
        from Gmail emails using AI-powered semantic analysis.
        
        ## Features
        
        - **Gmail Integration**: OAuth 2.0 authenticated email fetching
        - **AI Analysis**: DeepSeek-powered transaction extraction
        - **Firefly III Sync**: Automatic account, category, and transaction creation
        - **Audit Trail**: Complete logging of all processing activities
        - **Idempotency**: Duplicate email detection and prevention
        
        ## Workflow
        
        1. Fetch emails matching financial patterns from Gmail
        2. Analyze each email with DeepSeek AI to extract transaction data
        3. Resolve/create accounts and categories in Firefly III
        4. Create the transaction with all metadata
        5. Log the result for audit purposes
        """,
        version=__version__,
        # Enable docs also in production
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    
    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.debug else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # Global exception handler
    @app.exception_handler(FinanceAnalyzerError)
    async def finance_analyzer_exception_handler(
        request: Request,
        exc: FinanceAnalyzerError,
    ) -> JSONResponse:
        """Handle application-specific exceptions."""
        logger.error(
            "application_error",
            error_type=type(exc).__name__,
            message=exc.message,
            details=exc.details,
        )
        return JSONResponse(
            status_code=500,
            content=exc.to_dict(),
        )
    
    @app.exception_handler(Exception)
    async def general_exception_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        """Handle unexpected exceptions."""
        logger.exception("unexpected_error", error=str(exc))
        return JSONResponse(
            status_code=500,
            content={
                "error": "InternalServerError",
                "message": "An unexpected error occurred",
                "details": {"error": str(exc)} if settings.debug else {},
            },
        )
    
    # Include API routes
    app.include_router(api_router, prefix="/api/v1")
    
    # Root endpoint
    @app.get("/", tags=["Root"])
    async def root() -> dict:
        """Root endpoint with API information."""
        return {
            "name": "Finance Analyzer",
            "version": __version__,
            "docs": "/docs",
            "api": "/api/v1",
            "health": "/api/v1/health",
        }
    
    return app


# Create application instance
app = create_application()


if __name__ == "__main__":
    import uvicorn
    
    settings = get_settings()
    
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )
