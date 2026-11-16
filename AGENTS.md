# Finance Analyzer v3 — repository contract

Single-owner Colombia service on main; Python 3.12+, FastAPI, async SQLAlchemy, Firefly III ledger. Read README.md and docs/enfoques.md. Never restore sessions, multi-tenant, OAuth or the obsolete individual branch.

## Boundaries

- app/core: settings, file-backed secrets, timezone/logging/errors. FINANCE_API_KEY and distinct PHONE_API_KEY required, each >=32 characters; both and DATABASE_URL support _FILE.
- app/db: inbox, IMAP checkpoints, source rules, audit events, cycle runs and notification throttle. Repositories are the data-access boundary. Alembic owns schema; init_db must not create/alter tables.
- app/clients: Firefly, OpenAI-compatible LLM, read-only header-first IMAP.
- app/parsers: common immutable Extraction plus one module per bank; Decimal money and anchored dates.
- app/services: persist before network; durable claims, idempotent external_id adoption, review, reconciliation, backoff, notifications and scheduler leadership.
- app/api: /api/v1, X-API-Key except health/live and health/ready. PHONE_API_KEY is ingestion-only (/notifications/webhook, /auth/check); private/admin routes require FINANCE_API_KEY. Production docs and raw inbox text (include_raw=true) are explicit opt-ins.

Use aware UTC internally and America/Bogota for Firefly. Never log credentials or financial bodies. OWN_ACCOUNTS replaces ACCOUNT_MAP: unresolved bank account goes to needs_review, never a silent cash default. Never create own accounts. Never merge identical purchases from the same channel. Cross-source/manual ambiguity requires review. Learned sources require explicit approval and aligned mail authentication by default.

LLM_AUTO_APPROVE_MAX_COP defaults to 0 (human review); SMS_AUTO_APPROVE_MAX_COP defaults to 2000000, with 0 requiring review for all SMS. SMS sender IDs are spoofable, not cryptographic bank authentication. OTP bodies must not reach storage/LLM. Telegram bot messages are not end-to-end encrypted; never include raw financial bodies or credentials.

Container runs alembic upgrade head before one uvicorn worker. Preserve existing v2 database and volume during upgrade; baseline supports empty and v2 DB, rejects v1. Do not stamp past migrations or import mutable runtime models into revision definitions. Schema changes require a new revision; never rewrite an applied baseline.

Pin every CI action to a verified upstream commit and Docker base images to verified registry multiarch digests. Give each job only required permissions; publishing/deployment require refs/heads/main even for workflow_dispatch. CI requires distinct valid long admin/phone keys.

## Verification

Use uv sync --frozen --extra dev, then uv run --frozen ruff check app tests alembic and uv run --frozen pytest only when assigned verification. CI tests SQLite and disposable PostgreSQL via TEST_DATABASE_URL. No live services implicitly. Update uv.lock with uv lock on dependency changes. Update README/examples when contracts change; never commit .env, DBs, tokens or virtual environments.
