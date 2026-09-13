# app/clients/

3 external HTTP clients. Each ~700+ lines. All async, `httpx.AsyncClient`, `tenacity` retry. Singleton lifecycle managed by `app/api/dependencies.py`.

## FILE MAP

| File | Class | Lines | Singleton scope |
|------|-------|------:|------------------|
| `gmail.py` | `GmailClient` | 745 | **Per-session** — `_gmail_clients[session_id]` dict in `dependencies.py`. Token file `credentials/google_token_<session_id>.json` (encrypted). |
| `deepseek.py` | `DeepSeekClient` | 707 | **Global** — `_deepseek_client` (single instance). API key shared across all sessions (no per-user DeepSeek keys). |
| `firefly.py` | `FireflyClient` | 741 | **Per-session** — `_firefly_clients[session_id]`. Token per session via `PUT /auth/firefly/token` or Telegram firefly auth. |
| `__init__.py` | — | — | (empty) |

## WHERE TO LOOK

| Task | Location |
|------|----------|
| Add new Gmail API call | `gmail.py` method on `GmailClient` — use pre-authenticated `google-api-python-client` resource |
| Change DeepSeek AI prompt | `deepseek.py` `SYSTEM_PROMPT_TRANSACTION` / `SYSTEM_PROMPT_SENDER_LEARNING` constants |
| Add Firefly endpoint wrapper | `firefly.py` — pattern is `_request(method, path, **kwargs)` -> retry-wrapped httpx call |
| Tune retry policy | `tenacity` decorators at top of each client method (exponential backoff, mostly DeepSeek rate-limit specific) |
| Add new external client | create `xxx.py` here + expose singleton factory in `app/api/dependencies.py` (per-session dict OR global) |

## CONVENTIONS (this dir only)

- **All clients accept a settings-derived config** — either explicit `Settings` or via `get_settings()` call inside `__init__`.
- **Per-session singletons** use the dict pattern (`_clients: dict[str, ClientType]`) keyed by `session_id`. `cleanup_clients()` (called from `lifespan`) iterates + closes them on shutdown.
- **Errors are typed per-client**:
  - `GmailError` → `GmailAuthenticationError`, `GmailFetchError`
  - `DeepSeekError` → `DeepSeekAPIError`, `DeepSeekRateLimitError` (specific retry policy)
  - `FireflyError` → `FireflyDuplicateError`, `FireflyValidationError`, `FireflyNotFoundError` (see `app/core/exceptions.py`)
- **`check_connection()` method on each** — used by `/api/v1/health` and `/api/v1/health/ready`. No side effects, no token consumption beyond one HTTP ping.
- **Token encryption** is enforced for any persisted token: `TokenEncryption.encrypt_dict` / `decrypt_dict` (Fernet). Gmail token JSON, Firefly bearer token — all encrypted before disk.
- **DeepSeek response parsing** uses `response_format={"type": "json_object"}` then `json.loads` → Pydantic `TransactionAnalysis.model_validate`. Trust the JSON mode + Pydantic validation; do not parse strings.

## ANTI-PATTERNS

- **Don't instantiate clients in endpoints/services** — get them from `dependencies.py` (`ServicesDep.gmail` / `get_request_gmail_client(session_id)`). Each new instance leaks `httpx.AsyncClient` connections.
- **Don't share `DeepSeekClient` per session** — it's global by design. Pricing/accounting is centralized on one key.
- **Don't bypass `tenacity` retry** — even for "quick" calls. Rate limits and transient 5xx are real on Gmail + DeepSeek + Firefly.
- **Don't return raw `httpx.Response`** from client methods — wrap into Pydantic schemas (see `EmailMessage`, `TransactionAnalysis`, etc. in `app/models/schemas.py`).
- **Don't print/log raw tokens** — `SecretStr` + `TokenEncryption.decrypt` only inside the client. Logs use `logger.info("event", token="<masked>")` patterns.
- **Don't add a new client without registering cleanup** — `cleanup_clients()` in `dependencies.py` must close the new client's HTTP pool. Otherwise FastAPI shutdown leaks connections.

## NOTES

- `GmailClient.authenticate` is defensive — if `credentials/google_token_<id>.json` is missing or invalid, it triggers OAuth (browser flow for desktop; signed state encodes the session_id for callback correlation).
- `DeepSeekClient` has no `check_connection` that hits the API harder than needed — it sends a minimal `/models` ping (verify when modifying).
- `FireflyClient` swallows 422 responses as `FireflyValidationError` — the caller decides if that equals retry vs. give-up.
- Token file naming convention is **mandatory** for scheduler discovery: `credentials/google_token_<session_id>.json` — `_discover_sessions_from_token_paths` globs this pattern (see `app/services/scheduler.py`).
- Gmail OAuth scopes are **read-only** (`gmail.readonly`) — never request write scopes, the app architecture assumes read-only Gmail access.
- DeepSeek prompts are tuned for **Colombian Spanish** financial emails + Colombian push notification apps (Bancolombia, Nequi, Davivienda, etc.) — see `app/api/endpoints/senders.py POST /seed` for the full sender whitelist.