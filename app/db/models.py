"""Single-owner durable inbox and IMAP checkpoints."""
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.core.timezone import now_utc
from app.db.database import Base
from app.security.crypto import EncryptedText


class AwareDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Datetime must be timezone aware")
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class InboxItem(Base):
    __tablename__ = "inbox_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20), index=True)
    source_ref: Mapped[str] = mapped_column(String(512), unique=True)
    account_name: Mapped[str | None] = mapped_column(String(255))
    message_id: Mapped[str | None] = mapped_column(String(512), index=True)
    app: Mapped[str | None] = mapped_column(String(255))
    sender: Mapped[str] = mapped_column(String(512), default="")
    subject_or_title: Mapped[str] = mapped_column(Text, default="")
    raw_text: Mapped[str | None] = mapped_column(EncryptedText)
    occurred_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    method: Mapped[str | None] = mapped_column(String(20))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str | None] = mapped_column(String(10))
    merchant: Mapped[str | None] = mapped_column(String(512))
    category: Mapped[str | None] = mapped_column(String(255))
    asset_account: Mapped[str | None] = mapped_column(String(255))
    transaction_type: Mapped[str | None] = mapped_column(String(20))
    destination_account: Mapped[str | None] = mapped_column(String(255))
    original_kind: Mapped[str | None] = mapped_column(String(20))
    template_id: Mapped[str | None] = mapped_column(String(255))
    review_reason: Mapped[str | None] = mapped_column(Text)
    claim_token: Mapped[str | None] = mapped_column(String(36))
    claim_until: Mapped[datetime | None] = mapped_column(AwareDateTime())
    firefly_transaction_id: Mapped[str | None] = mapped_column(String(100))
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("inbox_items.id"))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(AwareDateTime(), index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)
    # v4 (Phase 4 migration adds these columns): owner/assistant description and context tags.
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str] | None] = mapped_column(JSON)
    # ServerQuality: verdict of the second model on the extraction, ``{"status": "verified" | "disagree", "model", "fields", "reason"}``.
    second_opinion: Mapped[dict | None] = mapped_column(JSON)
    # A USD movement converted with the TRM of its day (``app.services.fx``): ``amount`` is then COP and these keep the original.
    foreign_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    foreign_currency: Mapped[str | None] = mapped_column(String(10))


class MailAccountState(Base):
    __tablename__ = "mail_account_state"
    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    uidvalidity: Mapped[str | None] = mapped_column(String(100))
    last_uid: Mapped[int] = mapped_column(Integer, default=0)
    last_poll_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)


class KnownSender(Base):
    __tablename__ = "known_senders"
    id: Mapped[int] = mapped_column(primary_key=True)
    keyword: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    source: Mapped[str] = mapped_column(String(30), default="manual")
    status: Mapped[str] = mapped_column(String(30), default="active")
    require_auth: Mapped[bool] = mapped_column(Boolean, default=True)


class InboxEvent(Base):
    __tablename__ = "inbox_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    inbox_id: Mapped[int] = mapped_column(ForeignKey("inbox_items.id"), index=True)
    action: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)


class CycleRun(Base):
    __tablename__ = "cycle_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    finished_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    result: Mapped[str | None] = mapped_column(Text)


class Notification(Base):
    __tablename__ = "notifications"
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    last_sent_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)


class OwnAccountRow(Base):
    """Owner's own account: which Firefly asset/liability a bank maps to and which sources feed it.

    Runtime source of truth for the ingest pipeline (``app.services.own_accounts``); ``OWN_ACCOUNTS`` only seeds it once.
    ``last4``, ``aliases`` and ``sources`` are JSON lists replaced wholesale on update.
    """
    __tablename__ = "own_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    firefly_account: Mapped[str] = mapped_column(String(255))
    bank: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(20))
    last4: Mapped[list[str]] = mapped_column(JSON, default=list)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    sources: Mapped[list[str]] = mapped_column(JSON, default=list)
    # ServerQuality: credit cards only. Day of the month of the statement cut and of the payment due date (1-31, clamped to the month length).
    statement_day: Mapped[int | None] = mapped_column(Integer)
    payment_day: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)


