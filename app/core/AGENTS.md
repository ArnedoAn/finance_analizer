# app/core/

Cross-cutting infrastructure: configuration, session routing, encryption, logging, timezones, exceptions. 6 files. **The linchpin of the multi-tenant architecture lives here.**

## FILE MAP

| File | Role |
|------|------|
| `config.py` | `Settings` (Pydantic Settings) + `get_settings()` lru_cached singleton. 40+ env vars. SecretStr for secrets. |
| `session.py` | **6-level session resolution chain** + OAuth state HMAC signing (PKCE) + normalization. The multi-tenant core. |
| `security.py` | `TokenEncryption` (Fernet singleton) + `get_token_encryption()` accessor. SHA-256 key derivation. |
| `logging.py` | `structlog` setup + `get_logger(__name__)`. File + console handlers. |
| `timezone.py` | UTC → America/Bogota (UTC-5 fixed offset) helpers. |
| `exceptions.py` | `FinanceAnalyzerError` base + client-specific hierarchies (`GmailError`, `DeepSeekRateLimitError`, `FireflyDuplicateError`, etc.). `to_dict()` for JSON serialization. |
| `__init__.py` | (re-exports key constants — `Settings`, `SESSION_HEADER_NAME`, etc.) |

## WHERE TO LOOK

| Task | Location |
|------|----------|
| Add env var | `config.py:Settings` + document in `.env.example` + `env.production.example` |
| Change session routing priority | `session.py:get_request_session_context` — single source of truth, called via `SessionDep` |
| Add OAuth flow state field | `session.py:build_oauth_state_payload` + `parse_oauth_state_payload` (keep HMAC sign + verify paired) |
| Add new cipher / encrypt new artifact | `security.py` — reuse `TokenEncryption` singleton (do NOT instantiate `TokenEncryption` directly — use `get_token_encryption()`) |
| Adjust log format / destination | `logging.py:setup_logging` — called from `app/main.py:lifespan` startup |
| Add new exception type | `exceptions.py` — extend the appropriate base (`GmailError`, `DeepSeekError`, `FireflyError`, `FinanceAnalyzerError` direct). Implement `to_dict()`. |
| Handle new Telegram-style routing | see `session.py:build_telegram_session_id` / `build_user_session_id` — add a sibling + new priority slot in `get_request_session_context` |

## SESSION RESOLUTION CHAIN (priority order)

```
1. X-Telegram-Session          -> normalize_session_id, use directly
2. X-Telegram-User-ID (+Chat)  -> build_telegram_session_id() -> "tg_<sha256(user:chat)[:40]>"
3. X-User-Id                   -> build_user_session_id()     -> "usr_<sha256[:40]>"
4. X-Session-ID                -> normalize_session_id, use directly
5. finance_session_id cookie   -> normalize_session_id, use directly
6. (none)                      -> generate_session_id() (uuid4 hex), is_new=True
```

Every response sets `X-Session-ID` + `X-Telegram-Session` headers + `finance_session_id` cookie (httponly, lax, secure in prod). Constants in `session.py`:

```
SESSION_HEADER_NAME              = "X-Session-ID"
USER_ID_HEADER_NAME              = "X-User-Id"
SESSION_COOKIE_NAME              = "finance_session_id"
TELEGRAM_SESSION_HEADER_NAME    = "X-Telegram-Session"
TELEGRAM_USER_ID_HEADER_NAME    = "X-Telegram-User-ID"
TELEGRAM_CHAT_ID_HEADER_NAME    = "X-Telegram-Chat-ID"
DEFAULT_SESSION_ID              = "default"
```

Webhook override (clients unable to send headers): `resolve_webhook_session_id(payload_user_id, ambient_session_id)` — if `payload_user_id` is set, returns `usr_<sha256[:40]>`; otherwise returns ambient.

## OAuth STATE SIGNING

- `build_oauth_state_payload(payload: dict)` → base64url of `{v:1, p:payload, s:signature}`, where `signature = _sign_oauth_payload(json.dumps(payload, separators=(",",":"), sort_keys=True))`.
- Signature key: `hashlib.sha256(TOKEN_ENCRYPTION_KEY + b":oauth_state").digest()` via HMAC-SHA256.
- `parse_oauth_state_payload(state)`: validates version, signature (constant-time `hmac.compare_digest`), `session_id` non-empty, `iat` is int, age ≤ `oauth_state_max_age_seconds` (rejects expired).
- Optional PKCE `code_verifier` field validated against `_PKCE_VERIFIER_PATTERN` regex.
- This is how `/auth/callback` recovers the session_id even though the OAuth redirect is a fresh browser request without headers.

## CONVENTIONS (this dir only)

- **Settings access via `get_settings()` only** — lru_cached, env-loaded once. Direct `Settings()` instantiation exists nowhere outside `get_settings`.
- **`SecretStr` discipline** — `.get_secret_value()` is the only legitimate access path. Never log the value; never format into f-strings destined for logs.
- **Encryption key derivation** is SHA-256 of `TOKEN_ENCRYPTION_KEY` then base64url-encoded (Fernet requires 32-byte urlsafe-b64 key). Done in `_create_fernet`.
- **Logging events are snake_case** — `logger.info("event_name", key1=value1, key2=value2)`. Structured logs are queryable by event name in the audit trail.
- **Timezone helper returns `datetime`** — naive or aware depending on input; document the contract per call site.

## ANTI-PATTERNS

- **Don't bypass `get_settings()`** — settings hot-reload is not supported; the singleton is the only consistent source.
- **Don't forge OAuth state** — always `build_oauth_state_payload`. Bypassing the HMAC signature breaks `/auth/callback` correlation.
- **Don't expose `SecretStr` in APIs** — wrap as `Bearer …` / `token=<masked>` in error messages + logs.
- **Don't invent a new session ID format** — `tg_<hash>`, `usr_<hash>`, `default`, or `<uuid4 hex>` are the only valid prefixes. Adding a new client-type prefix requires a `build_xxx_session_id` helper + a slot in the priority chain.
- **Don't mutate `oauth_state_max_age_seconds` without rotating** `TOKEN_ENCRYPTION_KEY` — older state payloads become permanently invalid.
- **Don't add custom exception hierarchies that bypass `FinanceAnalyzerError`** — global exception handler in `app/main.py` only catches `FinanceAnalyzerError` + generic `Exception`. Subclass the right client base.

## NOTES

- `Settings` `model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=True)` — case-sensitive env var names match attribute names.
- `oauth_state_max_age_seconds` default is project-specific (see `.env.example`); tune for slow OAuth round-trips.
- `SCHEDULER_DEFAULT_SESSION_ID` is used by `SchedulerService.__init__` and `/scheduler` admin endpoints — `/status`/`/start`/`/stop` ignore ambient session.
- `TZ=America/Bogota` is **hardcoded** in all 3 docker-compose files — `timezone.py` helpers assume `America/Bogota`. Colombia abolished DST in 1992, so fixed UTC-5 is correct.
- `GMAIL_SUBJECT_FILTERS` default `Factura,Pago,Recibo,Compra,Transferencia` — Spanish financial keywords for Colombian context. Localize via env var if adopting elsewhere.
- Pre-commit missing — `pre-commit>=3.6.0` is in dev deps but no `.pre-commit-config.yaml`. CI has no lint/test gate.