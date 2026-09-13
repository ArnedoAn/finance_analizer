# app/db/

Async SQLAlchemy 2.0 data layer. SQLite dev / PostgreSQL 16 prod. **Repository pattern is the only allowed data-access path** — services never use raw SQLAlchemy.

## FILE MAP

| File | Role |
|------|------|
| `database.py` | Async engine, `init_db`, `close_db`, `db_session_factory`, `get_db_session` context manager. Schema bootstrap via `Base.metadata.create_all()` (no Alembic). |
| `models.py` | SQLAlchemy ORM models — all tables carry `session_id` column for multi-tenant isolation. |
| `repositories.py` | **974 lines** — 10 repository classes, single file. The data-access layer. |
| `__init__.py` | (empty package marker) |

## REPOSITORY INVENTORY

| Repository | Lines | Methods | Backing table |
|------------|-----:|---------|---------------|
| `ProcessedEmailRepository` | 42–112 | `exists`, `mark_processed`, `get_processed_ids`, `clear_all` | `processed_emails` |
| `AuditLogRepository` | 115–217 | `create`, `update_status`, `get_by_email`, `get_recent`, `get_statistics` | `audit_logs` |
| `AccountCacheRepository` | 220–360 | `get_by_name`, `get_by_partial_name`, `get_by_firefly_id`, `upsert`, `get_all`, `sync_from_firefly` | `account_cache` |
| `CategoryCacheRepository` | 363–432 | `get_by_name`, `get_by_firefly_id`, `upsert`, `get_all`, `sync_from_firefly` | `category_cache` |
| `TagCacheRepository` | 435–475 | `get_by_tag`, `upsert`, `get_all` | `tag_cache` |
| `KnownSenderRepository` | 478–593 | `get_by_keyword`, `find_by_email`, `get_all_active`, `get_all_keywords`, `add_sender`, `update_match_count`, `deactivate_sender`, `exists`, `bulk_add` | `known_senders` |
| `SchedulerJobLogRepository` | 596–704 | `create`, `complete`, `fail`, `get_last_run`, `get_recent` | `scheduler_job_logs` |
| `ProcessedNotificationRepository` | 707–776 | `exists`, `mark_processed`, `get_recent`, `get_count` | `processed_notifications` |
| `ProcessingJobRepository` | 779–867 | `create`, `mark_running`, `mark_completed`, `mark_failed`, `get_by_id` | `processing_jobs` (**looks unused — verify before deleting**) |
| `TransactionFingerprintRepository` | 870–974 | `find_duplicate`, `create`, `compute_hash` (static) | `transaction_fingerprints` |

## WHERE TO LOOK

| Task | Location |
|------|----------|
| Add new table | `app/db/models.py` (add ORM class, add `session_id` column unless global) + add new repo class in `repositories.py` |
| Add new query method | find repo class for table, append method, take `AsyncSession` only — no commit |
| Change idempotency logic | `ProcessedEmailRepository.exists` (message_id+internal_id) for emails; `ProcessedNotificationRepository.exists` (notification_hash) for notifications; `TransactionFingerprintRepository.find_duplicate` (SHA-256 hash + time window) for cross-channel dedup |
| Add new cache table to mirror Firefly | see `AccountCacheRepository.sync_from_firefly` as template |
| Migrate schema (no Alembic) | change `models.py`, restart — `init_db()` calls `create_all()`. Existing columns are NOT altered automatically. Manual SQL or DB reset required. |

## CONVENTIONS (this dir only)

- **All repo `__init__(self, session: AsyncSession, session_id: str = DEFAULT_SESSION_ID)`** — validate + normalize session_id, raise `ValueError` on garbage. Every filter appends `.where(Model.session_id == self._session_id)`.
- **Repos use `session.flush()`, not `session.commit()`** — commit is the service/caller's responsibility. Documented exceptions in services elsewhere.
- **Pydantic input → SQLAlchemy row conversion** happens in repo (`upsert(...).values(**model.model_dump())`), never in services.
- `compute_hash` is a **static method** on `TransactionFingerprintRepository` — call via class, not instance, when computing from analysis.
- `find_duplicate` takes `window_hours: int` — defaults to small window; check call-site when extending.

## ANTI-PATTERNS

- **Don't reach `session.execute()` directly from a service** — wrap it in a repo method. Known violation (in services, not here): `sender_learning.get_all_senders(include_inactive=True)`.
- **Don't add a `commit=True` flag to repo methods** — services own the transaction. If a bulk op needs commit mid-method, that's a service-layer responsibility.
- **Don't write SQL without `session_id` filter** — every multi-tenant table carries it. Global tables (none currently) must be explicitly documented.
- **Don't compute `transaction_fingerprint` hash inline** — call `TransactionFingerprintRepository.compute_hash(amount=..., date=..., account=...)` (it normalizes COP to integer via `Decimal` rounding).
- **Don't assume `AccountCacheRepository.get_by_partial_name` is fast** — it loads ALL rows of a type into Python and matches in-memory (3-tier: exact → contains → reverse-contains). Should be SQL LIKE; flagged for refactor.

## NOTES

- `init_db()` also runs a one-off column migration: `ALTER TABLE ... ADD COLUMN session_id` wrapped in try/except (idempotent, swallowed on repeat). See `database.py` for the migration block.
- `get_db_session` is exposed as a FastAPI dependency (`app/api/dependencies.py`) but also usable as `async with` context manager — see `app/services/scheduler.py` usage.
- `AsyncSession` lifetime: per-request when via `get_db_session` dep; explicit `async with db_session_factory()` outside FastAPI (scheduler, background tasks).
- Cross-DB portability: built for SQLite (dev) + PostgreSQL `asyncpg` (prod). Avoid SQLite-only functions (e.g. `JSON_EXTRACT`) — use SQLAlchemy `JSON` type accessors instead.
- Prod connection string hardcodes the `postgres` Docker service hostname — only resolvable inside the prod compose network.