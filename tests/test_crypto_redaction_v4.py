"""Phase 2: raw_text column encryption, redaction battery, scope-gated raw access and 30-day purge."""
import base64
import hashlib
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

from app.api.routers.common import PUBLIC_ITEM_FIELDS, item_data
from app.core.config import Settings
from app.core.timezone import now_utc
from app.db.models import InboxItem
from app.security import crypto
from app.security.crypto import (
    UNDECRYPTABLE_TEXT,
    DecryptionError,
    EncryptedText,
    KeyNotConfiguredError,
    decrypt,
    encrypt,
    key_id,
    parse_key,
    parse_old_keys,
    token_key_id,
)
from app.security.redaction import redact
from app.security.sensitive_codes import is_sensitive_code

ADMIN_KEY = "test-admin-key-01234567890123456789"
API = "/api/v1"
ADMIN = {"X-API-Key": ADMIN_KEY}
MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "20261122_02_encrypt_raw_text.py"


def b64_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


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


@pytest.fixture
def warnings(monkeypatch):
    """Records what ``crypto`` logs, as (event, fields) pairs."""
    calls = []
    monkeypatch.setattr(crypto, "logger", SimpleNamespace(warning=lambda event, **fields: calls.append((event, fields))))
    return calls


def use_keys(monkeypatch, active, retired=()):
    monkeypatch.setattr(crypto, "master_key", lambda: active)
    monkeypatch.setattr(crypto, "old_keys", lambda: tuple(retired))


# --- Cipher -----------------------------------------------------------------------------------


def test_token_format_has_prefix_key_id_nonce_and_roundtrip():
    key = os.urandom(32)
    message = "Pagaste $12.000 en café ñandú"
    first, second = encrypt(message, key), encrypt(message, key)
    kid = hashlib.sha256(key).hexdigest()[:8]
    prefix = f"enc:v1:{kid}:"
    assert first != second and first.startswith(prefix) and "Pagaste" not in first
    assert key_id(key) == token_key_id(first) == kid
    blob = base64.b64decode(first[len(prefix):], validate=True)
    assert len(blob) == 12 + len(message.encode()) + 16
    assert decrypt(first, key) == decrypt(second, key) == message
    assert decrypt(encrypt("", key), key) == ""


def test_wrong_key_tampering_and_other_column_do_not_decrypt():
    key = os.urandom(32)
    prefix = f"enc:v1:{key_id(key)}:"
    token = encrypt("Pagaste $12.000", key)
    with pytest.raises(DecryptionError):
        decrypt(token, os.urandom(32))  # no key with that id
    other = os.urandom(32)
    with pytest.raises(DecryptionError):  # right id written, wrong key behind it
        decrypt(f"enc:v1:{key_id(other)}:" + token[len(prefix):], other)
    blob = bytearray(base64.b64decode(token[len(prefix):]))
    blob[-1] ^= 1
    with pytest.raises(DecryptionError):
        decrypt(prefix + base64.b64encode(bytes(blob)).decode(), key)
    nonce = os.urandom(12)
    foreign = prefix + base64.b64encode(nonce + AESGCM(key).encrypt(nonce, b"x", b"other.column")).decode()
    with pytest.raises(DecryptionError):
        decrypt(foreign, key)
    broken_values = (
        prefix + "!!!not-base64!!!", prefix + base64.b64encode(b"short").decode(), "plain text",
        "v1:" + token[len(prefix):], "enc:v1:", "enc:v1:nokid", "enc:v1:" + key_id(key) + "AAAA",
    )
    for broken in broken_values:
        with pytest.raises(DecryptionError):
            decrypt(broken, key)


def test_retired_keys_decrypt_values_sealed_before_a_rotation():
    old, new = os.urandom(32), os.urandom(32)
    token = encrypt("Recibiste $30.000 de JUAN", old)
    assert token_key_id(token) == key_id(old) != key_id(new)
    assert decrypt(token, new, (old,)) == "Recibiste $30.000 de JUAN"
    assert decrypt(token, old, (new,)) == "Recibiste $30.000 de JUAN"
    assert decrypt(encrypt("nuevo", new), new, (old,)) == "nuevo"
    with pytest.raises(DecryptionError):
        decrypt(token, new)
    with pytest.raises(DecryptionError):
        decrypt(token, new, (os.urandom(32),))


