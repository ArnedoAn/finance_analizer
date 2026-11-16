"""Fail-closed email authentication and approved domain rules."""
import re
from dataclasses import dataclass
from email.utils import parseaddr

GENERIC_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "yahoo.com", "yahoo.es", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com",
})
PROVIDER_AUTHSERV_IDS = {
    "gmail": ("mx.google.com",), "yahoo": ("atlas.mail.yahoo.com",),
    "icloud": ("bimi.icloud.com", "mx.icloud.com"),
}


def normalize_domain(value: str) -> str | None:
    value = value.strip().lower().rstrip(".")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if len(value) > 253 or "." not in value:
        return None
    if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in value.split(".")):
        return None
    return value


def address_domain(address: str) -> str | None:
    parsed = parseaddr(address)[1]
    return normalize_domain(parsed.rsplit("@", 1)[1]) if "@" in parsed else None


def domain_matches(domain: str, parent: str) -> bool:
    return domain == parent or domain.endswith("." + parent)


@dataclass(frozen=True)
class AuthResult:
    dkim: str = "none"
    spf: str = "none"
    dmarc: str = "none"
    domain: str | None = None
    trusted: bool = False


@dataclass(frozen=True)
class SourceRule:
    domain: str
    require_auth: bool = True
    status: str = "active"


@dataclass(frozen=True)
class TrustDecision:
    trusted: bool
    rule: SourceRule | None
    reason: str


def authentication_result(message, from_address: str, authserv_ids) -> AuthResult:
    """Use first (highest) allowlisted Authentication-Results, never ARC or lower results.

    Deployment must ensure the receiving provider strips forged headers claiming its
    own authserv-id. IMAP cannot independently verify a provider's header provenance.
    """
    allowed = {value.strip().lower() for value in authserv_ids}
    source = address_domain(from_address)
    for header in message.get_all("Authentication-Results", []):
        parts = str(header).split(";")
        authserv = parts[0].strip().split()
        if not authserv or authserv[0].lower() not in allowed:
            continue
        states = {"dkim": "none", "spf": "none", "dmarc": "none"}
        trusted_domain = None
        for part in parts[1:]:
            match = re.match(r"\s*(dkim|spf|dmarc)\s*=\s*([a-z]+)\b", part, re.I)
            if not match:
                continue
            method, result = (s.lower() for s in match.groups())
            state = "pass" if result == "pass" else "none" if result == "none" else "fail"
            if states[method] != "pass":
                states[method] = state
            prop = "header.d" if method == "dkim" else "header.from"
            domain_match = re.search(r"\b" + re.escape(prop) + r'\s*=\s*"?([a-z0-9.-]+)', part, re.I)
            # Gmail commonly reports the DKIM AUID (header.i), not header.d.
            # Prefer an explicit signing domain; never override a misaligned d.
            if domain_match is None and method == "dkim":
                domain_match = re.search(r'\bheader\.i\s*=\s*"?[^\s;"@]*@([a-z0-9.-]+)', part, re.I)
            domain = normalize_domain(domain_match.group(1)) if domain_match else None
            if state == "pass" and method in {"dkim", "dmarc"} and source and domain:
                if domain_matches(domain, source) or domain_matches(source, domain):
                    trusted_domain = domain
        return AuthResult(**states, domain=trusted_domain, trusted=trusted_domain is not None)
    return AuthResult()


def evaluate_email(header, rules) -> TrustDecision:
    domain = address_domain(header.from_address)
    if not domain:
        return TrustDecision(False, None, "invalid_from_address")
    for rule in rules:
        if getattr(rule, "status", "active") != "active":
            continue
        raw = getattr(rule, "domain", None) or getattr(rule, "keyword", "")
        raw = raw.removeprefix("email:")
        candidate = normalize_domain(raw)
        if not candidate or candidate in GENERIC_DOMAINS or not domain_matches(domain, candidate):
            continue
        if getattr(rule, "require_auth", True) and not header.auth.trusted:
            return TrustDecision(False, rule, "authentication_failed")
        return TrustDecision(True, rule, "approved_domain")
    return TrustDecision(False, None, "unknown_sender")
