# PROJECT KNOWLEDGE BASE

**Generated:** 2026-08-16
**Commit:** 360c2f9
**Branch:** main

## OVERVIEW

FastAPI microservice automating financial transactions. Inputs: Gmail emails + phone push notifications. Pipeline: DeepSeek AI extraction → Firefly III transaction sync. Multi-tenant via custom per-session header routing. Python 3.11+, async SQLAlchemy (SQLite dev / PostgreSQL prod), structlog, APScheduler for background CRON.

## STRUCTURE

```
finance_analizer/
├── app/
│   ├── main.py                 # FastAPI bootstrap, lifespan, scheduler init
│   ├── api/
│   │   ├── routes.py           # Router aggregation (single mount point)
│   │   ├── dependencies.py     # SessionDep, ServicesDep, singleton clients
│   │   └── endpoints/          # 8 router modules — see subdir AGENTS.md
│   ├── clients/                # Gmail/DeepSeek/Firefly HTTP — see subdir AGENTS.md
│   ├── core/                   # config, session, security, logging, timezone — subdir AGENTS.md
│   ├── db/                     # async SQLAlchemy + 10-repository pattern — subdir AGENTS.md
│   ├── models/
│   │   └── schemas.py          # Pydantic v2: TransactionAnalysis, EmailMessage, enums
│   └── services/               # Orchestration: Email/Notification/Sync/Transaction/Sender/Scheduler — subdir AGENTS.md
├── tests/                      # 8 test files, asyncio_mode=auto, in-memory SQLite
├── scripts/                    # debug_firefly.py, list_accounts.py (manual utils)
├── docs/                       # omo-model-config.md, processing_async_polling.md
├── credentials/                # gitignored OAuth token JSON (encrypted at rest)
├── data/                       # SQLite db (gitignored)
├── .github/workflows/          # deploy.yml (manual custom-registry), deploy-ghcr.yml (push to main → GHCR)
└── Dockerfile (prod), Dockerfile.dev (reload+debug 5678), Dockerfile.prod (param base), 3 docker-compose.*.yml
```

## WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Add API endpoint | `app/api/endpoints/<feature>.py` + register in `app/api/routes.py` | Use `ServicesDep` for full DI, or `SessionDep` + `get_db_session` for manual |
| Session routing / multi-user | `app/core/session.py` + `app/api/dependencies.py` | 6-level priority: X-Telegram-Session → X-Telegram-User-ID(+Chat) → X-User-Id → X-Session-ID → cookie → generated uuid4 |
| Settings / env var | `app/core/config.py` `Settings` (lru_cached) | Pydantic Settings + SecretStr |
| Token encryption | `app/core/security.py` `TokenEncryption` (Fernet singleton) | `_create_fernet` derives key via SHA-256 of `TOKEN_ENCRYPTION_KEY` |
| Email processing | `app/services/email_processor.py` `EmailProcessorService` | `process_batch` (214-line orchestrator), `process_single_email` (177 lines) |
| Notification webhook | `app/services/notification_processor.py` `process_notification` | 265-line method, 3 duplicate audit-log-and-commit blocks |
| Account resolution | `app/services/sync_service.py` `resolve_source_account` | 288-line 7-tier fallback cascade — highest-complexity method |
| Background scheduler | `app/services/scheduler.py` `SchedulerService` | Multi-session fan-out via `_discover_active_session_ids` (UNION ALL across 9 tables) |
| DB access | `app/db/repositories.py` | 10 repository classes, repository pattern mandatory |
| Idempotency | `ProcessedEmailRepository`, `TransactionFingerprintRepository` | message_id+internal_id; cross-channel SHA-256(amount:date:account) |
| Live Firefly test | `tests/test_firefly_integration.py` | Skipped unless env configured |
| Webhook session override | `app/api/endpoints/notifications.py` + `resolve_webhook_session_id` | Body `user_id` overrides ambient session (for clients w/o header support) |
| DeepSeek AI prompt | `app/clients/deepseek.py` `SYSTEM_PROMPT_TRANSACTION`, `SYSTEM_PROMPT_SENDER_LEARNING` | JSON-forced output via `response_format={"type": "json_object"}` |
| Colombia TZ | `app/core/timezone.py` + `transaction_service._build_notes` | UTC→America/Bogota (UTC-5 fixed offset) |
| Production deploy | `.github/workflows/deploy-ghcr.yml` | Push to main → GHCR build → Coolify webhook. **No lint/test step in CI** |

## CODE MAP

