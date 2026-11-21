"""Phase 2: raw_text column encryption, redaction battery, scope-gated raw access and 30-day purge."""
import base64
import importlib.util
import os
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr, ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import StatementError

from app.core.config import Settings
from app.core.timezone import now_utc
from app.db.models import InboxItem
from app.security import crypto
from app.security.crypto import (
    DecryptionError,
    EncryptedText,
    KeyNotConfiguredError,
    decrypt,
    encrypt,
    parse_key,
)
from app.security.redaction import redact

ADMIN_KEY = "test-admin-key-01234567890123456789"
API = "/api/v1"
ADMIN = {"X-API-Key": ADMIN_KEY}
MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "20261122_02_encrypt_raw_text.py"


def b64_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def root_cause(error):
    return error.orig if isinstance(error, StatementError) and error.orig is not None else error


async def add_item(db, **values):
    row = InboxItem(source="sms", source_ref=str(uuid4()), sender="85954", subject_or_title="Compra", **values)
    db.add(row)
    await db.commit()
    return row


@pytest.fixture
def no_master_key(monkeypatch):
    monkeypatch.setattr(crypto, "get_settings", lambda: SimpleNamespace(field_encryption_key=SecretStr("")))
    crypto.master_key.cache_clear()
    yield
    crypto.master_key.cache_clear()


# --- Cipher -----------------------------------------------------------------------------------


def test_token_format_nonce_and_roundtrip():
    key = os.urandom(32)
    message = "Pagaste $12.000 en café ñandú"
    first, second = encrypt(message, key), encrypt(message, key)
    assert first != second and first.startswith("v1:") and "Pagaste" not in first
    blob = base64.b64decode(first[3:], validate=True)
    assert len(blob) == 12 + len(message.encode()) + 16
    assert decrypt(first, key) == decrypt(second, key) == message
    assert decrypt(encrypt("", key), key) == ""


def test_wrong_key_tampering_and_other_column_do_not_decrypt():
    key = os.urandom(32)
    token = encrypt("Pagaste $12.000", key)
    with pytest.raises(DecryptionError):
        decrypt(token, os.urandom(32))
    blob = bytearray(base64.b64decode(token[3:]))
    blob[-1] ^= 1
    with pytest.raises(DecryptionError):
        decrypt("v1:" + base64.b64encode(bytes(blob)).decode(), key)
    nonce = os.urandom(12)
    foreign = "v1:" + base64.b64encode(nonce + AESGCM(key).encrypt(nonce, b"x", b"other.column")).decode()
    with pytest.raises(DecryptionError):
        decrypt(foreign, key)
    for broken in ("v1:!!!not-base64!!!", "v1:" + base64.b64encode(b"short").decode(), "plain text"):
        with pytest.raises(DecryptionError):
            decrypt(broken, key)


def test_parse_key_requires_base64_of_32_bytes():
    raw = os.urandom(32)
    assert parse_key(base64.b64encode(raw).decode() + "\n") == raw
    for bad in ("", "not base64!", base64.b64encode(os.urandom(16)).decode(), base64.b64encode(os.urandom(33)).decode()):
        with pytest.raises(ValueError):
            parse_key(bad)


def test_type_passes_none_and_legacy_plaintext_through():
    column = EncryptedText()
    assert column.process_bind_param(None, None) is None
    assert column.process_result_value(None, None) is None
    assert column.process_result_value("texto heredado", None) == "texto heredado"
    token = column.process_bind_param("hola", None)
    assert token.startswith("v1:") and column.process_result_value(token, None) == "hola"


def test_type_with_other_key_raises_instead_of_returning_garbage(monkeypatch):
    token = EncryptedText().process_bind_param("secreto", None)
    other = os.urandom(32)
    monkeypatch.setattr(crypto, "master_key", lambda: other)
    with pytest.raises(DecryptionError):
        EncryptedText().process_result_value(token, None)


def test_without_key_raw_text_is_refused_but_null_is_fine(no_master_key):
    column = EncryptedText()
    assert column.process_bind_param(None, None) is None
    assert column.process_result_value("texto heredado", None) == "texto heredado"
    with pytest.raises(KeyNotConfiguredError, match="FIELD_ENCRYPTION_KEY"):
        column.process_bind_param("Pagaste $12.000", None)
    with pytest.raises(KeyNotConfiguredError):
        column.process_result_value("v1:AAAA", None)


# --- Settings validator -----------------------------------------------------------------------


def make_settings(**changes):
    return Settings(
        finance_api_key=ADMIN_KEY, phone_api_key="test-phone-key-01234567890123456789",
        mail_accounts=[], own_accounts=[], _env_file=None, **changes,
    )


