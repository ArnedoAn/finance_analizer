"""Single-owner settings; environment names are uppercase field names."""
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

    @model_validator(mode="after")
    def resolve_host(self):
        if not self.host:
            self.host = {"gmail": "imap.gmail.com", "yahoo": "imap.mail.yahoo.com", "icloud": "imap.mail.me.com"}.get(self.provider, "")
        if not self.host or not self.name or not self.username:
            raise ValueError("Mail account requires name, username and host (or known provider)")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)
    finance_api_key: SecretStr = Field(min_length=1)
    app_name: str = "finance-analyzer"
    app_env: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    log_dir: str = "logs"
    log_file: str = "finance_analyzer.log"
    log_max_bytes: int = 10485760
    log_backup_count: int = 3
    database_url: str = "sqlite+aiosqlite:///./data/finance_v2.db"
    timezone: str = "America/Bogota"
    firefly_base_url: str = ""
    firefly_api_token: SecretStr = SecretStr("")
    firefly_default_asset_account: str = "Efectivo"
    manual_default_account: str = "Efectivo"
    firefly_cache_ttl_minutes: int = Field(default=360, ge=0)
    auto_create_categories: bool = False
    account_map: dict[str, str] = Field(default_factory=dict)
    mail_accounts: list[MailAccount] = Field(default_factory=list)
    email_lookback_days: int = Field(default=7, ge=0)
    email_max_body_chars: int = Field(default=12000, gt=0)
    llm_base_url: str = "https://opencode.ai/inference/go/openai/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_models: str = "deepseek-v4.1-flash,hy3,mimo-v2.6-flash"
    llm_user_agent: str = "finance-analyzer/2.0"
    llm_fallback_base_url: str = "https://api.deepseek.com/v1"
    llm_fallback_api_key: SecretStr = SecretStr("")
    llm_fallback_model: str = "deepseek-chat"
    dedup_window_minutes: int = Field(default=60, ge=0)
    max_attempts: int = Field(default=8, gt=0)
    cycle_cron: str = "0 * * * *"
    sender_learning_cron: str = "0 3 1,15 * *"
    scheduler_enabled: bool = True

    @model_validator(mode="after")
    def unique_mail_names(self):
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