| Symbol | Type | File | Role |
|-------|------|------|------|
| `create_application` | function | app/main.py | FastAPI factory, lifespan, exception handlers |
| `app` | ASGI instance | app/main.py | Single export — `uvicorn app.main:app` |
| `Settings` | class | app/core/config.py:16 | Pydantic Settings, `get_settings()` lru_cached |
| `ServicesDep` | dataclass dep | app/api/dependencies.py | Payload w/ all clients+services per request |
| `SessionDep` | dep | app/api/dependencies.py | `RequestSessionContext` from 6-level header chain |
| `EmailProcessorService` | class | app/services/email_processor.py | Email → AI → Firefly (667 lines) |
| `NotificationProcessorService` | class | app/services/notification_processor.py | Webhook pipeline (437 lines) |
| `SchedulerService` | class | app/services/scheduler.py | CRON, multi-session fan-out (319 lines) |
| `SyncService.resolve_source_account` | method | app/services/sync_service.py:97 | 288-line account resolution cascade |
| `TransactionService.create_from_analysis` | method | app/services/transaction_service.py | Build Firefly transaction from Pydantic analysis |
| `TokenEncryption` | class | app/core/security.py:21 | Fernet encrypt/decrypt for OAuth tokens |
| `parse_oauth_state_payload` | function | app/core/session.py:222 | HMAC-signed state (session_id + PKCE verifier + iat) |
| `FireflyClient` / `GmailClient` / `DeepSeekClient` | class | app/clients/ | HTTP clients w/ tenacity retry |
| 10 × `*Repository` | class | app/db/repositories.py | Repository pattern for all DB access |
| `EmailMessage` / `TransactionAnalysis` | pydantic | app/models/schemas.py | Core domain schemas |

## CONVENTIONS

- All code under `app/` (hatchling wheel target). No `src/` layout.
- Async-first: AsyncSession, httpx.AsyncClient, FastAPI.
- **Repository pattern mandatory** — services receive `AsyncSession`, build repositories. NEVER raw `.execute()` in services.
- **Session isolation everywhere** — every repo `__init__(session, session_id=DEFAULT_SESSION_ID)` filters queries. All tables carry `session_id`.
- Settings via `get_settings()` only. Never `Settings()` directly.
- Logging via `from app.core.logging import get_logger`. Structured: `logger.info("event_name", k=v)`.
- DTOs in `app/models/schemas.py`. Pydantic v2. `ConfigDict(frozen=True)` for immutable messages.
- Errors rooted in `app/core/exceptions.py` `FinanceAnalyzerError`. Domain-specific (`GmailError`, `DeepSeekRateLimitError`, `FireflyDuplicateError`). Global handler in `app/main.py`.
- Retries via `tenacity` in clients (exponential backoff for DeepSeek).
- Token files: `google_token_<session_id>.json` in `credentials/` (Fernet-encrypted).
- Lint: black + ruff `line-length=100` (select E/F/I/N/W/UP, ignore E501 — length enforced by black only). Type: mypy strict, `ignore_missing_imports=true`.
- Python 3.11+ required.

## ANTI-PATTERNS (THIS PROJECT)

- **No raw SQLAlchemy in services** — always via repository. Known violation: `sender_learning.get_all_senders(include_inactive=True)` does raw ORM (fix needed).
- **Never log `SecretStr` raw** — use `.get_secret_value()` only for passing to client.
- **Never instantiate Gmail/Firefly clients per request** — use per-session singletons from `app/api/dependencies.py`. DeepSeek is global singleton.
- **Never forge OAuth state** — always via `build_oauth_state_payload`. `parse_oauth_state_payload` rejects bad HMAC signatures / expired state (> `oauth_state_max_age_seconds`).
- **No `session.commit()` inside services** — repos use `flush`, commit is caller's job. Known exception: `sender_learning.learn_from_recent_emails:168`.
- **No `as any` / `# type: ignore` / `@ts-ignore`** — mypy strict enforced.
- **No `pytest.mark.asyncio` boilerplate** — `asyncio_mode = "auto"` in pyproject.toml.
- **Don't add top-level dirs** without `.gitignore` update — `data/`, `logs/`, `credentials/`, `.codegraph/`, `.omo/` all ignored.

## UNIQUE STYLES