# Case-insensitive uniqueness of the Firefly name, enforced by the database on SQLite and PostgreSQL alike.
Index("uq_own_accounts_firefly_account_lower", func.lower(OwnAccountRow.firefly_account), unique=True)


# v4 domain tables live in one module per phase so parallel work never shares a file.
# Imported here so Base.metadata (tests, Alembic autogenerate) always sees them.
from app.db import (  # noqa: E402,F401
    models_alerts,
    models_assistant,
    models_debts,
    models_devices,
    models_flags,
    models_ledger,
    models_rules,
)


# --- ServerStatements ---
class Statement(Base):
    """A reconciled bank statement. Holds only extracted rows (see ``StatementRow``) and the reconciliation result:
    never the PDF, its text or its password. ``file_sha256`` makes a repeated upload idempotent per account.

    ``own_account_id`` has no foreign key (like ``audit``): the statement outlives a deleted own account and keeps its
    name in ``account_name``. ``totals`` and ``summary`` are plain JSON of decimal strings and counts; ``extra_json`` is the
    encrypted list of ledger movements without a statement row. Purged after ``statements.RETENTION_DAYS``.
    """
    __tablename__ = "statements"
    __table_args__ = (Index("uq_statements_account_file", "own_account_id", "file_sha256", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    own_account_id: Mapped[int] = mapped_column(Integer, index=True)
    account_name: Mapped[str] = mapped_column(String(255))
    file_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), index=True)
    reason: Mapped[str | None] = mapped_column(String(60))
    period_from: Mapped[date | None] = mapped_column(Date())
    period_to: Mapped[date | None] = mapped_column(Date())
    rows_count: Mapped[int] = mapped_column(Integer, default=0)
    discarded_rows: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    totals: Mapped[dict | None] = mapped_column(JSON)
    summary: Mapped[dict | None] = mapped_column(JSON)
    extra_json: Mapped[str | None] = mapped_column(EncryptedText)
    reconciled_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, index=True)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)


class StatementRow(Base):
    """One extracted statement row. ``description`` and the ledger description are encrypted at rest.

    ``match_status`` is None until reconciled, then ``matched``, ``mismatched`` (same movement, different amount),
    ``missing`` (no ledger movement) or ``imported`` (created from the statement through ``transactions/manual``).
    """
    __tablename__ = "statement_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey("statements.id", ondelete="CASCADE"), index=True)
    row_no: Mapped[int] = mapped_column(Integer)
    day: Mapped[date] = mapped_column(Date())
    description: Mapped[str] = mapped_column(EncryptedText)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    direction: Mapped[str] = mapped_column(String(6))
    balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    match_status: Mapped[str | None] = mapped_column(String(12))
    firefly_id: Mapped[str | None] = mapped_column(String(100))
    firefly_day: Mapped[date | None] = mapped_column(Date())
    firefly_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    firefly_description: Mapped[str | None] = mapped_column(EncryptedText)
    inbox_id: Mapped[int | None] = mapped_column(Integer)


# --- ServerAiReview ---
from sqlalchemy import Float, UniqueConstraint  # noqa: E402


class AiUsage(Base):
    """LLM calls per day and kind (``app.services.ai_budget``). ``auto_calls`` is the part made by automatic jobs.

    ``allowance`` is the last ``opencode.allowance`` OpenCode Go reported for that day and kind (shape not fixed).
    """
    __tablename__ = "ai_usage"
    __table_args__ = (UniqueConstraint("day", "kind", name="uq_ai_usage_day_kind"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date(), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    calls: Mapped[int] = mapped_column(Integer, default=0)
    auto_calls: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    allowance: Mapped[dict | list | str | int | float | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)


class AiSuggestion(Base):
    """The LLM's proposed resolution of one ``needs_review`` item (one row per item, replaced when proposed again).

    ``status``: pending | applied | discarded | stale (the item left review) | error (no usable answer; ``error`` says why).
    ``action``: approve (register with the proposed fields) | ignore | duplicate (both reject the item).
    """
    __tablename__ = "ai_suggestions"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("inbox_items.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    action: Mapped[str] = mapped_column(String(12), default="approve")
    firefly_account: Mapped[str | None] = mapped_column(String(255))
    category: Mapped[str | None] = mapped_column(String(255))
    merchant: Mapped[str | None] = mapped_column(String(255))
    transaction_type: Mapped[str | None] = mapped_column(String(20))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    duplicate_of_id: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str | None] = mapped_column(String(100))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)
    applied_at: Mapped[datetime | None] = mapped_column(AwareDateTime())


class MerchantCorrection(Base):
    """Streak of owner category corrections of one normalized merchant (rules that learn by themselves)."""
    __tablename__ = "merchant_corrections"
    id: Mapped[int] = mapped_column(primary_key=True)
    merchant: Mapped[str] = mapped_column(String(255), unique=True)
    category: Mapped[str] = mapped_column(String(255))
    streak: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)


