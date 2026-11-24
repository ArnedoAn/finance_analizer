"""Debts, incomes and the official exchange rate (finance plan, v4).

Nothing here is encrypted, like the other money tables (``bills``, budgets, ``own_accounts``): the columns hold names the
owner typed and figures, never message text, and the database is only reachable through the authenticated API. The
free text of the app that IS sensitive (``raw_text``, statement descriptions, notes) uses ``EncryptedText``; the
``payment_match`` and ``match`` patterns are short merchant or counterparty names, stored in JSON like ``own_accounts.aliases``.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, Date, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timezone import now_utc
from app.db.database import Base
from app.db.models import AwareDateTime


class Debt(Base):
    """A loan or credit card the owner pays off. The balance is NOT stored: Firefly owns it (``firefly_account_id``).

    ``rate_ea`` is a percentage (``28.50``). ``firefly_account`` is the account name as Firefly has it, the key the
    processor uses to recognize the payment as a transfer to the liability. ``active`` false is a soft delete.
    """
    __tablename__ = "debts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(12))  # loan | credit_card
    lender: Mapped[str] = mapped_column(String(100), default="")
    firefly_account_id: Mapped[str] = mapped_column(String(40))
    firefly_account: Mapped[str] = mapped_column(String(255))
    currency: Mapped[str] = mapped_column(String(3), default="COP")
    original_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    rate_ea: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    term_months: Mapped[int | None] = mapped_column(Integer)
    start_date: Mapped[date | None] = mapped_column(Date())
    installment: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    payment_day: Mapped[int | None] = mapped_column(Integer)
    insurance_monthly: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal(0))
    credit_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    minimum_payment: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    payment_match: Mapped[list[str]] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)


class Income(Base):
    """A source of income: ``fixed`` ones count in the month's plan, ``variable`` ones only when they arrive."""
    __tablename__ = "incomes"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(10))  # fixed | variable
    currency: Mapped[str] = mapped_column(String(3), default="COP")
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    day_of_month: Mapped[int | None] = mapped_column(Integer)
    match: Mapped[list[str]] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc, onupdate=now_utc)


class FxRate(Base):
    """One TRM (COP per USD) and the days it governs: ``valid_from`` to ``valid_until`` inclusive (weekends share Friday's)."""
    __tablename__ = "fx_rates"
    valid_from: Mapped[date] = mapped_column(Date(), primary_key=True)
    valid_until: Mapped[date] = mapped_column(Date())
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    source: Mapped[str] = mapped_column(String(40), default="datos.gov.co")
    fetched_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=now_utc)
