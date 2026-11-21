# Core v2

Settings are single-owner Pydantic settings: FINANCE_API_KEY required SecretStr, JSON MAIL_ACCOUNTS and ACCOUNT_MAP, configurable Firefly/OpenAI-compatible endpoints. get_settings is cached; tests clear cache and pass _env_file=None. No session.py, cookie routing or OAuth token encryption. Dates must be timezone-aware UTC internally and America/Bogota when displayed/sent to Firefly; never naive utcnow. See README and .env.example for supported settings.

Notifications: v4 has no bot and no push endpoint, so there are no delivery URL or delivery key settings (and no secret files for them). Alerts live in the `alerts` table that the app polls; `NOTIFY_MIN_INTERVAL_MINUTES` (default 15) is only the default anti-noise interval per alert type/dedupe key, and `PHONE_SILENCE_HOURS` drives the `phone_silence` alert.
