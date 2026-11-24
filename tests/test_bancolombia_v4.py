"""A real Bancolombia QR / llave e-mail (synthetic data): footer trim, credential false positives, llave template, owner's name."""
from datetime import UTC, datetime
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app.clients.imap import IMAPClient, MailMessage
from app.core.config import MailAccount
from app.parsers import Extraction, parse
from app.parsers.trim import trim_template
from app.security import is_sensitive_code
from app.security.redaction import redact
from app.security.sender_trust import AuthResult
from app.services import descriptions
from app.services import processor as pipeline

BOGOTA = ZoneInfo("America/Bogota")
GENERIC = "Alertas y Notificaciones"
SENDER = "alertasynotificaciones@notificacionesbancolombia.com"
HTML = (Path(__file__).parent / "fixtures" / "bancolombia_qr.html").read_text(encoding="utf-8")
TRANSACTION = "pagaste $22,500.00 por codigo QR desde tu cuenta *0000 a la llave 3001234567 el 22/11/2026 a las 17:50."


def body_of(html: str) -> str:
    message = EmailMessage()
    message.set_content(html, subtype="html")
    account = MailAccount(name="primary", provider="gmail", username="u@gmail.com", password="test")
    return IMAPClient(account)._body(message)


BODY = body_of(HTML)


@pytest.fixture(autouse=True)
def fresh_cache():
    descriptions.invalidate()
    yield
    descriptions.invalidate()


# ---- 1. the footer ---------------------------------------------------------------------------------------


def test_the_fixture_reads_as_one_line_with_the_footer():
    assert "\n" not in BODY and TRANSACTION in BODY
    assert "Por tu seguridad" in BODY and "consejos de seguridad" in BODY and "018000912345" in BODY


def test_trim_keeps_the_transaction_and_drops_advice_support_lines_and_promotion():
    trimmed = trim_template(BODY)
    assert trimmed.endswith(TRANSACTION) and "JUAN PRUEBA PEREZ" in trimmed
    for gone in ("consejos", "Por tu seguridad", "018000912345", "Dudas", "es facil", "Bre-B", "nunca te solicitaremos"):
        assert gone not in trimmed
    assert len(trimmed) < len(BODY) / 2
    assert trim_template(trimmed) == trimmed  # idempotent


@pytest.mark.parametrize("text", [
    "Por tu seguridad te informamos: Compraste $5.000 en TIENDA a las 10:00.",  # before the amount: never cut
    "Por tu seguridad, nunca compartas nada",  # no amount: nothing to protect, nothing safe to cut
    "Compraste $5.000 en TIENDA el 22/11/2026.",
    "Aviso de compra\nValor: $45.000,00\nComercio: TIENDA X",
    "", "   ",
])
def test_trim_leaves_everything_else_alone(text):
    assert trim_template(text) == text


def test_trim_handles_multiline_mail_and_a_code_that_precedes_the_footer():
    mail = "Compraste $45.000 en EXITO.\nVigilado Superintendencia Financiera de Colombia.\nTu equipo Bancolombia"
    assert trim_template(mail) == "Compraste $45.000 en EXITO."
    otp = "Tu clave dinamica es 123456. Por tu seguridad no la compartas."
    assert trim_template(otp) == "Tu clave dinamica es 123456." and is_sensitive_code(trim_template(otp))
    assert trim_template(None) is None


def test_trim_removes_the_support_line_even_without_a_footer_marker():
    assert trim_template("Compraste $9.000 en TIENDA. Dudas al 018000912345.") == "Compraste $9.000 en TIENDA."


# ---- 2. credentials ---------------------------------------------------------------------------------------


def test_the_real_mail_is_not_a_credential():
    assert not is_sensitive_code(BODY, GENERIC)
    assert not is_sensitive_code(trim_template(BODY), GENERIC)
    assert "[CODIGO]" not in redact(trim_template(BODY)) and "[CODIGO]" not in redact(BODY)


@pytest.mark.parametrize("text", [
    "Por tu seguridad, desde Bancolombia nunca te solicitaremos tus datos personales, como usuario y clave; ni tus datos bancarios, como números de tarjetas de crédito, códigos de seguridad y plazos. Llama al 018000912345 o escribe 3001234567.",
    "Nunca compartas tu código de seguridad con nadie. Tu cuenta *0000 está protegida.",
    "No compartas tu clave ni tu código con terceros. Cuenta *0000.",
    "Pagaste $22,500.00 por codigo QR desde tu cuenta *0000",
    "Con codigo QR es facil y de una. Cuenta *0000",
    "Escanea el QR code desde tu cuenta *0000",
])
def test_warnings_and_qr_codes_are_not_credentials(text):
    assert not is_sensitive_code(text)


