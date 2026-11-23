"""Rows of a bank statement and the extension point for deterministic per-bank parsers.

``ParsedRow`` and ``ParsedBlock`` are the only shapes the statement pipeline understands, whether the
rows come from the LLM (``LLMClient.statement_rows``) or, in the future, from a bank-specific parser.
Money is ``Decimal`` with two decimals; ``amount`` is always positive and ``direction`` says whether it
leaves (``debit``) or enters (``credit``) the account.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

Direction = Literal["debit", "credit"]


@dataclass(frozen=True, slots=True)
class ParsedRow:
    date: date
    description: str
    amount: Decimal
    direction: Direction
    balance: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    """What one piece of the statement text yielded.

    ``discarded`` counts model rows that failed validation; balances and period are only set when the text shows them.
    """
    rows: tuple[ParsedRow, ...] = ()
    discarded: int = 0
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None
    period_from: date | None = None
    period_to: date | None = None


def parse_known_bank(pages: list[str]) -> ParsedBlock | None:
    """Deterministic parser for statement layouts that are known exactly; ``None`` means «unknown, use the LLM».

    Extension point for Bancolombia, Nu and Lulo. Each bank would need its own recognizer (a marker that
    only that bank's statement carries) and a row parser anchored on that bank's real column layout, the
    way ``app.parsers.<bank>`` does for SMS and e-mail templates. No layout is implemented on purpose:
    guessing column formats without real samples would silently produce wrong rows. When adding one,
    return a complete ``ParsedBlock`` (all pages, balances and period when printed) or ``None``; never
    a partial result, and add fixtures built from invented data, never from a real statement.
    """
    return None