class CounterpartyMerge(Base):
    """One merge of Firefly expense/revenue accounts, with the map of moved splits for the 7-day undo.

    ``changes`` is ``{"sources": [{"id", "name", "deleted", "restored_id", "moves": [{"group_id", "journal_id", "field",
    "done"}]}]}``; ``status``: running | applied | partial (some moves failed) | undone.
    """
    __tablename__ = "counterparty_merges"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_type: Mapped[str] = mapped_column(String(10))
    target_id: Mapped[str] = mapped_column(String(100))
    target_name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(10), default="running", index=True)
    changes: Mapped[dict] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(100), default="system")
    device_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(AwareDateTime())
    undone_at: Mapped[datetime | None] = mapped_column(AwareDateTime())


# --- ServerSnapshots ---
class DashboardSnapshot(Base):
    """Precomputed monthly dashboard (``app.services.snapshots``): one row per ``YYYY-MM``."""
    __tablename__ = "dashboard_snapshots"
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)
    computed_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    version: Mapped[int] = mapped_column(Integer, default=1)


class ServerError(Base):
    """Server error shown in the app (``app.core.error_log``); the newest 300 rows, 30 days at most."""
    __tablename__ = "server_errors"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, index=True)
    component: Mapped[str] = mapped_column(String(20), index=True)
    operation: Mapped[str] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(Text, default="")
    exc_type: Mapped[str | None] = mapped_column(String(100))
    status_code: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[str | None] = mapped_column(Text)


# --- ServerAiInsights ---
class AiItemSuggestion(Base):
    """Category and tag proposal for one registered movement (``app.services.ai_categorize``); one row per inbox item.

    ``status``: ``pending`` (owner decides), ``auto_applied`` (the server applied it; ``group_id`` is the ledger group to
    undo), ``applied`` (owner), ``discarded``, ``stale`` (the movement changed or left), ``none`` (nothing to propose)
    or ``error`` (retried after a few hours). ``applied_fields`` lists what was written: ``category`` and/or ``tags``.
    """
    __tablename__ = "ai_item_suggestions"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("inbox_items.id"), unique=True)
    status: Mapped[str] = mapped_column(String(12), index=True, default="pending")
    category: Mapped[str | None] = mapped_column(String(255))
    category_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(String(300), default="")
    note: Mapped[str | None] = mapped_column(String(60))
    model: Mapped[str | None] = mapped_column(String(100))
    group_id: Mapped[str | None] = mapped_column(String(36), index=True)
    applied_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)
    applied_at: Mapped[datetime | None] = mapped_column(AwareDateTime())


class AiNarrative(Base):
    """Narrated monthly report. ``text`` and ``figures_json`` (the exact figures the model was given) are encrypted."""
    __tablename__ = "ai_narratives"
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    text: Mapped[str] = mapped_column(EncryptedText)
    figures_json: Mapped[str] = mapped_column(EncryptedText)
    model: Mapped[str | None] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(10), default="auto")
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)