- **Multi-tenant via header routing, NOT URL paths** — single FastAPI instance, sessions resolved from 6-level priority header chain. No `/v1/users/{id}/...`.
- **Webhook body-session override** — `POST /notifications/webhook` reads `payload.user_id` for clients unable to send headers, resolves `usr_<sha256[:40]>` session. See `resolve_webhook_session_id`.
- **Cross-channel transaction dedup** — `TransactionFingerprintRepository.compute_hash` (SHA-256 of `amount:date:account`, COP rounded to integer) prevents email + notification from creating duplicate transactions.
- **Scheduler multi-session fan-out** — `_discover_active_session_ids` runs `UNION ALL` across 9 tables + globs `credentials/google_token_<id>.json` paths. Per-tick iteration constructs fresh service instances per session, bypassing FastAPI DI entirely.
- **OAuth state with embedded + signed session_id** — `build_oauth_state_payload` signs `{sid, iat, cv?}` via HMAC-SHA256 of `TOKEN_ENCRYPTION_KEY + b":oauth_state"`.
- **AI JSON-forced output** — DeepSeek uses `response_format={"type": "json_object"}` + Pydantic validation of `TransactionAnalysis`.
- **Colombian context hardcoded** — `DEFAULT_CURRENCY=COP`, `TZ=America/Bogota`, `GMAIL_SUBJECT_FILTERS=Factura,Pago,Recibo,Compra,Transferencia`, 14 seeded Colombian financial senders in `POST /senders/seed`.

## COMMANDS

```bash
# Dev
uvicorn app.main:app --reload --port 8000

# Tests (asyncio_mode=auto, in-memory SQLite, no external deps)
pytest
pytest --cov=app

# Lint + format
ruff check app/ --fix
black app/
mypy app/ --strict

# Docker dev (hot-reload + debug 5678)
docker-compose -f docker-compose.dev.yml up

# Docker prod (prebuilt image)
docker-compose -f docker-compose.prod.yml up -d

# First-run
pip install -e ".[dev]"
cp .env.example .env
# Fill: TOKEN_ENCRYPTION_KEY (openssl rand -hex 32), DEEPSEEK_API_KEY, FIREFLY_API_TOKEN, FIREFLY_BASE_URL
# Place Gmail OAuth JSON at credentials/google_credentials.json
```

## NOTES

- **README out-of-date** — actual code: 8 endpoint modules, 7 service modules, 6 core modules. README tree shows 5/3/4. Trust `app/` not README.
- **No CI lint/test gate** — `deploy-ghcr.yml` only builds & pushes image. Quality is local responsibility. `pre-commit` dep declared but `.pre-commit-config.yaml` missing.
- **No alembic migrations despite dep** — `init_db()` uses `Base.metadata.create_all()`. Schema changes need manual migration or drop-the-db reset.
- **3 redundant Dockerfiles** — `Dockerfile` and `Dockerfile.prod` differ only by unused `ARG REGISTRY_URL` + README copy. Consolidate when touching.
- **`ProcessingJobRepository` looks unused** — no service references found. Verify before removing.
- **Primary refactor targets**:
  - `SyncService.resolve_source_account` — 288 lines, 5-tier cascade, duplicated auto-create try/except (lines 210–244 and 265–300).
  - `NotificationProcessorService.process_notification` — 265 lines, 3 duplicated audit-log-and-commit blocks.
  - `AccountCacheRepository.get_by_partial_name` — loads ALL accounts into Python and matches in-memory. Should be SQL LIKE.
- **Prod DB URL hardcodes internal hostname** (`postgres`) — only works inside Docker network.
- **TZ hardcoded** to `America/Bogota` in all 3 compose files — should be env-configurable.
- **Test coverage gaps**: `email_processor.py`, `notification_processor.py`, `scheduler.py`, `sender_learning.py`, `clients/deepseek.py`, `clients/gmail.py`, most endpoints → 0 tests. Covered: schemas, repositories, sync_service, transaction_service, auth endpoints, firefly live integration.
- **Codegraph index present** at `.codegraph/` — use `codegraph_explore` for symbol source + call graph.

---

# Communication Rules (AI agents)

Respond terse like smart caveman. All technical substance stay. Only fluff die.

Rules:
- Drop: articles (a/an/the), filler (just/really/basically), pleasantries, hedging
- Fragments OK. Short synonyms. Technical terms exact. Code unchanged.
- Pattern: [thing] [action] [reason]. [next step].
- Not: "Sure! I'd be happy to help you with that."
- Yes: "Bug in auth middleware. Fix:"

Switch level: /caveman lite|full|ultra|wenyan-lite|wenyan-full|wenyan-ultra
Stop: "stop caveman" or "normal mode"

Auto-Clarity: drop caveman for security warnings, irreversible actions, user confused. Resume after.

Boundaries: code/commits/PRs written normal.