def test_settings_validate_field_encryption_key():
    good = b64_key()
    assert make_settings(field_encryption_key=good).field_encryption_key.get_secret_value() == good
    assert make_settings(field_encryption_key="").app_env == "development"
    with pytest.raises(ValidationError) as caught:
        make_settings(field_encryption_key="not-a-valid-key")
    assert "not-a-valid-key" not in str(caught.value)
    with pytest.raises(ValidationError):
        make_settings(field_encryption_key=base64.b64encode(os.urandom(16)).decode())


def test_production_requires_field_encryption_key():
    with pytest.raises(ValidationError, match="FIELD_ENCRYPTION_KEY"):
        make_settings(app_env="production", field_encryption_key="")
    assert make_settings(app_env="production", field_encryption_key=b64_key()).is_production


# --- Database: ciphertext at rest -------------------------------------------------------------


async def test_sql_shows_ciphertext_and_orm_returns_plaintext(db):
    secret = "Bancolombia: Compraste $52.900,00 con la cuenta 0550012345678"
    row = await add_item(db, raw_text=secret)
    empty = await add_item(db, raw_text=None)
    stored = (await db.execute(text("SELECT raw_text FROM inbox_items ORDER BY id"))).scalars().all()
    assert stored[0].startswith("v1:")
    assert "Bancolombia" not in stored[0] and "0550012345678" not in stored[0] and secret not in stored[0]
    assert stored[1] is None
    await db.refresh(row)
    await db.refresh(empty)
    assert row.raw_text == secret and empty.raw_text is None
    assert (await db.execute(select(InboxItem.raw_text).where(InboxItem.id == row.id))).scalar_one() == secret


async def test_orm_read_with_other_key_fails(db, monkeypatch):
    row = await add_item(db, raw_text="secreto")
    other = os.urandom(32)
    monkeypatch.setattr(crypto, "master_key", lambda: other)
    with pytest.raises(Exception) as caught:
        await db.refresh(row)
    assert isinstance(root_cause(caught.value), DecryptionError)
    await db.rollback()


# --- Migration --------------------------------------------------------------------------------