@pytest.mark.parametrize("text", [
    "Tu clave dinamica es 123456", "Código de verificación: 482913", "Bancolombia: Tu clave dinamica es 123456. Nunca la compartas.",
    "No compartas tu codigo 123456 con nadie", "123456 es tu codigo. No lo compartas con nadie.",
    "Nunca compartas este codigo. Tu codigo es 123456", "Tu código es 123 456. No lo compartas.",
    "Tu clave temporal es Ab12Cd34. Nunca la compartas",
])
def test_real_one_time_codes_stay_sensitive(text):
    assert is_sensitive_code(text)
    assert "[CODIGO]" in redact(text)


# ---- 3. the llave / QR template -----------------------------------------------------------------------------


def test_the_qr_payment_is_a_withdrawal_to_a_llave():
    result = parse(trim_template(BODY), GENERIC, "87400")
    assert result.kind == "withdrawal" and result.amount == Decimal("22500.00") and result.currency == "COP"
    assert result.template_id == "bancolombia.pagaste" and result.account_hint == "0000"
    assert result.merchant == "LLAVE ···4567" and result.counterparty_hint == "la llave ···4567"
    assert result.occurred_at == datetime(2026, 11, 22, 17, 50, tzinfo=BOGOTA)
    assert "3001234567" not in repr(result)  # the whole key never travels


@pytest.mark.parametrize(("text", "kind", "merchant"), [
    ("Bancolombia: Transferiste $50.000 a la llave 3109876543 el 22/11/2026 a las 10:00.", "withdrawal", "LLAVE ···6543"),
    ("Bancolombia: Enviaste $50.000,00 a la llave 3109876543 el 22/11/2026 a las 10:00.", "withdrawal", "LLAVE ···6543"),
    ("Bancolombia: Recibiste $80.000 de la llave 3109876543 en tu cuenta *0000 el 22/11/2026 a las 10:00.", "deposit", "LLAVE ···6543"),
    ("Bancolombia: Pagaste $10.000 por codigo QR desde tu cuenta *0000 el 22/11/2026 a las 10:00.", "withdrawal", "PAGO QR"),
    ("Bancolombia: Pagaste $10.000 por codigo QR desde tu cuenta *0000 a las 10:00 del 22/11/2026.", "withdrawal", "PAGO QR"),
])
def test_llave_and_qr_variants(text, kind, merchant):
    result = parse(text, GENERIC, "87400")
    assert result is not None and result.kind == kind and result.merchant == merchant


def item(**changes):
    values = dict(
        merchant="LLAVE ···4567", transaction_type="withdrawal", tags=[], destination_account=None, asset_account="Bancolombia",
        description=None, subject_or_title=GENERIC, sender=SENDER,
    )
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(("changes", "template", "text", "kind", "expected"), [
    ({}, "pagaste", "Pagaste $22,500.00 por codigo QR desde tu cuenta *0000 a la llave 3001234567", "withdrawal", "Pago QR a llave ···4567"),
    ({"merchant": "LLAVE ···6543"}, "transferiste", "Transferiste $50.000 a la llave 3109876543", "withdrawal", "Transferencia a llave ···6543"),
    ({"merchant": "LLAVE ···6543", "transaction_type": "deposit"}, "recibiste", "Recibiste $80.000 de la llave 3109876543", "deposit", "Recibiste de llave ···6543"),
    ({"merchant": "PAGO QR"}, "pagaste", "Pagaste $10.000 por codigo QR desde tu cuenta *0000", "withdrawal", "Pago QR"),
])
def test_description_of_a_llave_payment(changes, template, text, kind, expected):
    extraction = Extraction(kind=kind, amount=Decimal("1.00"), template_id=f"bancolombia.{template}")
    assert descriptions.derive(item(**changes), extraction, text) == expected


# ---- 4. the owner's name ---------------------------------------------------------------------------------


def test_the_owners_name_is_masked_before_the_llm():
    sent = redact(trim_template(BODY))
    assert "JUAN" not in sent and "PEREZ" not in sent and "PRUEBA" not in sent
    assert "Bancolombia: [TITULAR] pagaste $22,500.00" in sent
    assert redact("Bancolombia: JUAN PEREZ compraste $45.000 en EXITO") == "Bancolombia: [TITULAR] compraste $45.000 en EXITO"
    assert redact("Bancolombia: Compraste $45.000 en EXITO") == "Bancolombia: Compraste $45.000 en EXITO"
    assert redact("Bancolombia: EXITO CALLE pagaste") == "Bancolombia: [TITULAR] pagaste"  # two or more capitalised words
    assert redact("Bancolombia: EXITO pagaste") == "Bancolombia: EXITO pagaste"