def test_parse_key_requires_base64_of_32_bytes():
    raw = os.urandom(32)
    assert parse_key(base64.b64encode(raw).decode() + "\n") == raw
    for bad in ("", "not base64!", base64.b64encode(os.urandom(16)).decode(), base64.b64encode(os.urandom(33)).decode()):
        with pytest.raises(ValueError):
            parse_key(bad)


def test_old_keys_list_is_comma_separated_base64_and_errors_do_not_echo_values():
    first, second = os.urandom(32), os.urandom(32)
    encoded = [base64.b64encode(key).decode() for key in (first, second)]
    assert parse_old_keys(f"{encoded[0]}, {encoded[1]}\n") == (first, second)
    assert parse_old_keys("") == () and parse_old_keys(None) == () and parse_old_keys(" , ") == ()
    with pytest.raises(ValueError, match="entry 2") as caught:
        parse_old_keys(f"{encoded[0]},not-a-key")
    assert "not-a-key" not in str(caught.value) and encoded[0] not in str(caught.value)


def test_old_keys_are_read_from_the_file_secret(monkeypatch, tmp_path):
    first, second = os.urandom(32), os.urandom(32)
    path = tmp_path / "old.keys"
    path.write_text(",".join(base64.b64encode(key).decode() for key in (first, second)) + "\n")
    monkeypatch.setenv("FIELD_ENCRYPTION_OLD_KEYS_FILE", str(path))
    monkeypatch.delenv("FIELD_ENCRYPTION_OLD_KEYS", raising=False)
    crypto.old_keys.cache_clear()
    try:
        assert crypto.old_keys() == (first, second)
    finally:
        crypto.old_keys.cache_clear()


def test_type_passes_none_and_legacy_plaintext_through():
    column = EncryptedText()
    assert column.process_bind_param(None, None) is None
    assert column.process_result_value(None, None) is None
    assert column.process_result_value("texto heredado", None) == "texto heredado"
    # A legacy plaintext that merely starts with "v1:" is not ciphertext.
    assert column.process_result_value("v1:texto heredado", None) == "v1:texto heredado"
    token = column.process_bind_param("hola", None)
    assert token.startswith("enc:v1:") and column.process_result_value(token, None) == "hola"


def test_type_with_unknown_key_returns_a_marker_and_logs_without_content(monkeypatch, warnings):
    token = EncryptedText().process_bind_param("secreto 0550012345678", None)
    other = os.urandom(32)
    use_keys(monkeypatch, other)
    assert EncryptedText().process_result_value(token, None) == "[texto cifrado no legible]" == UNDECRYPTABLE_TEXT
    assert warnings == [("raw_text_undecryptable", {"key_id": token_key_id(token)})]
    assert "secreto" not in repr(warnings) and "0550012345678" not in repr(warnings)
    corrupted = ("enc:v1:", "enc:v1:abcd1234:!!!", f"enc:v1:{key_id(other)}:AAAA", token[:-4] + "AAAA")
    assert [EncryptedText().process_result_value(value, None) for value in corrupted] == [UNDECRYPTABLE_TEXT] * 4
    assert len(warnings) == 5 and {event for event, _ in warnings} == {"raw_text_undecryptable"}


def test_type_reads_rows_sealed_with_a_retired_key(monkeypatch, warnings):
    token = EncryptedText().process_bind_param("secreto", None)
    retired, new = crypto.master_key(), os.urandom(32)
    use_keys(monkeypatch, new, (retired,))
    assert EncryptedText().process_result_value(token, None) == "secreto" and warnings == []
    assert token_key_id(EncryptedText().process_bind_param("otro", None)) == key_id(new)


