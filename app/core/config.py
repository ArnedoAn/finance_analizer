"""Single-owner settings; environment names are uppercase field names."""
import json
import os
from decimal import Decimal
from functools import lru_cache

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MailAccount(BaseModel):
    name: str
    provider: str = "custom"
    host: str = ""
    port: int = 993
    username: str
    password: SecretStr
    folder: str = "INBOX"
    ssl: bool = True
    authserv_ids: list[str] | None = None

    @model_validator(mode="after")
    def resolve_host(self):
        if not self.host:
            self.host = {"gmail": "imap.gmail.com", "yahoo": "imap.mail.yahoo.com", "icloud": "imap.mail.me.com"}.get(self.provider, "")
        if not self.host or not self.name or not self.username:
            raise ValueError("Mail account requires name, username and host (or known provider)")
        return self


class OwnAccount(BaseModel):
    firefly_account: str = Field(min_length=1)
    bank: str
    kind: str = Field(pattern="^(savings|checking|credit|wallet|pocket|cash)$")
    last4: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False, enable_decoding=False, hide_input_in_errors=True)
    finance_api_key: SecretStr = Field(min_length=32)
    phone_api_key: SecretStr = Field(min_length=32)
    app_name: str = "finance-analyzer"
    app_env: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    log_dir: str = "logs"
    log_file: str = "finance_analyzer.log"
    log_max_bytes: int = 10485760
    log_backup_count: int = 3
    database_url: str = "sqlite+aiosqlite:///./data/finance_v3.db"
    timezone: str = "America/Bogota"
    firefly_base_url: str = ""
    firefly_api_token: SecretStr = SecretStr("")
    manual_default_account: str = "Efectivo"
    firefly_cache_ttl_minutes: int = Field(default=360, ge=0)
    mail_accounts: list[MailAccount] = Field(default_factory=list)
    own_accounts: list[OwnAccount] = Field(default_factory=list)
    expose_docs: bool = False
    max_amount_cop: Decimal = Decimal("50000000")
    review_amount_cop: Decimal = Decimal("2000000")
    llm_auto_approve_max_cop: Decimal = Field(default=Decimal("0"), ge=0)
    sms_auto_approve_max_cop: Decimal = Field(default=Decimal("2000000"), ge=0)
    fail_after_hours: int = Field(default=72, gt=0)
    transfer_pair_window_minutes: int = Field(default=30, ge=0)
    raw_text_retention_days: int = Field(default=30, ge=1)
    notify_url: str = ""
    notify_api_key: SecretStr = SecretStr("")
    notify_min_interval_minutes: int = Field(default=15, ge=0)
    phone_silence_hours: int = Field(default=24, gt=0)
    digest_cron: str = "0 8 * * *"
    reconcile_cron: str = "0 4 * * *"
    weekly_digest_cron: str = "0 8 * * 1"
    email_lookback_days: int = Field(default=7, ge=0)
    email_max_body_chars: int = Field(default=12000, gt=0)
    llm_base_url: str = "https://opencode.ai/inference/go/openai/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_models: str = "deepseek-v4.1-flash,hy3,mimo-v2.6-flash"
    llm_user_agent: str = "finance-analyzer/3.0"
    llm_fallback_base_url: str = "https://api.deepseek.com/v1"
    llm_fallback_api_key: SecretStr = SecretStr("")
    llm_fallback_model: str = "deepseek-chat"
    dedup_window_minutes: int = Field(default=60, ge=0)
    cycle_cron: str = "0 * * * *"
    sender_learning_cron: str = "0 3 1,15 * *"
    scheduler_enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def file_secrets(cls, values):
        from app.core.secrets import read_secret

        values = dict(values)
        for name in ("FINANCE_API_KEY", "PHONE_API_KEY", "DATABASE_URL", "FIREFLY_API_TOKEN", "LLM_API_KEY", "LLM_FALLBACK_API_KEY", "MAIL_ACCOUNTS", "NOTIFY_API_KEY", "OWN_ACCOUNTS"):
            if name.lower() in values and not os.environ.get(name + "_FILE"):
                continue
            value = read_secret(name)
            if value is not None:
                values[name.lower()] = value
        for key in ("mail_accounts", "own_accounts"):
            value = values.get(key)
            if isinstance(value, str):
                values[key] = json.loads(value) if value.strip() else []
        return values

    @model_validator(mode="after")
    def unique_mail_names(self):
        if self.finance_api_key.get_secret_value() == self.phone_api_key.get_secret_value():
            raise ValueError("FINANCE_API_KEY and PHONE_API_KEY must be distinct")
        names = [account.name for account in self.mail_accounts]
        if len(names) != len(set(names)):
            raise ValueError("MAIL_ACCOUNTS names must be unique")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
