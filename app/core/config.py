"""
Application Configuration Module

Centralized configuration management using Pydantic Settings.
All sensitive values are loaded from environment variables.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.
    
    All sensitive values use SecretStr to prevent accidental exposure in logs.
    """
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    
    # =========================================================================
    # Application Settings
    # =========================================================================
    app_name: str = Field(default="finance-analyzer", description="Application name")
    app_env: Literal["development", "staging", "production"] = Field(
        default="development", description="Environment"
    )
    debug: bool = Field(default=False, description="Debug mode")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Logging level"
    )
    log_dir: str = Field(default="./logs", description="Directory for log files")
    log_file: str = Field(default="finance_analyzer.log", description="Log file name")
    log_max_bytes: int = Field(
        default=10485760, description="Max log file size in bytes (default 10MB)"
    )
    log_backup_count: int = Field(
        default=5, description="Number of backup log files to keep"
    )
    
    # =========================================================================
    # Server Settings
    # =========================================================================
    host: str = Field(default="0.0.0.0", description="Server host")
    port: int = Field(default=8000, ge=1, le=65535, description="Server port")
    
    # =========================================================================
    # Database
    # =========================================================================
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/finance_analyzer.db",
        description="Database connection URL"
    )
    
    # =========================================================================
    # Google / Gmail API
    # =========================================================================
    google_credentials_path: Path = Field(
        default=Path("./credentials/google_credentials.json"),
        description="Path to Google OAuth credentials JSON"
    )
    google_token_path: Path = Field(
        default=Path("./credentials/google_token.json"),
        description="Path to store OAuth token"
    )
    token_encryption_key: SecretStr = Field(
        ..., description="Encryption key for OAuth tokens"
    )
    gmail_redirect_uri: str = Field(
        default="http://localhost:8000/api/v1/auth/callback",
        description="OAuth redirect URI (must match Google Console)"
    )
    gmail_subject_filters: str = Field(
        default="Factura,Pago,Recibo,Compra,Confirmación,Transferencia,Cargo,Abono",
        description="Comma-separated email subject filters"
    )
    gmail_max_results: int = Field(
        default=50, ge=1, le=500, description="Max emails per batch"
    )
    gmail_use_subject_filters: bool = Field(
        default=True,
        description="Whether to include subject filters in Gmail queries"
    )
    
    # =========================================================================
    # DeepSeek AI
    # =========================================================================
    deepseek_api_key: SecretStr = Field(..., description="DeepSeek API key")
    deepseek_api_url: str = Field(
        default="https://api.deepseek.com/v1/chat/completions",
        description="DeepSeek API endpoint"
    )
    deepseek_model: str = Field(default="deepseek-chat", description="DeepSeek model")
    deepseek_timeout: int = Field(default=60, ge=10, le=300, description="API timeout")
    deepseek_max_retries: int = Field(default=3, ge=1, le=10, description="Max retries")
    
    # =========================================================================
    # Firefly III
    # =========================================================================
    firefly_base_url: str = Field(..., description="Firefly III base URL")
    firefly_api_token: SecretStr = Field(..., description="Firefly III API token")
    firefly_timeout: int = Field(default=30, ge=5, le=120, description="API timeout")
    firefly_max_retries: int = Field(default=3, ge=1, le=10, description="Max retries")
    firefly_default_asset_account: str = Field(
        default="Bancolombia", description="Default asset account for withdrawals"
    )
    firefly_default_expense_account: str = Field(
        default="Gastos Generales", description="Default expense account for unknown merchants"
    )
    firefly_default_revenue_account: str = Field(
        default="Ingresos Generales", description="Default revenue account for unknown income sources"
    )
    
    # =========================================================================
    # Currency Settings
    # =========================================================================
    default_currency: str = Field(
        default="COP", description="Default currency code (ISO 4217)"
    )
    
    # =========================================================================
    # Processing Options
    # =========================================================================
    dry_run: bool = Field(
        default=False, description="Dry run mode (analyze without creating)"
    )
    auto_create_accounts: bool = Field(
        default=True, description="Auto-create accounts if not exist"
    )
    auto_create_categories: bool = Field(
        default=True, description="Auto-create categories if not exist"
    )
    email_lookback_days: int = Field(
        default=7, ge=1, le=365, description="Days to look back for emails"
    )
    test_mode_clear_processed: bool = Field(
        default=False,
        description="TEST MODE: Clear processed emails table before processing (for testing only)"
    )
    
    # =========================================================================
    # Retry Configuration
    # =========================================================================
    retry_max_attempts: int = Field(default=3, ge=1, le=10)
    retry_wait_seconds: int = Field(default=2, ge=1, le=60)
    retry_max_wait_seconds: int = Field(default=30, ge=5, le=300)
    
    # =========================================================================
    # Scheduler Configuration (CRON)
    # =========================================================================
    scheduler_enabled: bool = Field(
        default=True, description="Enable scheduled tasks"
    )
    scheduler_processing_cron: str = Field(
        default="0 * * * *",  # Every hour
        description="CRON expression for email processing job"
    )
    scheduler_learning_cron: str = Field(
        default="0 0 1,15 * *",  # 1st and 15th of each month
        description="CRON expression for sender learning job"
    )
    scheduler_learning_email_count: int = Field(
        default=100, ge=10, le=500,
        description="Number of emails to analyze for learning"
    )
    
    # =========================================================================
    # Token Optimization
    # =========================================================================
    deepseek_max_email_chars: int = Field(
        default=2000, ge=500, le=10000,
        description="Max characters to send to AI (token reduction)"
    )
    deepseek_strip_html: bool = Field(
        default=True, description="Strip HTML tags from emails"
    )
    deepseek_strip_signatures: bool = Field(
        default=True, description="Strip email signatures"
    )
    deepseek_strip_footers: bool = Field(
        default=True, description="Strip legal/marketing footers"
    )

    @property
    def gmail_subjects_list(self) -> list[str]:
        """Parse comma-separated subjects into list."""
        return [s.strip() for s in self.gmail_subject_filters.split(",") if s.strip()]
    
    @property
    def is_production(self) -> bool:
        """Check if running in production."""
        return self.app_env == "production"
    
    @field_validator("google_credentials_path", "google_token_path", mode="before")
    @classmethod
    def validate_path(cls, v: str | Path) -> Path:
        """Convert string paths to Path objects."""
        return Path(v) if isinstance(v, str) else v


@lru_cache
def get_settings() -> Settings:
    """
    Get cached settings instance.
    
    Uses lru_cache to ensure settings are only loaded once.
    """
    return Settings()
