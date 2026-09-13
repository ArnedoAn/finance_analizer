# app/services/

Business-logic orchestration layer. 7 modules, 2527 lines total. Each file = 1 service class (no shared base class).

## SERVICE MAP

| File | Class | Lines | Role |
|------|-------|------:|------|
| `email_processor.py` | `EmailProcessorService` | 667 | Main email → AI → Firefly pipeline. Orchestrates `GmailClient` + `DeepSeekClient` + `SyncService` + `TransactionService`. |
| `notification_processor.py` | `NotificationProcessorService` | 437 | Phone push-notification pipeline (no Gmail). Same downstream shape as email processor. |
| `sync_service.py` | `SyncService` | 522 | Firefly III cache sync + account/category resolution. **Highest complexity in codebase.** |
| `transaction_service.py` | `TransactionService` | 232 | Builds + POSTs Firefly transaction from `TransactionAnalysis`. UTC→Bogota TZ conversion. |
| `sender_learning.py` | `SenderLearningService` | 325 | AI-driven discovery of financial-sender patterns per session. |
| `scheduler.py` | `SchedulerService` (+ module-level `_scheduler`) | 319 | APScheduler CRON + multi-session fan-out (bypasses FastAPI DI). |
| `__init__.py` | (3 service exports) | 7 | Public re-exports — see file for which. |

## WHERE TO LOOK

| Task | Location |
|------|----------|
| Trace email → transaction flow | `email_processor.process_batch` → `_analyze_email` → `transaction_service.create_from_analysis` |
| Add new transaction type handling | `transaction_service.py` + `sync_service.resolve_destination_account` (each TransactionType has its own branch) |
| Add a new AI prompt | `app/clients/deepseek.py` (NOT here — services call client, prompts live in clients) |
| Define a new CRON job | `scheduler.setup_jobs` — add `scheduler.add_job(...)` call with CRON expression |
| Add cross-channel dedup logic | `TransactionFingerprintRepository` (`app/db/repositories.py`) — services query it, repos compute hash |
| Handle new external notification channel | `notification_processor.process_notification` — add to `_analyze_notification` + cross-channel fingerprint key |

## COMPLEXITY HOTSPOTS (refactor targets)

### 1. `SyncService.resolve_source_account` — 288 lines

5-tier fallback cascade for `TransactionType.withdrawal`:
1. Exact name match in `account_cache`
2. Partial name in cache (`get_by_partial_name` — itself Python-side, loads ALL rows)
3. Email sender → `KnownSender` → bank name → exact match
4. Bank name → partial match
5. Auto-create in Firefly (with error handling)
6. Second auto-create with different name candidate
7. Fallback to default asset account

Duplicated `try/except FireflyError` on auto-create (lines 210–244 and 265–300) — extract to helper.

### 2. `NotificationProcessorService.process_notification` — 265 lines

6-step pipeline with **3 duplicated audit-log-and-commit blocks** (lines 191–211, 243–264, 344–352). Extract to `_commit_with_audit(...)`.

### 3. `EmailProcessorService.process_batch` — 214 lines

Branches on `use_known_senders` vs `subject_filters`, pre-filters via `_is_likely_financial_email` (Spanish financial vocabulary hardcoded). Has `test_mode_clear_processed` flag sprinkled as 5 conditionals.

### 4. `EmailProcessorService.process_single_email` — 177 lines

Full pipeline: idempotency check → AI analysis → transaction creation → fingerprint dedup → 4-way exception handling (`FireflyDuplicateError` / `DeepSeekError` / `FireflyError` / generic).

## CONVENTIONS (this dir only)

- **Constructor signature** everywhere: `__init__(self, session: AsyncSession, <clients>, session_id: str = DEFAULT_SESSION_ID)` — raises `ValueError` on invalid session_id.
- **Repos `flush`, services don't commit** unless explicitly required. Known exception: `sender_learning.learn_from_recent_emails:168` (manual `session.commit()` for bulk insert path — verify if still needed).
- `SchedulerService` is **not FastAPI DI-wired** — instantiated in `app/main.py:lifespan` with `settings.scheduler_default_session_id`. Per-tick iteration manually constructs `EmailProcessorService` + `SenderLearningService` for each discovered session.
- All services normalize `session_id` via app/core/session.py helpers.

## ANTI-PATTERNS

- **Don't add raw SQLAlchemy queries in services** — known violation in `sender_learning.get_all_senders(include_inactive=True)` (bypasses repo). Fix when touching.
- **Don't duplicate audit-log-and-commit** — consolidate via helper (see refactor targets above).
- **Don't mutate `TransactionAnalysis`** — it is `frozen=True`. Create new instance if enrichment needed.
- **Don't add `print()` or unstructured logging** — `from app.core.logging import get_logger; logger.info("event_name", k=v)`.
- **Don't call `reprocess_failed` from inside `process_batch`** — separate entry point, returns its own `BatchProcessResponse`.

## NOTES

- `EmailProcessorService._is_likely_financial_email` hardcodes Spanish tokens (`banco`, `pago`, `factura`, etc.). Localize or move to settings if reused.
- `SchedulerService._discover_active_session_ids` runs UNION ALL across 9 tables — fragile if schema changes. Consider a `Session` registry table.
- `_run_email_processing` and `_run_sender_learning` create **fresh service instances per session per tick** — high allocation pressure under many sessions. Reuse pool when optimizing.
- `transaction_service` does manual UTC→Bogota conversion (UTC-5 fixed) — no DST. Colombia has abolished DST, so OK; document the assumption.