# app/api/endpoints/

8 FastAPI router modules. All mounted under `/api/v1` via `app/api/routes.py` aggregation.

## ROUTER FILE MAP

| File | Prefix | Exports | Role |
|------|--------|---------|------|
| `auth.py` | `/auth` | `router` | Gmail OAuth flow with PKCE + signed state, Firefly token CRUD per session, Telegram firefly auth, DeepSeek status |
| `emails.py` | `/emails` | `router` | Gmail listing, detail, AI analysis preview |
| `health.py` | `/health` | `router` | `/` rich check, `/live` (no deps — pure liveness), `/ready` returns 503 on external outage |
| `processing.py` | `/processing` | `router` | Batch (async, 202 + job_id), job poll, single, retry-failed, audit, statistics, dry-run, debug/test-transaction |
| `sync.py` | `/sync` | `router` | Firefly III sync (cache + direct API), accounts + categories |
| `notifications.py` | `/notifications` | `router` | Webhook (async 202), sync process, history, stats |
| `scheduler.py` | `/scheduler` | `router` | Job status/trigger/logs — admin endpoints bypass session routing |
| `senders.py` | `/senders` | `router` | Known-sender CRUD, AI learn, seed Colombian banks/apps |

## WHERE TO LOOK

| Task | Location |
|------|----------|
| Add new endpoint to existing feature | append to relevant `*.py` `router`, no registration needed (already in `routes.py`) |
| Add new feature module | create `new_feature.py` exporting `router`, include in `app/api/routes.py` |
| Async (background) endpoint pattern | see `processing.py:batch` + `notifications.py:webhook_receive` — return 202 + `job_id`, `BackgroundTasks.add_task(...)` |
| Endpoint that bypasses session (admin) | see `scheduler.py` `/status` `/start` `/stop` — uses `SCHEDULER_DEFAULT_SESSION_ID`, no `SessionDep` |
| Body-field session override | see `notifications.py:webhook_receive` — `resolve_webhook_session_id(payload.user_id, session_ctx.session_id)` for clients without header support |

## CONVENTIONS (this dir only)

- **`ServicesDep`** = full DI bundle (all clients + all services pre-wired for the resolved session). Use for stateless handlers.
- **`SessionDep` + `get_db_session`** = manual mode (when you need raw control of lifecycle, async jobs, custom client construction).
- Async batch endpoints return 202 with `{job_id, poll_url}` — client polls `GET /jobs/{job_id}` (see `processing.py`).
- All endpoints set `X-Session-ID` + `X-Telegram-Session` response headers + `finance_session_id` cookie (via `SessionDep`).
- Health `/live` is the **only zero-dep endpoint** — keep it that way. Liveness probe must never fail on DB / external outage.

## ANTI-PATTERNS

- **Don't import repositories directly in endpoints** — use `ServicesDep` or instantiate services. Endpoints are routing + IO boundary, not data owners.
- **Don't construct clients inline** — `FireflyClient(...)`, `GmailClient(...)` belong in DI (`app/api/dependencies.py`). Endpoints consume what DI yields.
- **No synchronous long-running work in handler** — push to `BackgroundTasks` and return 202 (see `webhook_receive`, `batch`).
- **Don't omit `SessionDep`** unless you're explicitly an admin-only endpoint (scheduler `/status`/`/start`/`/stop` — they use `SCHEDULER_DEFAULT_SESSION_ID`).
- `notifications.py` background task **must `model_validate(payload_dict)` first** — Pydantic must re-validate on the way out of the background task queue (see `_process_notification_background`).

## NOTES

- `processing.py:/debug/test-transaction` is the only endpoint that bypasses AI — guard with `has_session_token()`.
- `senders.py` uses `get_request_gmail_client` (session-scoped) + `get_deepseek_client` (global) — not `ServicesDep`. Same dep pattern as other manual-construction endpoints.
- `auth.py:/callback` parses signed OAuth state via `parse_oauth_state_payload`, sets cookie, redirects — runs without `SessionDep` (browser redirect can't carry headers).