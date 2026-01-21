# Finance Analyzer - AI Coding Instructions

## Architecture Overview

This is a **FastAPI microservice** that processes Gmail emails → analyzes with DeepSeek AI → creates transactions in Firefly III.

**Data Flow**: `GmailClient` → `DeepSeekClient` (AI extraction) → `SyncService` (account resolution) → `FireflyClient` → SQLite (audit/cache)

**Key Layers**:
- `app/clients/` - External API clients (Gmail, DeepSeek, Firefly) with retry logic via `tenacity`
- `app/services/` - Business logic orchestration (`EmailProcessorService` is the main coordinator)
- `app/db/repositories.py` - Repository pattern for all database access (never use raw SQLAlchemy in services)
- `app/api/dependencies.py` - FastAPI DI with singleton clients (`ServicesDep` provides all services)

## Critical Patterns

### Dependency Injection
Services receive clients via constructor injection. Use `ServicesDep` in endpoints:
```python
@router.post("/process")
async def process(services: ServicesDep):
    await services.email_processor.process_batch(request)
```

### Pydantic Schemas
All data models are in `app/models/schemas.py`. Use `ConfigDict(frozen=True)` for immutable message types. Key schemas:
- `EmailMessage` - Gmail email with `idempotency_key` property
- `TransactionAnalysis` - AI-extracted data with validators
- Enums: `TransactionType` (withdrawal/deposit/transfer), `ProcessingStatus`

### Error Handling
Custom exceptions in `app/core/exceptions.py` extend `FinanceAnalyzerError`. Client-specific hierarchies:
- `GmailError` → `GmailAuthenticationError`, `GmailFetchError`
- `DeepSeekError` → `DeepSeekAPIError`, `DeepSeekRateLimitError`
- `FireflyError` → `FireflyDuplicateError`, `FireflyValidationError`

### Idempotency
Email deduplication uses `message_id + internal_id` stored in `ProcessedEmail` table. Check before processing:
```python
if await self._processed_repo.exists(email.message_id, email.internal_id):
    return  # Skip duplicate
```

### Logging
Use `structlog` via `app/core/logging.py`. Always use structured logging:
```python
from app.core.logging import get_logger
logger = get_logger(__name__)
logger.info("event_name", key1=value1, key2=value2)
```

## Developer Commands

```bash
# Run locally
uvicorn app.main:app --reload --port 8000

# Run tests (async mode auto-configured)
pytest tests/ -v

# Docker development
docker-compose -f docker-compose.dev.yml up

# Lint & format
ruff check app/ --fix
black app/
```

## Configuration

All settings via `app/core/config.py` using Pydantic Settings. Required env vars:
- `TOKEN_ENCRYPTION_KEY` - For OAuth token encryption
- `DEEPSEEK_API_KEY` - AI API key
- `FIREFLY_BASE_URL`, `FIREFLY_API_TOKEN` - Firefly III connection

Secrets use `SecretStr` - access via `.get_secret_value()`, never log raw values.

## Database

SQLite with async SQLAlchemy (`aiosqlite`). Models in `app/db/models.py`:
- `ProcessedEmail` - Idempotency tracking
- `AuditLog` - Full processing trail (JSON columns for `analysis_result`, `error_details`)
- `AccountCache`, `CategoryCache` - Local Firefly data cache
- `KnownSender` - Learned financial email senders

Always use repositories from `app/db/repositories.py`, not direct ORM access.

## AI Integration

DeepSeek prompts in `app/clients/deepseek.py` are optimized for token efficiency:
- `SYSTEM_PROMPT_TRANSACTION` - Extracts structured transaction data
- `SYSTEM_PROMPT_SENDER_LEARNING` - Identifies financial senders

AI responses force JSON mode. Output parsed into `TransactionAnalysis` schema with validation.

## Testing

Tests use in-memory SQLite. Fixtures in `tests/conftest.py`:
- `db_session` - Fresh database per test
- `client` - AsyncClient for API tests
- `sample_email_data` - Test email fixture

Use `pytest-asyncio` with `asyncio_mode = "auto"` (configured in pyproject.toml).