def test_without_key_raw_text_is_refused_but_null_is_fine(no_master_key):
    column = EncryptedText()
    assert column.process_bind_param(None, None) is None
    assert column.process_result_value("texto heredado", None) == "texto heredado"
    with pytest.raises(KeyNotConfiguredError, match="FIELD_ENCRYPTION_KEY"):
        column.process_bind_param("Pagaste $12.000", None)
    with pytest.raises(KeyNotConfiguredError):
        column.process_result_value("enc:v1:abcd1234:AAAA", None)


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
    assert stored[0].startswith(f"enc:v1:{key_id(crypto.master_key())}:")
    assert "Bancolombia" not in stored[0] and "0550012345678" not in stored[0] and secret not in stored[0]
    assert stored[1] is None
    await db.refresh(row)
    await db.refresh(empty)
    assert row.raw_text == secret and empty.raw_text is None
    assert (await db.execute(select(InboxItem.raw_text).where(InboxItem.id == row.id))).scalar_one() == secret


async def test_orm_read_with_unknown_key_returns_a_marker_and_does_not_poison_listings(db, monkeypatch, warnings):
    row = await add_item(db, raw_text="secreto")
    await add_item(db, raw_text=None)
    use_keys(monkeypatch, os.urandom(32))
    await db.refresh(row)
    assert row.raw_text == UNDECRYPTABLE_TEXT
    listing = (await db.execute(select(InboxItem.raw_text).order_by(InboxItem.id))).scalars().all()
    assert listing == [UNDECRYPTABLE_TEXT, None]
    assert warnings and {event for event, _ in warnings} == {"raw_text_undecryptable"}


async def test_orm_read_with_a_retired_key_returns_plaintext(db, monkeypatch):
    row = await add_item(db, raw_text="secreto")
    use_keys(monkeypatch, os.urandom(32), (crypto.master_key(),))
    await db.refresh(row)
    assert row.raw_text == "secreto"


async def test_purge_runs_even_when_stored_rows_cannot_be_decrypted(db, service, monkeypatch):
    await add_item(db, raw_text="secreto", created_at=now_utc() - timedelta(days=31))
    use_keys(monkeypatch, os.urandom(32))
    await service._purge()
    assert (await db.execute(select(InboxItem.raw_text))).scalars().all() == [None]


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


