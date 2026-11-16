from email import policy
from email.parser import BytesParser

import pytest

from app.clients.imap import MailHeader
from app.security import is_sensitive_code
from app.security.sender_trust import AuthResult, SourceRule, authentication_result, evaluate_email


def authenticate(headers, address="alert@bank.test", ids=("mx.google.com",)):
    raw = "\r\n".join("Authentication-Results: " + h for h in headers) + "\r\n\r\n"
    return authentication_result(BytesParser(policy=policy.default).parsebytes(raw.encode()), address, ids)


def test_highest_allowlisted_header_wins_over_injected_lower_pass():
    result = authenticate([
        "evil.test; dkim=pass header.d=bank.test",
        "mx.google.com; dkim=fail header.d=bank.test; dmarc=fail header.from=bank.test",
        "mx.google.com; dkim=pass header.d=bank.test",
    ])
    assert not result.trusted
    assert result.dkim == "fail"


@pytest.mark.parametrize("result", [
    "dkim=pass header.d=bank.test", "dkim=pass header.d=mail.bank.test",
    "dmarc=pass header.from=bank.test",
])
def test_aligned_authentication(result):
    assert authenticate(["mx.google.com; " + result]).trusted


@pytest.mark.parametrize("result", [
    "spf=pass smtp.mailfrom=bank.test", "dkim=pass header.d=evilbank.test",
    "dkim=pass header.d=bank.test.evil.test", "dmarc=pass header.from=evil.test",
])
def test_unaligned_or_spf_alone_is_untrusted(result):
    assert not authenticate(["mx.google.com; " + result]).trusted


def test_empty_authserv_configuration_is_fail_closed():
    assert not authenticate(["mx.google.com; dkim=pass header.d=bank.test"], ids=[]).trusted


def test_display_name_never_matches_rule():
    header = MailHeader(1, from_address="attacker@evil.test", from_display="Bancolombia bank.test",
                        auth=AuthResult(trusted=True))
    assert not evaluate_email(header, [SourceRule("bank.test")]).trusted


@pytest.mark.parametrize("address,expected", [
    ("alert@bank.test", True), ("alert@news.bank.test", True),
    ("alert@evilbank.test", False), ("alert@bank.test.evil.test", False),
])
def test_domain_boundary_matching(address, expected):
    assert evaluate_email(MailHeader(1, from_address=address, auth=AuthResult(trusted=True)),
                          [SourceRule("bank.test")]).trusted is expected


def test_auth_required_pending_and_generic_rules_blocked():
    header = MailHeader(1, from_address="alert@bank.test")
    assert not evaluate_email(header, [SourceRule("bank.test")]).trusted
    assert evaluate_email(header, [SourceRule("bank.test", require_auth=False)]).trusted
    assert not evaluate_email(header, [SourceRule("bank.test", False, "pending_approval")]).trusted
    gmail = MailHeader(1, from_address="person@gmail.com", auth=AuthResult(trusted=True))
    assert not evaluate_email(gmail, [SourceRule("gmail.com", False)]).trusted


@pytest.mark.parametrize("identity,expected", [
    ("@bancolombia.com.co", True), ("alerts@bancolombia.com.co", True),
    ("@mail.bancolombia.com.co", True), ("@evilbancolombia.com.co", False),
    ("@bancolombia.com.co.evil.test", False),
])
def test_gmail_real_format_dkim_identity(identity, expected):
    result = authenticate([
        f"mx.google.com; dkim=pass header.i={identity} header.s=selector header.b=abcdef",
    ], address="alertas@bancolombia.com.co")
    assert result.trusted is expected


def test_dkim_identity_does_not_override_explicit_signing_domain():
    assert not authenticate([
        "mx.google.com; dkim=pass header.d=evil.test header.i=@bank.test",
    ]).trusted


def test_authentication_failure_and_unknown_sender_have_distinct_reasons():
    header = MailHeader(1, from_address="alert@bank.test")
    rule = SourceRule("bank.test")
    decision = evaluate_email(header, [rule])
    assert decision.reason == "authentication_failed" and decision.rule is rule
    assert evaluate_email(header, [SourceRule("other.test")]).reason == "unknown_sender"


@pytest.mark.parametrize("text,subject", [
    ("Tu código de seguridad es 123456", ""),
    ("OTP: 1234", ""), ("Clave dinámica 12345678", ""),
    ("Tu contraseña temporal es 123456", ""),
    ("Token de autenticación: 987654", ""),
    ("Your one-time password is 123456", ""),
    ("Your verification code is 123456", ""),
    ("Temporary password: 123456", ""),
    ("123456 is your security code", ""),
    ("PIN: 1234", ""), ("Passcode: 123456", ""),
    ("123456", "Código de acceso"),
    ("Nuevo mensaje\nTu clave es\n123456", ""),
])
def test_sensitive_security_codes_are_detected(text, subject):
    assert is_sensitive_code(text, subject)


@pytest.mark.parametrize("text", [
    "Compraste $5000 en TIENDA", "Tu código es 123", "Tu código es 123456789",
    "Código de producto ABC123456DEF", "Recibiste $123456 de JUAN",
    "La contraseña debe tener al menos ocho caracteres",
])
def test_non_codes_do_not_match(text):
    assert not is_sensitive_code(text)