# ---- 5. end to end ------------------------------------------------------------------------------------------


@pytest.fixture
async def mailbox(service, clients, add_own_account, monkeypatch):
    monkeypatch.setattr(pipeline, "parse", parse)
    monkeypatch.setattr(pipeline, "_now", lambda: datetime(2026, 11, 23, 15, 0, tzinfo=UTC))
    await add_own_account("Bancolombia", bank="bancolombia", kind="credit", last4=["0000"], sources=["email:notificacionesbancolombia.com"])
    service.settings.mail_accounts = [MailAccount(name="primary", provider="gmail", username="u@gmail.com", password="test")]
    message = MailMessage(
        uid=1, message_id="qr-message@notificacionesbancolombia.com", from_address=SENDER, subject=GENERIC,
        occurred_at=datetime(2026, 11, 22, 22, 50, tzinfo=UTC), body=BODY,
        auth=AuthResult(dkim="pass", domain="notificacionesbancolombia.com", trusted=True),
    )
    imap = AsyncMock()
    imap.poll.return_value = (7, 1, [message])
    monkeypatch.setattr(pipeline, "IMAPClient", lambda account: imap)
    return service


async def test_the_real_mail_goes_from_the_poll_to_firefly_without_the_llm(mailbox, clients):
    result = await mailbox.poll_mail()
    assert result["queued"] == 1 and result["ignored"] == 0
    row = await mailbox.inbox.by_ref("mail:primary:7:1")
    assert row.status == "pending" and row.review_reason is None and row.template_id is None
    assert TRANSACTION in row.raw_text
    for gone in ("consejos de seguridad", "Por tu seguridad", "018000912345", "Dudas", "es facil"):
        assert gone not in row.raw_text
    done = await mailbox.process(row)
    assert done.status == "created", done.review_reason
    assert done.method == "parser" and done.merchant == "LLAVE ···4567" and done.asset_account == "Bancolombia"
    assert done.amount == Decimal("22500.00") and done.description == "Pago QR a llave ···4567"
    assert done.occurred_at == datetime(2026, 11, 22, 22, 50, tzinfo=UTC)
    clients[1].analyze.assert_not_awaited()
    clients[0].create_account.assert_awaited_once()
    assert clients[0].create_account.await_args.args == ("LLAVE ···4567", "expense")
    split = clients[0].create_transaction.call_args.args[0]["transactions"][0]
    assert split["description"] == "Pago QR a llave ···4567" and split["amount"] == "22500.00"


async def test_the_llm_never_sees_the_footer_the_subject_or_the_owners_name(mailbox, clients, extraction, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(pipeline, "parse", lambda *args, **kwargs: None)
    clients[1].analyze.return_value = replace(extraction, template_id="llm:test", merchant="LLAVE", summary="Pago QR a llave 4567 por $22.500")
    await mailbox.poll_mail()
    await mailbox.process(await mailbox.inbox.by_ref("mail:primary:7:1"))
    sent = clients[1].analyze.call_args.args[0]
    assert "[TITULAR]" in sent and "JUAN" not in sent and "PEREZ" not in sent
    assert "Alertas" not in sent and "seguridad" not in sent and "018000" not in sent


async def test_a_mail_stored_before_the_trim_is_trimmed_when_it_is_extracted(mailbox):
    row = await mailbox.inbox.add(
        source="email", source_ref="old-mail", sender=SENDER, subject_or_title=GENERIC, raw_text=BODY,
        occurred_at=datetime(2026, 11, 22, 22, 50, tzinfo=UTC), status="pending",
    )
    await mailbox.session.commit()
    done = await mailbox.process(row)
    assert done.status == "created" and done.description == "Pago QR a llave ···4567"


async def test_a_repoll_rescues_a_mail_wrongly_ignored_as_a_sensitive_code(mailbox):
    # Before the footer trim, every Bancolombia mail was ignored as a one-time code and lost its text.
    row = await mailbox.inbox.add(
        source="email", source_ref="mail:primary:7:1", sender=SENDER, subject_or_title="", raw_text=None,
        occurred_at=datetime(2026, 11, 22, 22, 50, tzinfo=UTC), status="ignored", method="sensitive_code",
        template_id="ignore.sensitive_code", review_reason="sensitive_code",
    )
    await mailbox.session.commit()
    result = await mailbox.poll_mail(since=datetime(2026, 11, 1).date(), account_name="primary")
    assert result["requeued"] == 1 and result["duplicates"] == 0
    await mailbox.session.refresh(row)
    assert (row.status, row.template_id, row.subject_or_title) == ("pending", None, GENERIC)
    done = await mailbox.process(row)
    assert done.status == "created" and done.description == "Pago QR a llave ···4567"
