from decimal import Decimal

import pytest

from app.parsers import normalize_merchant, parse_transaction, should_ignore


@pytest.mark.parametrize("message", [
    "Compra rechazada por $50.000 en TIENDA",
    "Compra declinada por fondos insuficientes",
    "La compra no fue aprobada", "Fecha de pago 20 de noviembre",
    "Tu próximo pago vence mañana", "Tu extracto está disponible",
    "Código de verificación: 123456", "Tu clave dinámica es 123456",
    "OTP: 123456",
])
def test_ignore_non_transactions(message):
    assert should_ignore(message)
    assert parse_transaction(message) is None


def test_ignore_subject_takes_precedence():
    assert should_ignore("Compraste $50.000 en TIENDA", "Compra rechazada")
    assert parse_transaction("Compraste $50.000 en TIENDA", "Compra rechazada") is None


@pytest.mark.parametrize("subject", ["Promoción exclusiva: compra hoy", "Descuento del 20%", "Oferta especial", "Preaprobado"])
def test_promotions_only_ignore_subject(subject):
    assert should_ignore("Mensaje publicitario", subject)
    assert not should_ignore("Compraste $20.000 en TIENDA con tu tarjeta. " + subject)
    assert parse_transaction("Compraste $20.000 en TIENDA con tu tarjeta. " + subject) is not None


@pytest.mark.parametrize("raw,expected", [
    ("  payu*  Tienda   Uno ", "TIENDA UNO"),
    ("DLO*UBR*Uber Eats", "UBER EATS"),
    ("mercadopago* Comercio", "COMERCIO"),
    ("Mercado Pago", "MERCADO PAGO"),
    ("", ""),
])
def test_merchant_normalization(raw, expected):
    assert normalize_merchant(raw) == expected
    assert normalize_merchant(expected) == expected


@pytest.mark.parametrize("amount,expected", [
    ("50.000", "50000"), ("50,000", "50000"), ("50.000,25", "50000.25"),
    ("50,000.25", "50000.25"), ("1250.50", "1250.50"), ("1250,50", "1250.50"),
    ("50 000", "50000"),
])
def test_purchase_amounts(amount, expected):
    transaction = parse_transaction(f"Bancolombia: Compraste ${amount} en PAYU*TIENDA con tu tarjeta *1234")
    assert transaction.amount == Decimal(expected)
    assert transaction.merchant == "TIENDA"
    assert transaction.currency == "COP"
    assert transaction.transaction_type == "withdrawal"
    assert transaction.date is None


def test_explicit_dollar_currency_and_time():
    transaction = parse_transaction("Compraste US$25.99 en AMAZON el 2026-11-16 13:45")
    assert transaction.currency == "USD"
    assert transaction.date == "2026-11-16T13:45:00-05:00"
    assert transaction.merchant == "AMAZON"


def test_date_only_is_not_assigned_an_invented_time():
    transaction = parse_transaction("Pagaste $25.000 en TIENDA el 16/11/2026")
    assert transaction.date == "2026-11-16"


def test_explicit_offset_is_preserved():
    transaction = parse_transaction("Compraste $20 en TIENDA el 2026-11-16T10:30:15Z")
    assert transaction.date == "2026-11-16T10:30:15Z"


def test_deposit_and_transfer():
    deposit = parse_transaction("Nequi: Recibiste $25.000 de JUAN en tu cuenta")
    assert deposit.transaction_type == "deposit"
    assert deposit.merchant == "JUAN"
    transfer = parse_transaction("Enviaste $12.000 a MARIA el 16/11/2026")
    assert transfer.transaction_type == "withdrawal"
    assert transfer.merchant == "MARIA"


@pytest.mark.parametrize("message", [
    "Tu saldo es $50.000", "Puede comprar por $50.000", "Compraste $0 en TIENDA",
    "Compraste $1.23.456 en TIENDA", "Un texto cualquiera", "Compra sin monto",
])
def test_unknown_or_invalid_templates_fall_through(message):
    assert parse_transaction(message) is None


@pytest.mark.parametrize("date,expected", [
    ("16/11/26 a las 01:45 PM", "2026-11-16T13:45:00-05:00"),
    ("2026/11/16 12:01 AM", "2026-11-16T00:01:00-05:00"),
    ("16/11/2026 12:01 PM", "2026-11-16T12:01:00-05:00"),
])
def test_short_dates_and_twelve_hour_clock(date, expected):
    transaction = parse_transaction(f"Compraste $10.000 en TIENDA el {date}")
    assert transaction.date == expected