class AiNote(Base):
    """Small model-written fact keyed by ``(kind, key)``: anomaly explanations and subscription verdicts.

    ``text`` is encrypted and empty when the model failed (``data["error"]`` is then true and the note is retried later).
    """
    __tablename__ = "ai_notes"
    __table_args__ = (UniqueConstraint("kind", "key", name="uq_ai_notes_kind_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    key: Mapped[str] = mapped_column(String(255))
    text: Mapped[str] = mapped_column(EncryptedText, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)


# --- ServerBudgets ---
class BudgetMap(Base):
    """What the owner's corrections taught: a normalized merchant (or a category) always goes to one Firefly budget.

    ``streak`` counts consecutive owner assignments of that key to ``budget``; the mapping is used (``learned``) once the
    streak reaches the threshold of its kind (``app.services.budget_assign``) and is dropped to a streak of one when the
    owner chooses another budget.
    """
    __tablename__ = "budget_map"
    __table_args__ = (UniqueConstraint("kind", "key", name="uq_budget_map_kind_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))
    key: Mapped[str] = mapped_column(String(255))
    budget: Mapped[str] = mapped_column(String(255))
    streak: Mapped[int] = mapped_column(Integer, default=1)
    learned: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)


class BudgetAssignment(Base):
    """The budget decision for one registered expense (one row per inbox item), also the owner's suggestion queue.

    ``status``: ``auto_applied`` (the server wrote it to Firefly; ``group_id`` is the ledger group to undo),
    ``pending`` (confidence below the threshold: the owner approves or discards), ``applied`` (owner), ``discarded``,
    ``existing`` (Firefly already had a budget), ``none`` (no budget fits), ``stale`` (the movement changed or left) or
    ``error`` (retried after a few hours). ``source``: learned | category_name | ai | firefly.
    """
    __tablename__ = "budget_assignments"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("inbox_items.id"), unique=True)
    status: Mapped[str] = mapped_column(String(12), index=True, default="pending")
    budget: Mapped[str | None] = mapped_column(String(255))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str | None] = mapped_column(String(14))
    reason: Mapped[str] = mapped_column(String(300), default="")
    note: Mapped[str | None] = mapped_column(String(60))
    model: Mapped[str | None] = mapped_column(String(100))
    month: Mapped[str] = mapped_column(String(7))
    group_id: Mapped[str | None] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)
    applied_at: Mapped[datetime | None] = mapped_column(AwareDateTime())


# --- ServerCapture ---
class Receipt(Base):
    """A photographed receipt reduced to its structured reading. The image itself is never stored.

    ``data`` is the encrypted JSON of everything free-form (merchant, NIT, items, payment method, split suggestion);
    the plain columns only hold what queries need. ``image_sha256`` (of the metadata-free bytes) makes a repeated upload
    idempotent. ``item_id`` is the registered movement the receipt belongs to (``match_kind`` ``linked``: the owner said
    so; ``matched``: found by amount, date and merchant). ``applied`` records what ``/receipts/{id}/apply`` did.
    """
    __tablename__ = "receipts"
    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    reason: Mapped[str | None] = mapped_column(String(60))
    image_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    item_id: Mapped[int | None] = mapped_column(Integer, index=True)
    match_kind: Mapped[str | None] = mapped_column(String(10))
    total: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    receipt_date: Mapped[date | None] = mapped_column(Date())
    model: Mapped[str | None] = mapped_column(String(100))
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[str | None] = mapped_column(EncryptedText)
    applied: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, index=True)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)