def test_migration_encrypts_plaintext_that_looks_like_the_old_prefix_and_keeps_enc_v1_rows(legacy_db, monkeypatch):
    engine, table, key, kept = legacy_db
    with engine.begin() as connection:
        connection.execute(table.insert(), [
            {"id": 7, "raw_text": "v1:Pagaste $9.000"}, {"id": 8, "raw_text": "ENC:V1:texto heredado"},
        ])
    monkeypatch.delenv("FIELD_ENCRYPTION_KEY_FILE", raising=False)
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", base64.b64encode(key).decode())
    run_upgrade(engine, load_migration())
    rows = stored_rows(engine, table)
    assert rows[4] == kept
    assert decrypt(rows[7], key) == "v1:Pagaste $9.000" and decrypt(rows[8], key) == "ENC:V1:texto heredado"
    assert all(rows[i].startswith(f"enc:v1:{key_id(key)}:") for i in (1, 3, 5, 6, 7, 8))


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
    # Counterparty after a P2P money verb (a|de + two or more Title/UPPER words); a purchase keeps its merchant.
    ("Nequi: Enviaste $50.000 a Carlos Gomez", "Nequi: Enviaste $50.000 a [NOMBRE]"),
    ("Recibiste $100.000 de MARIA GOMEZ", "Recibiste $100.000 de [NOMBRE]"),
    (
        "Daviplata: Transferiste $20.000 a Juan Pablo Rojas Diaz. Saldo $5.000",
        "Daviplata: Transferiste $20.000 a [NOMBRE]. Saldo $5.000",
    ),
    (
        "Bancolombia: Transferiste $150.000 de la cuenta *1234 a la cuenta de Maria Fernanda Lopez",
        "Bancolombia: Transferiste $150.000 de la cuenta [CUENTA-1234] a la cuenta de [NOMBRE]",
    ),
    (
        "Bancolombia: Recibiste una transferencia de $100.000 de MARIA GOMEZ en tu cuenta *1234",
        "Bancolombia: Recibiste una transferencia de $100.000 de [NOMBRE] en tu cuenta [CUENTA-1234]",
    ),
    (
        "Davivienda: Recibiste un giro de $80.000 de ANA MARIA RUIZ el 21/11/2026",
        "Davivienda: Recibiste un giro de $80.000 de [NOMBRE] el 21/11/2026",
    ),
    ("Nequi: CARLOS GOMEZ te envió $50.000", "Nequi: [NOMBRE] te envió $50.000"),
    ("Compraste en EXITO", "Compraste en EXITO"),
    ("Nequi: Enviaste $300.000 a BANCOLOMBIA", "Nequi: Enviaste $300.000 a BANCOLOMBIA"),
    ("Pagaste $500.000 a TARJETA DE CREDITO VISA", "Pagaste $500.000 a TARJETA DE CREDITO VISA"),
    ("Pagaste $89.900 a ALMACENES EXITO SA", "Pagaste $89.900 a ALMACENES EXITO SA"),
    ("Recibiste $5.000 de tu Bolsillo", "Recibiste $5.000 de tu Bolsillo"),
    ("Enviaste $12.000 a MARIA", "Enviaste $12.000 a MARIA"),
    ("Recibiste $30.000 de JUAN", "Recibiste $30.000 de JUAN"),
    # OTP typed in groups and temporary alphanumeric keys next to their label.
    (
        "Bancolombia: Tu clave dinamica es 123 456. No la compartas.",
        "Bancolombia: Tu clave dinamica es [CODIGO]. No la compartas.",
    ),
    (
        "Davivienda: codigo de seguridad 482-913 vence en 5 minutos",
        "Davivienda: codigo de seguridad [CODIGO] vence en 5 minutos",
    ),
    ("Nequi: tu OTP es 123\u00a0456", "Nequi: tu OTP es [CODIGO]"),
    (
        "Bancolombia: Tu clave temporal es Ab12Cd34. Cambiala al ingresar.",
        "Bancolombia: Tu clave temporal es [CODIGO]. Cambiala al ingresar.",
    ),
    # URLs with tokens or parameters; plain informational URLs stay.
    (
        "Ingresa a https://bancolombia.com.co/login?token=abc123 para validar tu clave.",
        "Ingresa a [URL] para validar tu clave.",
    ),
    ("Paga aqui https://pse.app.co/pay/9f8a7b6c5d4e3f.", "Paga aqui [URL]."),
    ("Confirma tu pago en bit.ly/3xYz9Ab antes de las 5pm", "Confirma tu pago en [URL] antes de las 5pm"),
    (
        "Mas info en https://www.bancolombia.com/personas y www.davivienda.com/seguridad.",
        "Mas info en https://www.bancolombia.com/personas y www.davivienda.com/seguridad.",
    ),
    # Phones separated by dots, spaces or non-breaking spaces.
    ("Llama al 300.123.4567, 310 555 8899 o +57 320 111 2233", "Llama al [TEL], [TEL] o [TEL]"),
    ("Contacto: 601.744.4000 o 300\u00a0123\u00a04567", "Contacto: [TEL] o [TEL]"),
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


def test_p2p_counterparties_of_the_security_review_are_masked():
    out = redact("Nequi: Enviaste $50.000 a Carlos Gomez")
    assert "Carlos" not in out and "Gomez" not in out and "$50.000" in out
    out = redact("Recibiste $100.000 de MARIA GOMEZ")
    assert "MARIA" not in out and "GOMEZ" not in out and "$100.000" in out
    assert redact("Compraste $45.000 en EXITO") == "Compraste $45.000 en EXITO"


@pytest.mark.parametrize("text", [
    "Tu codigo es 123 456", "Tu código es 123-456", "OTP: 1234 5678", "Clave: 12 34 56",
    "Tu clave es 123\u00a0456", "Tu clave temporal es Ab12Cd34", "Temporary password: Xy7kLm29",
    "Your verification code is A7K9P2", "Contraseña: Temp2026x", "Tu contraseña provisional es Prueba#2026",
])
def test_grouped_and_alphanumeric_credentials_are_sensitive(text):
    assert is_sensitive_code(text)