def load_migration():
    spec = importlib.util.spec_from_file_location("encrypt_raw_text_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def legacy_db():
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table("inbox_items", metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("raw_text", sa.Text))
    metadata.create_all(engine)
    key = os.urandom(32)
    kept = encrypt("ya cifrado", key)
    with engine.begin() as connection:
        connection.execute(table.insert(), [
            {"id": 1, "raw_text": "Pagaste $12.000"}, {"id": 2, "raw_text": None},
            {"id": 3, "raw_text": "Cuenta 0550012345678"}, {"id": 4, "raw_text": kept},
            {"id": 5, "raw_text": ""}, {"id": 6, "raw_text": "Recibiste $1.500"},
        ])
    yield engine, table, key, kept
    engine.dispose()


def run_upgrade(engine, module):
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        module.upgrade()


def stored_rows(engine, table):
    with engine.connect() as connection:
        return dict(connection.execute(sa.select(table.c.id, table.c.raw_text)).all())


@pytest.mark.parametrize("use_file", [False, True])
def test_migration_encrypts_existing_rows_in_batches(legacy_db, monkeypatch, tmp_path, use_file):
    engine, table, key, kept = legacy_db
    encoded = base64.b64encode(key).decode()
    if use_file:
        key_file = tmp_path / "field.key"
        key_file.write_text(encoded + "\n")
        monkeypatch.setenv("FIELD_ENCRYPTION_KEY_FILE", str(key_file))
        monkeypatch.setenv("FIELD_ENCRYPTION_KEY", b64_key())  # the file wins
    else:
        monkeypatch.delenv("FIELD_ENCRYPTION_KEY_FILE", raising=False)
        monkeypatch.setenv("FIELD_ENCRYPTION_KEY", encoded)
    module = load_migration()
    module.BATCH_SIZE = 2
    run_upgrade(engine, module)
    rows = stored_rows(engine, table)
    assert rows[2] is None and rows[4] == kept
    assert {i: decrypt(rows[i], key) for i in (1, 3, 5, 6)} == {
        1: "Pagaste $12.000", 3: "Cuenta 0550012345678", 5: "", 6: "Recibiste $1.500",
    }
    assert "0550012345678" not in rows[3]
    run_upgrade(engine, module)  # idempotent: encrypted rows are left alone
    assert stored_rows(engine, table) == rows


def test_migration_without_key_fails_before_changing_anything(legacy_db, monkeypatch):
    engine, table, _, _ = legacy_db
    before = stored_rows(engine, table)
    monkeypatch.delenv("FIELD_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("FIELD_ENCRYPTION_KEY_FILE", raising=False)
    with pytest.raises(RuntimeError, match="FIELD_ENCRYPTION_KEY"):
        run_upgrade(engine, load_migration())
    assert stored_rows(engine, table) == before
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", "not-a-key")
    with pytest.raises(RuntimeError, match="no data was changed"):
        run_upgrade(engine, load_migration())
    assert stored_rows(engine, table) == before


def test_migration_needs_no_key_when_nothing_to_encrypt(monkeypatch):
    monkeypatch.delenv("FIELD_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("FIELD_ENCRYPTION_KEY_FILE", raising=False)
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table("inbox_items", metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("raw_text", sa.Text))
    metadata.create_all(engine)
    run_upgrade(engine, load_migration())
    with pytest.raises(RuntimeError):
        load_migration().downgrade()
    engine.dispose()


# --- Redaction --------------------------------------------------------------------------------

BATTERY = [
    # Bancolombia purchase SMS: masked card, toll line, amount with cents, merchant and time kept.
    (
        "Bancolombia: Compraste $52.900,00 en EXITO CALLE 80 con tu T.Deb *1234, el 21/11/2026 a las 14:35. Inquietudes al 6045109095.",
        "Bancolombia: Compraste $52.900,00 en EXITO CALLE 80 con tu T.Deb [CUENTA-1234], el 21/11/2026 a las 14:35. Inquietudes al [TEL].",
    ),
    # Davivienda ATM withdrawal: full 13-digit savings account and support line.
    (
        "Davivienda le informa: Se realizo un retiro por $300.000 en el cajero ATM CC UNICENTRO con la cuenta de ahorros No. 0550012345678 el 21/11/2026 a las 09:45. Si no reconoce esta operacion llame al 6016020088.",
        "Davivienda le informa: Se realizo un retiro por $300.000 en el cajero ATM CC UNICENTRO con la cuenta de ahorros No. [CUENTA-5678] el 21/11/2026 a las 09:45. Si no reconoce esta operacion llame al [TEL].",
    ),
    # Nequi: greeting name, own mobile number (the Nequi account), e-mail and toll-free line.
    (
        "Nequi: Hola Carlos Andres, recibiste $100.000 en tu Nequi 3001234567 el 21/11/2026 a las 08:03. Para ayuda escribe a soporte@nequi.com.co o llama al 018000912345.",
        "Nequi: Hola [NOMBRE], recibiste $100.000 en tu Nequi [TEL] el 21/11/2026 a las 08:03. Para ayuda escribe a [EMAIL] o llama al [TEL].",
    ),
    # OTP next to a label; the date and the amount survive.
    (
        "Bancolombia: Tu clave dinamica es 482913. No la compartas. Valida en EXITO por $45.900 el 21/11/2026.",
        "Bancolombia: Tu clave dinamica es [CODIGO]. No la compartas. Valida en EXITO por $45.900 el 21/11/2026.",
    ),
    (
        "Davivienda: Su codigo OTP es 735192 y vence en 5 minutos. Compra en AMAZON por USD 12.50",
        "Davivienda: Su codigo OTP es [CODIGO] y vence en 5 minutos. Compra en AMAZON por USD 12.50",
    ),
    # Full card number and cardholder after a marker.
    (
        "Davivienda: Compra aprobada por $85.400 en FARMATODO con la tarjeta 4513 1234 5678 9012 el 21/11/2026 11:20. Titular de la tarjeta: Ana Maria Rojas.",
        "Davivienda: Compra aprobada por $85.400 en FARMATODO con la tarjeta [CUENTA-9012] el 21/11/2026 11:20. Titular de la tarjeta: [NOMBRE].",
    ),
    # Company NIT with verification digit.
    (
        "Pago PSE a ALMACENES EXITO SA NIT 890.900.608-9 por $320.000 el 21/11/2026. Referencia 12345.",
        "Pago PSE a ALMACENES EXITO SA NIT [ID] por $320.000 el 21/11/2026. Referencia 12345.",
    ),
    # P2P transfer: «para» + Title-case name is a person.
    (
        "Nequi: Enviaste $50.000 para Carlos Gomez. Tu saldo es $120.500. Ref M1234567",
        "Nequi: Enviaste $50.000 para [NOMBRE]. Tu saldo es $120.500. Ref M1234567",
    ),
    (
        "Hola Laura, tu pago de $89.900 a CLARO HOGAR fue exitoso. Cuenta de ahorros terminada en 8765. Si tienes dudas llama a la linea (601) 744 4000.",
        "Hola [NOMBRE], tu pago de $89.900 a CLARO HOGAR fue exitoso. Cuenta de ahorros terminada en 8765. Si tienes dudas llama a la linea [TEL].",
    ),
    # Texts without personal data stay byte-identical (amounts, merchants, dates, times).
    ("Pagaste $12.000 en SUPERMERCADO CENTRAL", "Pagaste $12.000 en SUPERMERCADO CENTRAL"),
    ("Pagaste $18.000 para Rappi el 21/11/2026 a las 19:02", "Pagaste $18.000 para Rappi el 21/11/2026 a las 19:02"),
    ("Estimado cliente, compraste $1.250.000,00 en Falabella 12.345", "Estimado cliente, compraste $1.250.000,00 en Falabella 12.345"),
    ("Estimado Cliente: compra en D1 por $7.800", "Estimado Cliente: compra en D1 por $7.800"),
]


@pytest.mark.parametrize(("raw", "expected"), BATTERY)
def test_redaction_battery(raw, expected):
    assert redact(raw) == expected
    assert redact(expected) == expected  # idempotent: redacting twice changes nothing


def test_redaction_of_a_bancolombia_email_with_name_id_account_and_contact():
    raw = (
        "Estimado(a) Sr(a). JUAN PEREZ GOMEZ:\n"
        "Bancolombia le confirma el pago de su factura de ENEL CODENSA por $215.430,00 desde la cuenta de ahorros 123-456789-12.\n"
        "Titular: JUAN PEREZ GOMEZ\nC.C. 1.098.765.432\n"
        "Fecha: 21/11/2026 Hora: 16:20\nContacto: juan.perez@gmail.com Tel. 3105558899"
    )
    out = redact(raw)
    for kept in ("$215.430,00", "ENEL CODENSA", "21/11/2026", "16:20", "[NOMBRE]", "[ID]", "[EMAIL]", "[TEL]", "[CUENTA-8912]"):
        assert kept in out
    for hidden in ("JUAN", "PEREZ", "1.098.765.432", "123-456789-12", "juan.perez", "3105558899"):
        assert hidden not in out
    assert redact(out) == out


def test_redaction_of_a_transfer_with_name_after_a_nombre_de():
    out = redact(
        "Bancolombia le informa que recibio una transferencia por $1.500.000 a nombre de Maria Fernanda Lopez Gomez "
        "en la cuenta de ahorros *5678 el 21/11/2026 a las 10:12."
    )
    for kept in ("$1.500.000", "21/11/2026", "10:12", "[CUENTA-5678]", "[NOMBRE]"):
        assert kept in out
    assert "Maria" not in out and "Lopez" not in out


def test_redaction_is_pure_and_handles_empty_values():
    assert redact(None) is None and redact("") == ""
    sample = "Cuenta 0550012345678 de Hola Pedro Gomez"
    assert redact(sample) == redact(sample)


# --- API: scope-gated raw text ---------------------------------------------------------------

RAW_SMS = "Bancolombia: Hola Maria Lopez, compraste $52.900,00 en EXITO con tu cuenta 0550012345678 el 21/11/2026."


async def device_headers(client, scopes):
    code = (await client.post(f"{API}/devices/enroll-token", headers=ADMIN, json={"scopes": scopes})).json()["code"]
    response = await client.post(f"{API}/devices/enroll", json={"code": code, "name": "test", "scopes": scopes})
    assert response.status_code == 201, response.text
    return {"X-API-Key": response.json()["key"]}


async def test_include_raw_needs_review_scope_and_redacted_variant_hides_accounts(client, db):
    row = await add_item(db, raw_text=RAW_SMS)
    reader = await device_headers(client, ["read"])
    reviewer = await device_headers(client, ["read", "review"])

    assert (await client.get(f"{API}/inbox/{row.id}?include_raw=true", headers=reader)).status_code == 403

    plain = await client.get(f"{API}/inbox/{row.id}", headers=reader)
    assert plain.status_code == 200 and "raw_text" not in plain.json()
    redacted = plain.json()["raw_text_redacted"]
    assert "0550012345678" not in redacted and "[CUENTA-5678]" in redacted
    assert "Maria" not in redacted and "$52.900,00" in redacted and "EXITO" in redacted

    for headers in (reviewer, ADMIN):
        full = await client.get(f"{API}/inbox/{row.id}?include_raw=true", headers=headers)
        assert full.status_code == 200
        assert full.json()["raw_text"] == RAW_SMS and full.json()["raw_text_redacted"] == redacted


# --- Purge ------------------------------------------------------------------------------------

STATUSES = ["pending", "submitting", "created", "needs_review", "failed", "voided", "ignored", "rejected", "duplicate"]


async def test_purge_deletes_raw_text_after_30_days_in_every_status(db, service):
    assert service.settings.raw_text_retention_days == 30
    for status in STATUSES:
        for label, age in (("old", 31), ("fresh", 29)):
            db.add(InboxItem(
                source="sms", source_ref=f"{status}-{label}", sender="85954", status=status,
                raw_text="Pagaste $12.000", created_at=now_utc() - timedelta(days=age),
            ))
    await db.commit()
    await service._purge()
    kept = dict((await db.execute(select(InboxItem.source_ref, InboxItem.raw_text))).all())
    for status in STATUSES:
        assert kept[f"{status}-old"] is None, status
        assert kept[f"{status}-fresh"] == "Pagaste $12.000", status
