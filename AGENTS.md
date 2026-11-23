# Finance Analyzer v3 — repository contract

Single-owner Colombia service on main; Python 3.12+, FastAPI, async SQLAlchemy, Firefly III ledger. Read README.md and docs/enfoques.md. Never restore sessions, multi-tenant, OAuth or the obsolete individual branch.

## Boundaries

- app/core: settings, file-backed secrets, timezone/logging/errors. FINANCE_API_KEY required (>=32 characters); PHONE_API_KEY optional legacy ingest key (>=32 characters when set, distinct from the admin key); both and DATABASE_URL support _FILE.
- app/db: inbox, IMAP checkpoints, source rules, audit events, cycle runs and notification throttle. Repositories are the data-access boundary. Alembic owns schema; init_db must not create/alter tables.
- app/clients: Firefly, OpenAI-compatible LLM, read-only header-first IMAP.
- app/parsers: common immutable Extraction plus one module per bank; Decimal money and anchored dates.
- app/services: persist before network; durable claims, idempotent external_id adoption, review, reconciliation, backoff, alerts (`alerts` table polled by the app via GET /alerts) and scheduler leadership.
- app/api: /api/v1, X-API-Key except health/live and health/ready and the code-authenticated POST devices/enroll. Keys are per device (`cofre_` + token_urlsafe(32)); only their sha256 hex is stored (table devices, unique index) with scopes ingest, read, review, admin (admin implies all, review implies read, ingest only webhook and auth/check). Revoked or expired devices get 401; last_used_at is written at most once a minute per device. FINANCE_API_KEY is the server-side admin (device_id None). PHONE_API_KEY is optional and legacy: its first valid use registers it as the ingest device `phone-legacy`; retire it by revoking that device and removing the variable. Production docs and raw inbox text (include_raw=true) are explicit opt-ins.

Use aware UTC internally and America/Bogota for Firefly. Never log credentials or financial bodies. Own accounts live in the `own_accounts` table, managed through /api/v1/own-accounts; read them only through `app.services.own_accounts.load` (per-process snapshot, invalidated after each committed write, one uvicorn worker), never from `Settings.own_accounts`, which OWN_ACCOUNTS only uses as a one-time startup seed into an empty table. OWN_ACCOUNTS replaces ACCOUNT_MAP: unresolved bank account goes to needs_review, never a silent cash default. Never create own accounts. Never merge identical purchases from the same channel. Cross-source/manual ambiguity requires review. Learned sources require explicit approval and aligned mail authentication by default.

LLM_AUTO_APPROVE_MAX_COP, SMS_AUTO_APPROVE_MAX_COP and NOTIFICATION_AUTO_APPROVE_MAX_COP default to 0 (human review); raising them is a deliberate owner decision. INGEST_MAX_EVENTS_PER_HOUR (default 120) caps ingest events per hour with an in-process counter, which is only correct with the single uvicorn worker. SMS sender IDs are spoofable, not cryptographic bank authentication. OTP bodies must not reach storage/LLM. Alerts contain amounts and merchants but never raw financial bodies or credentials.

Container runs alembic upgrade head before one uvicorn worker. Preserve existing v2 database and volume during upgrade; baseline supports empty and v2 DB, rejects v1. Do not stamp past migrations or import mutable runtime models into revision definitions. Schema changes require a new revision; never rewrite an applied baseline.

Pin every CI action to a verified upstream commit and Docker base images to verified registry multiarch digests. Give each job only required permissions; publishing/deployment require refs/heads/main even for workflow_dispatch. CI requires distinct valid long admin/phone keys.

## Verification

Use uv sync --frozen --extra dev, then uv run --frozen ruff check app tests alembic and uv run --frozen pytest only when assigned verification. CI tests SQLite and disposable PostgreSQL via TEST_DATABASE_URL. No live services implicitly. Update uv.lock with uv lock on dependency changes. Update README/examples when contracts change; never commit .env, DBs, tokens or virtual environments.