@pytest.mark.parametrize("text", [
    "Tu clave es segura", "Tu código de producto es ABC123456DEF", "Llama al 601 744 4000 si tienes dudas",
])
def test_plain_text_near_credential_words_is_not_sensitive(text):
    assert not is_sensitive_code(text)


# --- Output redaction: item_data is the only exit point --------------------------------------


def item_stub(**values):
    fields = {name: None for name in PUBLIC_ITEM_FIELDS} | {"tags": [], "raw_text": None} | values
    return SimpleNamespace(**fields)


LEAKY = {
    "sender": "alertas@bancolombia.com.co",
    "subject_or_title": "Extracto de cuenta ****1234 para Carlos Gomez",
    "description": "Giro a Maria Lopez",
    "review_reason": "El SMS dice: Enviaste $50.000 a Carlos Gomez",
    "last_error": "Firefly 422: cuenta 0550012345678 no existe",
    "merchant": "EXITO",
}


def test_item_data_redacts_free_text_fields_unless_include_raw():
    data = item_data(item_stub(**LEAKY))
    assert data["sender"] == "[EMAIL]"
    assert data["subject_or_title"] == "Extracto de cuenta [CUENTA-1234] para [NOMBRE]"
    assert data["description"] == "Giro a [NOMBRE]"
    assert data["review_reason"] == "El SMS dice: Enviaste $50.000 a [NOMBRE]"
    assert data["last_error"] == "Firefly 422: cuenta [CUENTA-5678] no existe"
    assert data["merchant"] == "EXITO" and "raw_text" not in data
    full = item_data(item_stub(**LEAKY), include_raw=True)
    assert all(full[name] == value for name, value in LEAKY.items())


def test_item_data_caps_review_reason_and_last_error_at_300_characters():
    long_text = "y" * 1000
    for kwargs in ({}, {"include_raw": True}):
        data = item_data(item_stub(review_reason=long_text, last_error=long_text, description=long_text), **kwargs)
        assert data["review_reason"] == data["last_error"] == "y" * 300
        assert data["description"] == long_text
    # Redaction runs first and the cut never leaves half a placeholder such as "[CUEN".
    data = item_data(item_stub(last_error="x" * 294 + " 0550012345678"))
    assert data["last_error"] == "x" * 294 + " "
    assert item_data(item_stub(last_error=None, review_reason=None))["last_error"] is None


async def test_inbox_read_redacts_free_text_and_review_scope_gets_it_raw(client, db):
    row = InboxItem(
        source="sms", source_ref=str(uuid4()), sender=LEAKY["sender"], subject_or_title=LEAKY["subject_or_title"],
        description=LEAKY["description"], review_reason=LEAKY["review_reason"] + " " + "z" * 400,
        last_error=LEAKY["last_error"] + " " + "y" * 400, raw_text=RAW_SMS,
    )
    db.add(row)
    await db.commit()
    reader = await device_headers(client, ["read"])
    reviewer = await device_headers(client, ["read", "review"])

    body = (await client.get(f"{API}/inbox/{row.id}", headers=reader)).json()
    assert body["sender"] == "[EMAIL]" and body["description"] == "Giro a [NOMBRE]"
    assert body["subject_or_title"] == "Extracto de cuenta [CUENTA-1234] para [NOMBRE]"
    assert body["review_reason"].startswith("El SMS dice: Enviaste $50.000 a [NOMBRE]") and len(body["review_reason"]) == 300
    assert body["last_error"].startswith("Firefly 422: cuenta [CUENTA-5678] no existe") and len(body["last_error"]) == 300
    assert "Carlos" not in str(body) and "0550012345678" not in str(body) and "alertas@" not in str(body)

    full = (await client.get(f"{API}/inbox/{row.id}?include_raw=true", headers=reviewer)).json()
    assert full["sender"] == LEAKY["sender"] and full["description"] == LEAKY["description"]
    assert full["subject_or_title"] == LEAKY["subject_or_title"]
    assert full["review_reason"].startswith(LEAKY["review_reason"]) and len(full["review_reason"]) == 300
    assert full["last_error"].startswith(LEAKY["last_error"]) and len(full["last_error"]) == 300
