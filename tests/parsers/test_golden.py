"""Audited regressions and representative golden messages, grouped by bank."""

import json
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.parsers import is_placeholder_merchant, normalize_merchant, parse, parse_amount

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
    ("1.651.063", "COP", "1651063.00"), ("1,651,063.00", "COP", "1651063.00"),
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


DAVIVIENDA_NOTICE = (
    "Apreciado(a) CLIENTE:\n\nLe informamos que se ha registrado el siguiente movimiento de su Cta de Ahorros "
    "terminada (o) en ****0000:\n\nFecha: 2026/11/15\nHora: 03:28:30\nValor Transacción: {amount}\n"
    "Clase de Movimiento: {movement}\nLugar de Transacción: {place}\n\nAtentamente,\n\nBANCO DAVIVIENDA S.A."
)


def davivienda(amount="$1,651,063", movement="Descuento Pago Tarjeta de Credito,", place="App Davivienda", subject="Alerta Davivienda"):
    return parse(DAVIVIENDA_NOTICE.format(amount=amount, movement=movement, place=place), subject, "alertas@davivienda.com")


@pytest.mark.parametrize("amount", ["$1,651,063", "$1.651.063", "$1,651,063.00", "$ 1651063", "1,651,063"])
def test_davivienda_notice_amount_groupings(amount):
    result = davivienda(amount)
    assert result is not None and result.kind == "withdrawal" and result.amount == Decimal("1651063.00")


def test_davivienda_notice_ambiguous_amount_goes_to_review():
    result = davivienda("$1.65")
    assert result is not None and result.kind == "review" and result.amount is None and result.reason


@pytest.mark.parametrize("movement", ["Cuota de Manejo", "Reverso Compra", "Devolucion Pago Tarjeta de Credito"])
def test_davivienda_notice_unknown_or_reversed_class_falls_through(movement):
    assert davivienda(movement=movement) is None


def test_davivienda_notice_declined_is_ignored():
    result = davivienda(movement="Compra rechazada")
    assert result is not None and result.kind == "ignore"


def test_davivienda_notice_sent_transfer_and_received_transfer():
    assert davivienda(movement="Transferencia a otra cuenta").kind == "withdrawal"
    assert davivienda(movement="Transferencia recibida").kind == "deposit"


def test_davivienda_notice_placeholder_merchant_stays_a_placeholder():
    result = davivienda(movement="Compra", place="PENDING")
    assert result is not None and is_placeholder_merchant(result.merchant)


def test_davivienda_notice_subject_word_descuento_does_not_ignore_it():
    result = davivienda(subject="Descuento en su cuenta")
    assert result is not None and result.kind == "withdrawal" and result.merchant == "PAGO TARJETA DE CREDITO"