class Bill(Base):
    """A utility bill to pay (water, power, gas, internet...). ``reference`` is always masked (only the last four characters).

    ``status`` is ``pending``, ``paid`` or ``overdue`` (the API also derives ``overdue`` from ``due_date`` between cycles).
    ``paid_item_id`` is the registered payment (inbox item) that settled it, None when marked paid by hand.
    """
    __tablename__ = "bills"
    __table_args__ = (UniqueConstraint("entity_key", "due_date", "amount", name="uq_bills_entity_due_amount"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    entity: Mapped[str] = mapped_column(String(255))
    entity_key: Mapped[str] = mapped_column(String(255), index=True)
    service: Mapped[str] = mapped_column(String(20))
    reference: Mapped[str | None] = mapped_column(String(40))
    period: Mapped[str | None] = mapped_column(String(40))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    issued_on: Mapped[date] = mapped_column(Date())
    due_date: Mapped[date] = mapped_column(Date(), index=True)
    status: Mapped[str] = mapped_column(String(10), index=True, default="pending")
    paid_item_id: Mapped[int | None] = mapped_column(Integer, index=True)
    paid_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    source: Mapped[str] = mapped_column(String(10), default="manual")
    source_item_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)


class BillCandidate(Base):
    """An e-mail the processor ignored as «not a transaction» that looks like a bill, waiting for the batch extraction.

    ``text`` is the REDACTED subject and body, encrypted, and is erased as soon as the candidate is processed
    (``status`` ``done``, ``skipped`` or ``failed``); the row itself is purged after ``bills.CANDIDATE_RETENTION_DAYS``.
    """
    __tablename__ = "bill_candidates"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(Integer, unique=True)
    sender: Mapped[str] = mapped_column(String(512), default="")
    received_on: Mapped[date] = mapped_column(Date())
    text: Mapped[str | None] = mapped_column(EncryptedText)
    status: Mapped[str] = mapped_column(String(10), index=True, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)



# --- ServerAgent ---
class AgentTask(Base):
    """A long task of the server-side agent (``app.services.agent``). ``kind``: ``owner`` | ``month_close``.

    ``status``: queued | deferred (waiting for the off-peak queue) | running | done | failed | cancelled. ``instruction``,
    ``summary`` and ``report`` are encrypted (they quote the owner's data). The agent only reads and adds proposals to
    ``batch_id`` (``proposal_batches``); it never writes to the ledger.
    """
    __tablename__ = "agent_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), default="owner", index=True)
    instruction: Mapped[str] = mapped_column(EncryptedText, default="")
    month: Mapped[str | None] = mapped_column(String(7))
    status: Mapped[str] = mapped_column(String(12), default="queued", index=True)
    run_now: Mapped[bool] = mapped_column(Boolean, default=False)
    auto: Mapped[bool] = mapped_column(Boolean, default=False)
    window_from: Mapped[datetime] = mapped_column(AwareDateTime())
    window_to: Mapped[datetime] = mapped_column(AwareDateTime())
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("proposal_batches.id"))
    job_id: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[str | None] = mapped_column(EncryptedText)
    report: Mapped[str | None] = mapped_column(EncryptedText)
    error_code: Mapped[str | None] = mapped_column(String(40))
    error: Mapped[str | None] = mapped_column(String(300))
    max_calls: Mapped[int] = mapped_column(Integer, default=60)
    calls_used: Mapped[int] = mapped_column(Integer, default=0)
    steps_count: Mapped[int] = mapped_column(Integer, default=0)
    requested_by: Mapped[str] = mapped_column(String(100), default="system")
    device_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, index=True)
    started_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)


class AgentStep(Base):
    """One tool step of an ``AgentTask``: the tool, its arguments and a short summary of the result (encrypted)."""
    __tablename__ = "agent_steps"
    __table_args__ = (UniqueConstraint("task_id", "n", name="uq_agent_steps_task_n"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True)
    n: Mapped[int] = mapped_column(Integer)
    tool: Mapped[str] = mapped_column(String(40))
    args: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(EncryptedText, default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)


class CoachTip(Base):
    """A weekly finding of the coach (``app.services.coach``). ``kind`` is the server-defined finding type; ``useful`` is the
    owner's feedback (None until given). ``detail``, ``suggestion`` and ``evidence`` are encrypted."""
    __tablename__ = "coach_tips"
    id: Mapped[int] = mapped_column(primary_key=True)
    week: Mapped[date] = mapped_column(Date(), index=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str] = mapped_column(EncryptedText)
    evidence: Mapped[str] = mapped_column(EncryptedText, default="[]")
    suggestion: Mapped[str] = mapped_column(EncryptedText, default="")
    model: Mapped[str | None] = mapped_column(String(100))
    useful: Mapped[bool | None] = mapped_column(Boolean)
    feedback_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)


