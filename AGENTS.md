# Finance Analyzer v2 — repository contract

Single-owner service on main; Python 3.12+, FastAPI, async SQLAlchemy, Firefly III ledger. Read README.md and docs/enfoques.md before changes. The old individual branch and multi-tenant architecture are obsolete (2026-11-16).

## Boundaries

- app/core: required FINANCE_API_KEY settings, timezone/logging/errors; no session or token-encryption layer.
- app/db: new Base, inbox_items, mail_account_state, known_senders; repositories are the data-access boundary. Bootstrap create_all against a fresh v2 database, no migrations.
- app/clients: Firefly HTTP, OpenAI-compatible LLM chain, read-only IMAP via stdlib/to_thread.
- app/parsers: deterministic transaction templates before LLM.
- app/services: durable inbox processing, cross-channel dedup, retry backoff, cache and scheduler.
- app/api: /api/v1 routes; X-API-Key everywhere except health/live and health/ready.

Persist before external processing. Use aware UTC internally and America/Bogota offsets for Firefly. COP unless explicit USD. Never create asset accounts; use configured mapping/default. Categories existing by default; apply Firefly rules. Never deduplicate repeated purchases within one channel. Do not log credentials or financial bodies.

No OAuth, session_id, per-user client caches, Telegram headers, cookies, or old batch/job polling endpoints. Single scheduler/worker; shared process lock guards processing. Update README and examples when contracts change. Do not commit .env, DBs, tokens or virtual environments.

## Verification

uv venv --python 3.12; uv pip install -e ".[dev]". Run ruff check app tests and pytest using that environment when assigned verification. Offline fixtures use in-memory Base v2, settings _env_file=None and get_db overrides. Live Firefly requires explicit opt-in. Preserve peers' new *_v2.py tests. Never run live services implicitly.
