"""
Pydantic Schemas

Defines all data models for the application including:
- Email messages from Gmail
- Transaction analysis from AI
- Firefly III API contracts
- Audit logging
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


# =============================================================================
# Enums
# =============================================================================

class TransactionType(str, Enum):
    """Firefly III transaction types."""
    WITHDRAWAL = "withdrawal"  # Expense/payment
    DEPOSIT = "deposit"        # Income
    TRANSFER = "transfer"      # Between own accounts


class AccountType(str, Enum):
    """Firefly III account types."""
    ASSET = "asset"           # Bank accounts, wallets
    EXPENSE = "expense"       # Merchants, payees
    REVENUE = "revenue"       # Income sources
    LIABILITY = "liability"   # Loans, credit cards


class ProcessingStatus(str, Enum):
    """Email processing status."""
    PENDING = "pending"
    PROCESSING = "processing"
    ANALYZED = "analyzed"
    CREATED = "created"
    SKIPPED = "skipped"
    FAILED = "failed"
    DRY_RUN = "dry_run"


class ProcessingJobStatus(str, Enum):
    """Async email batch job lifecycle status."""
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# =============================================================================
# Gmail Models
# =============================================================================

class EmailMessage(BaseModel):
    """Represents an email message from Gmail."""
    
    model_config = ConfigDict(frozen=True)
    
    message_id: str = Field(..., description="Gmail Message-ID header")
    internal_id: str = Field(..., description="Gmail internal ID")
    thread_id: str = Field(..., description="Gmail thread ID")
    subject: str = Field(default="", description="Email subject")
    sender: str = Field(..., description="Email sender address")
    recipient: str = Field(default="", description="Email recipient")
    date: datetime = Field(..., description="Email date")
    body_text: str = Field(default="", description="Plain text body")
    body_html: str = Field(default="", description="HTML body")
    snippet: str = Field(default="", description="Email snippet/preview")
    labels: list[str] = Field(default_factory=list, description="Gmail labels")
    
    @property
    def body(self) -> str:
        """Return the best available body content."""
        return self.body_text or self.body_html
    
    @property
    def idempotency_key(self) -> str:
        """Generate unique key for deduplication."""
        return f"{self.message_id}:{self.internal_id}"


class EmailFilter(BaseModel):
    """Configuration for email filtering."""
    
    subjects: list[str] = Field(
        default_factory=list,
        description="Subject patterns to match"
    )
    senders: list[str] = Field(
        default_factory=list,
        description="Sender addresses to match"
    )
    after_date: datetime | None = Field(
        default=None,
        description="Only fetch emails after this date"
    )
    max_results: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Maximum emails to fetch"
    )
    exclude_processed: bool = Field(
        default=True,
        description="Exclude already processed emails"
    )


# =============================================================================
# AI Analysis Models
# =============================================================================

class TransactionAnalysis(BaseModel):
    """
    Structured transaction data extracted by AI.
    
    This is the output of DeepSeek's semantic analysis of an email.
    """
    
    model_config = ConfigDict(str_strip_whitespace=True)
    
    amount: Decimal = Field(..., gt=0, description="Transaction amount")
    currency: str = Field(
        default="USD",
        min_length=3,
        max_length=3,
        description="ISO 4217 currency code"
    )
    date: datetime = Field(..., description="Transaction date (ISO 8601)")
    description: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Transaction description"
    )
    merchant: str = Field(
        default="",
        max_length=255,
        description="Merchant or counterparty name"
    )
    suggested_category: str = Field(
        default="Sin Categoría",
        max_length=100,
        description="AI-suggested category"
    )
    suggested_account_name: str = Field(
        default="",
        max_length=255,
        description="AI-suggested account name"
    )
    transaction_type: TransactionType = Field(
        default=TransactionType.WITHDRAWAL,
        description="Transaction type"
    )
    confidence_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="AI confidence in extraction"
    )
    raw_extracted: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw AI response for debugging"
    )
    email_sender: str = Field(
        default="",
        max_length=255,
        description="Email sender address (used to identify source bank)"
    )
    
    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, v: str) -> str:
        """Ensure currency is uppercase."""
        return v.upper() if isinstance(v, str) else v
    
    @field_validator("amount", mode="before")
    @classmethod
    def parse_amount(cls, v: Any) -> Decimal:
        """Parse amount from various formats."""
        if isinstance(v, Decimal):
            return v
        if isinstance(v, (int, float)):
            return Decimal(str(v))
        if isinstance(v, str):
            # Remove currency symbols and thousands separators
            cleaned = v.replace("$", "").replace("€", "").replace(",", "").strip()
            return Decimal(cleaned)
        return v


class AnalysisRequest(BaseModel):
    """Request model for AI analysis."""
    
    email_content: str = Field(..., description="Email content to analyze")
    email_subject: str = Field(default="", description="Email subject for context")
    email_sender: str = Field(default="", description="Sender for context")
    preferred_currency: str = Field(default="USD", description="Default currency")


# =============================================================================
# Firefly III Models
# =============================================================================

class AccountCreate(BaseModel):
    """Model for creating a Firefly III account."""
    
    name: str = Field(..., min_length=1, max_length=255)
    type: AccountType
    currency_code: str = Field(default="USD", min_length=3, max_length=3)
    active: bool = Field(default=True)
    include_net_worth: bool = Field(default=True)
    notes: str | None = Field(default=None, max_length=65535)
    account_role: str | None = Field(
        default=None,
        description="Account role (required for asset accounts, e.g., 'defaultAsset')"
    )


class AccountResponse(BaseModel):
    """Response model for Firefly III account."""
    
    model_config = ConfigDict(from_attributes=True)
    
    id: str
    name: str
    type: AccountType
    currency_code: str
    active: bool
    current_balance: Decimal = Field(default=Decimal("0"))


class CategoryCreate(BaseModel):
    """Model for creating a Firefly III category."""
    
    name: str = Field(..., min_length=1, max_length=255)
    notes: str | None = Field(default=None, max_length=65535)


class CategoryResponse(BaseModel):
    """Response model for Firefly III category."""
    
    model_config = ConfigDict(from_attributes=True)
    
    id: str
    name: str
    spent: list[dict[str, Any]] = Field(default_factory=list)
    earned: list[dict[str, Any]] = Field(default_factory=list)


class TransactionSplit(BaseModel):
    """
    A single split within a Firefly III transaction.
    
    Firefly III supports split transactions; most transactions
    have a single split.
    """
    
    type: TransactionType
    date: str = Field(..., description="Date in YYYY-MM-DD format")
    amount: str = Field(..., description="Amount as string")
    description: str = Field(..., min_length=1, max_length=1000)
    source_name: str | None = Field(default=None, description="Source account name")
    source_id: str | None = Field(default=None, description="Source account ID")
    destination_name: str | None = Field(default=None, description="Destination account name")
    destination_id: str | None = Field(default=None, description="Destination account ID")
    category_name: str | None = Field(default=None, description="Category name")
    category_id: str | None = Field(default=None, description="Category ID")
    currency_code: str = Field(default="USD", min_length=3, max_length=3)
    tags: list[str] = Field(default_factory=list)
    notes: str | None = Field(default=None, max_length=65535)
    external_id: str | None = Field(default=None, description="External reference ID")


class TransactionCreate(BaseModel):
    """Model for creating a Firefly III transaction."""
    
    error_if_duplicate_hash: bool = Field(default=True)
    apply_rules: bool = Field(default=True)
    fire_webhooks: bool = Field(default=True)
    transactions: list[TransactionSplit] = Field(..., min_length=1)


class TransactionResponse(BaseModel):
    """Response model for Firefly III transaction."""
    
    model_config = ConfigDict(from_attributes=True)
    
    id: str
    transaction_journal_id: str
    type: TransactionType
    date: datetime
    amount: Decimal
    description: str
    source_name: str | None = None
    destination_name: str | None = None
    category_name: str | None = None
    tags: list[str] = Field(default_factory=list)


# =============================================================================
# Processing & Audit Models
# =============================================================================

class ProcessingResult(BaseModel):
    """Result of processing a single email."""
    
    email_id: str = Field(..., description="Gmail message ID")
    status: ProcessingStatus
    analysis: TransactionAnalysis | None = Field(default=None)
    transaction_id: str | None = Field(default=None)
    error_message: str | None = Field(default=None)
    error_details: dict[str, Any] = Field(default_factory=dict)
    processing_time_ms: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    @property
    def is_success(self) -> bool:
        """Check if processing was successful."""
        return self.status in (ProcessingStatus.CREATED, ProcessingStatus.DRY_RUN)


class AuditLogCreate(BaseModel):
    """Model for creating an audit log entry."""
    
    session_id: str | None = None
    email_message_id: str = Field(..., description="Gmail Message-ID")
    email_internal_id: str = Field(..., description="Gmail internal ID")
    email_subject: str = Field(default="")
    email_sender: str = Field(default="")
    email_date: datetime
    status: ProcessingStatus
    analysis_result: dict[str, Any] | None = Field(default=None)
    firefly_transaction_id: str | None = Field(default=None)
    error_message: str | None = Field(default=None)
    error_details: dict[str, Any] | None = Field(default=None)
    processing_time_ms: int = Field(default=0)
    dry_run: bool = Field(default=False)


class AuditLogResponse(BaseModel):
    """Response model for audit log entry."""
    
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    email_message_id: str
    email_internal_id: str
    email_subject: str
    email_sender: str
    email_date: datetime
    status: ProcessingStatus
    firefly_transaction_id: str | None
    error_message: str | None
    processing_time_ms: int
    dry_run: bool
    created_at: datetime
    updated_at: datetime


# =============================================================================
# API Response Models
# =============================================================================

class HealthCheck(BaseModel):
    """Health check response."""
    
    status: str = Field(default="healthy")
    version: str
    environment: str
    session_id: str | None = None
    services: dict[str, bool] = Field(default_factory=dict)


class BatchProcessRequest(BaseModel):
    """Request for batch email processing."""
    
    max_emails: int = Field(default=50, ge=1, le=500)
    dry_run: bool = Field(default=False)
    subject_filters: list[str] | None = Field(default=None)
    after_date: datetime | None = Field(default=None)
    use_known_senders: bool = Field(
        default=True,
        description="Filter emails by known financial senders"
    )


class BatchProcessResponse(BaseModel):
    """Response for batch email processing."""
    
    total_emails: int
    processed: int
    created: int
    skipped: int
    failed: int
    dry_run: bool
    results: list[ProcessingResult]
    processing_time_ms: int


class ProcessingJobCreateResponse(BaseModel):
    """Immediate response when an async processing job is enqueued."""

    job_id: str
    status: ProcessingJobStatus
    poll_url: str
    message: str


class ProcessingJobStatusResponse(BaseModel):
    """Polling response for async processing job state."""

    job_id: str
    status: ProcessingJobStatus
    session_id: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: BatchProcessResponse | None = None
    error_message: str | None = None


class ErrorResponse(BaseModel):
    """Standard error response."""
    
    error: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# =============================================================================
# Known Senders Models
# =============================================================================

class KnownSenderCreate(BaseModel):
    """Request model for adding a known sender."""
    
    keyword: str = Field(
        ...,
        min_length=2,
        max_length=100,
        description=(
            "Sender identifier. Recommended: full From email address for exact match "
            "(e.g., 'alertas@bancolombia.com'). "
            "Can also be a keyword contained in the email address."
        ),
    )
    sender_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Human-readable sender name"
    )
    sender_type: str = Field(
        default="bank",
        description="Type: bank, payment, store, subscription"
    )


class KnownSenderResponse(BaseModel):
    """Response model for known sender."""
    
    id: int
    keyword: str
    sender_name: str
    sender_type: str
    is_active: bool
    is_auto_learned: bool
    confidence_score: float
    emails_matched: int
    last_matched_at: datetime | None
    created_at: datetime


class KnownSenderBulkCreate(BaseModel):
    """Request model for bulk adding senders."""
    
    senders: list[KnownSenderCreate] = Field(
        ...,
        min_length=1,
        description="List of senders to add"
    )


# =============================================================================
# Scheduler Models
# =============================================================================

class SchedulerJobStatus(BaseModel):
    """Status of a scheduler job."""
    
    id: str
    name: str
    next_run: datetime | None
    trigger: str


class SchedulerJobLogResponse(BaseModel):
    """Response model for scheduler job log."""
    
    id: int
    job_name: str
    job_type: str
    status: str
    started_at: datetime
    completed_at: datetime | None
    emails_processed: int
    transactions_created: int
    senders_learned: int
    error_message: str | None


class SenderLearningRequest(BaseModel):
    """Request for manual sender learning."""
    
    email_count: int = Field(default=100, ge=10, le=500)
    days_back: int = Field(default=30, ge=7, le=90)


class SenderLearningResponse(BaseModel):
    """Response for sender learning operation."""
    
    emails_analyzed: int
    senders_learned: int
    new_senders: list[dict[str, Any]]


# =============================================================================
# Notification / Webhook Models
# =============================================================================

class NotificationPayload(BaseModel):
    """
    Incoming webhook payload from phone notification app.
    
    Maps the template variables from the phone app's webhook configuration.
    For multi-user without custom headers, set ``user_id`` (same value as
    ``X-User-Id``) so Firefly/tokens are scoped to that user.
    """
    
    model_config = ConfigDict(frozen=True)
    
    id: str = Field(..., description="Notification ID (from-date)")
    type: str = Field(default="", description="Notification type")
    app: str = Field(..., description="Source app package name (e.g., com.nequi.MobileApp)")
    sender: str = Field(default="", description="Notification sender")
    message: str = Field(default="", description="Notification body")
    title: str = Field(default="", description="Notification title")
    text: str = Field(default="", description="Notification text content")
    timestamp: str = Field(default="", description="Notification timestamp")
    date: str = Field(default="", description="Notification date")
    category: str = Field(default="", description="Notification category")
    device: str = Field(default="", description="Device that received the notification")
    user_id: str | None = Field(
        default=None,
        max_length=128,
        validation_alias=AliasChoices("user_id", "userId"),
        description="External user id; same semantics as X-User-Id for session resolution",
    )
    source_channel: Literal["notification", "sms"] = Field(
        default="notification",
        validation_alias=AliasChoices("source_channel", "sourceChannel"),
        description="Tag in Firefly (notification vs SMS)",
    )
    
    @field_validator("user_id", mode="before")
    @classmethod
    def _normalize_user_id(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            return s if s else None
        return v
    
    @property
    def content(self) -> str:
        """Return the best available content (message > text)."""
        return self.message or self.text
    
    @property
    def notification_hash(self) -> str:
        """Generate SHA-256 hash for idempotency."""
        import hashlib
        raw = f"{self.sender}:{self.message}:{self.timestamp}"
        return hashlib.sha256(raw.encode()).hexdigest()


class NotificationProcessingResult(BaseModel):
    """Result of processing a single notification."""
    
    notification_hash: str = Field(..., description="Notification idempotency hash")
    source_app: str = Field(default="", description="Source app package name")
    status: ProcessingStatus
    analysis: TransactionAnalysis | None = Field(default=None)
    transaction_id: str | None = Field(default=None)
    error_message: str | None = Field(default=None)
    error_details: dict[str, Any] = Field(default_factory=dict)
    processing_time_ms: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    @property
    def is_success(self) -> bool:
        """Check if processing was successful."""
        return self.status in (ProcessingStatus.CREATED, ProcessingStatus.DRY_RUN)


class NotificationWebhookResponse(BaseModel):
    """Immediate response for webhook (202 Accepted)."""
    
    accepted: bool = Field(default=True)
    notification_hash: str = Field(..., description="Hash for tracking")
    message: str = Field(default="Notification accepted for processing")