# --- ServerMemory ---
class Memory(Base):
    """Short Spanish note the assistant keeps about the owner (``app.services.memories``). Never holds secrets.

    ``kind``: preference | fact | rule. ``origin``: owner | assistant. ``status``: active | archived.
    ``rule_proposal`` is the structured rule the LLM proposed (``category_rule`` | ``budget_rule`` | ``alert_mute``) and
    ``rule_ref`` points to what approving it created (``rule:<id>``, ``budget_map:<id>``, ``alert_mute:<id>``).
    """
    __tablename__ = "memories"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(10))
    origin: Mapped[str] = mapped_column(String(10), default="owner")
    status: Mapped[str] = mapped_column(String(10), default="active", index=True)
    rule_proposal: Mapped[dict | None] = mapped_column(JSON)
    rule_ref: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)
    last_used_at: Mapped[datetime | None] = mapped_column(AwareDateTime())


class AlertMute(Base):
    """«No avisar de X menor a Y»: ``alerts.emit`` drops an alert of ``alert_type`` (None: any mutable type) about
    ``merchant`` (normalized, None: any) whose amount is below ``below_amount``."""
    __tablename__ = "alert_mutes"
    id: Mapped[int] = mapped_column(primary_key=True)
    alert_type: Mapped[str | None] = mapped_column(String(40))
    merchant: Mapped[str | None] = mapped_column(String(255))
    below_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    memory_id: Mapped[int | None] = mapped_column(ForeignKey("memories.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)


class AlertQuestion(Base):
    """The answerable part of a ``question`` alert (one per alert; one per ``(kind, key)`` ever, so nothing is asked twice).

    ``kind``: category | new_merchant | duplicate | personal. ``choices`` is ``[{id, label}]``; ``payload`` keeps what applying
    needs. ``status``: open | answering | answered | stale (the movement changed before the owner answered).
    """
    __tablename__ = "alert_questions"
    __table_args__ = (UniqueConstraint("kind", "key", name="uq_alert_questions_kind_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id", ondelete="CASCADE"), unique=True)
    kind: Mapped[str] = mapped_column(String(20))
    key: Mapped[str] = mapped_column(String(255))
    item_id: Mapped[int | None] = mapped_column(Integer, index=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    choices: Mapped[list] = mapped_column(JSON, default=list)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)
    answer: Mapped[str | None] = mapped_column(String(40))
    claimed_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, index=True)
    answered_at: Mapped[datetime | None] = mapped_column(AwareDateTime())


# --- ServerQuality ---
class AiJob(Base):
    """A heavy AI job that waits for, or goes through, the queue of ``app.services.ai_jobs``.

    ``status``: queued (runs on the next tick), waiting_offpeak (parked until ``run_after``, the start of the next valley, or
    until the owner runs it), running, done, failed, cancelled. ``decided_by``: ``policy`` (``peak_policy`` or the valley decided)
    or ``owner`` (run now / wait). ``runner_key`` names the registered runner; ``payload`` is its small JSON argument.
    """
    __tablename__ = "ai_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), index=True, default="queued")
    runner_key: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(120), index=True)
    estimated_calls: Mapped[int] = mapped_column(Integer, default=1)
    estimated_allowance_pct: Mapped[float | None] = mapped_column(Float)
    model: Mapped[str | None] = mapped_column(String(100))
    run_after: Mapped[datetime | None] = mapped_column(AwareDateTime())
    decided_by: Mapped[str | None] = mapped_column(String(10))
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)
    started_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(AwareDateTime())


class AiEvent(Base):
    """A trip or event the server detected among the registered expenses (``app.services.ai_events``): a tag proposal for a
    block of movements. ``status``: pending (owner decides), applied (``group_id`` is the ledger group to undo), discarded,
    stale (its movements changed). ``item_ids`` are inbox ids; ``name_source``: rule | llm."""
    __tablename__ = "ai_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(80), unique=True)
    label: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(10), default="trip")
    city: Mapped[str | None] = mapped_column(String(60))
    start_date: Mapped[date] = mapped_column(Date())
    end_date: Mapped[date] = mapped_column(Date())
    item_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    name_source: Mapped[str] = mapped_column(String(10), default="rule")
    reason: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(10), index=True, default="pending")
    group_id: Mapped[str | None] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)
    applied_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
