"""Audited regressions and representative golden messages, grouped by bank."""

import json
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.parsers import normalize_merchant, parse, parse_amount

FIXTURES = Path(__file__).parent / "fixtures"
CASES = [(path.stem, index, case)
         for path in sorted(FIXTURES.glob("*.json"))
         for index, case in enumerate(json.loads(path.read_text(encoding="utf-8")))]


@pytest.mark.parametrize("bank,index,case", CASES, ids=[f"{b}-{i}" for b, i, _ in CASES])
def test_bank_golden(bank, index, case):
    result = parse(case["text"], case["subject"], case["sender"])
    assert result is not None, (bank, index)
    actual = asdict(result)
    for key, value in actual.items():
        if isinstance(value, Decimal):
            actual[key] = str(value)
        elif isinstance(value, (datetime, date)):
            actual[key] = value.isoformat()
    for key, expected in case["expected"].items():
        assert actual[key] == expected, (bank, index, key, case["text"])
    if result.amount is not None:
        assert result.amount.as_tuple().exponent == -2
    if result.occurred_at:
        assert result.occurred_at.utcoffset() is not None


@pytest.mark.parametrize("raw,currency,expected", [
    ("1", "COP", "1.00"), ("50000", "COP", "50000.00"),
    ("50.000", "COP", "50000.00"), ("1.234.567,00", "COP", "1234567.00"),
    ("50000.00", "COP", "50000.00"), ("50,000.00", "COP", "50000.00"),
    ("25.99", "USD", "25.99"), ("1,234.56", "USD", "1234.56"),
    ("1.5", "USD", "1.50"), ("25", "USD", "25.00"),
])
def test_strict_amounts(raw, currency, expected):
    assert parse_amount(raw, currency) == Decimal(expected)


@pytest.mark.parametrize("raw,currency", [
    ("1.5", "COP"), ("2.5", "COP"), ("0.500", "COP"), ("5.000,000", "COP"),
    ("5 000", "COP"), ("1,234", "COP"), ("1.000,50", "COP"), ("0", "COP"),
    ("-1", "COP"), ("NaN", "COP"), ("1.000", "USD"), ("25,99", "USD"),
    ("1.00", "EUR"),
])
def test_ambiguous_amounts_raise(raw, currency):
    with pytest.raises(ValueError):
        parse_amount(raw, currency)


@pytest.mark.parametrize("raw,expected", [
    ("PAYU*TIENDA", "TIENDA"), ("UBR* CAFE", "CAFE"),
    ("TIENDA. Saldo $123", "TIENDA"), ("TIENDA $123", ""),
    ("TIENDA 123456", ""), ("X" * 61, ""),
])
def test_merchant_normalization(raw, expected):
    assert normalize_merchant(raw) == expected
    assert normalize_merchant(expected) == expected


def test_unknown_message_falls_through():
    assert parse("Un mensaje que no describe una transacción") is None


def test_due_date_with_el_is_not_transaction_date():
    result = parse("Compraste $50.000 en TIENDA. Vence el 15/12/2027")
    assert result is not None
    assert result.occurred_at is None
    assert result.date_only is None
