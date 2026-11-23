"""Conservative local-only screening of secrets before persistence or inference.

Shared with ``redaction``: the label vocabulary and the span finders below are the single definition
of «what looks like a credential», so detection (``is_sensitive_code``) and masking (``redact``)
cannot drift apart.
"""
import re
import unicodedata

_LABEL = re.compile(
    r"\b(?:otp|codigo|code|clave|token|contrasena|password|passcode|pin|"
    r"one[\s-]+time[\s-]+(?:password|passcode)|security[\s-]+number)\b"
)
_DIGITS = re.compile(r"(?<!\w)\d{4,8}(?!\w)")
CODE_WINDOW = 80

# One-time codes typed in groups ("123 456", "123-456", "1234 5678"), also with NBSP / narrow NBSP.
# Not preceded/followed by anything that would make the run a date, an amount or a longer number.
_GROUP_SEP = " \u00a0\u202f-"
_SPACED = re.compile(
    r"(?<![\w$/.,-])(?<!\d[" + _GROUP_SEP + r"])"
    r"\d{2,4}(?:[" + _GROUP_SEP + r"]\d{2,4}){1,3}"
    r"(?![\w/]|[.,]\d|[" + _GROUP_SEP + r"]\d)"
)
_DATE = re.compile(r"\d{1,2}[ -]\d{1,2}[ -]\d{4}")

# Temporary alphanumeric credentials: «clave temporal es Ab12Cd34», «Password: Xy7!kLm2». The label must be
# followed directly by an «es/is/:/=» connector (plus a few qualifier words), so «código de producto
# ABC123456DEF» is not a credential. «código/code» only counts with a security qualifier.
_SECURITY_WORD = (
    r"(?:seguridad|security|verificacion|verification|acceso|access|confirmacion|confirmation|"
    r"autenticacion|temporal|temporary|otp)"
)
_CREDENTIAL_LABEL = (
    r"(?:clave|contrasena|password|passcode|pin|token|otp|"
    r"(?:codigo|code)(?=\s+(?:de\s+)?" + _SECURITY_WORD + r"\b)|" + _SECURITY_WORD + r"\s+(?:codigo|code))"
)
_QUALIFIER = (
    r"(?:de|del|un|una|solo|uso|unico|acceso|access|temporal|temporary|dinamica|dynamic|seguridad|"
    r"security|verificacion|verification|confirmacion|autenticacion|virtual|nueva|new|inicial|"
    r"provisional|one|time)"
)
_TEMP_KEY = re.compile(
    r"\b" + _CREDENTIAL_LABEL + r"(?:\s+" + _QUALIFIER + r"){0,4}"
    r"(?:\s+(?:es|is|sera)\b\s*[:=]?|\s*[:=])\s*"
    r"(?P<key>(?=[a-z0-9!@#$%&*+._-]*\d)(?=[a-z0-9!@#$%&*+._-]*[a-z])[a-z0-9!@#$%&*+._-]{5,31}[a-z0-9])"
    r"(?![a-z0-9])"
)

# Words that make a sentence a WARNING about codes («nunca te solicitaremos… códigos», «no compartas tu código»).
_WARNING = re.compile(
    r"\b(?:nunca|jamas|ninguna\s+(?:persona|entidad)|evit\w+\s+compartir|"
    r"no\s+(?:(?:te|se|lo|la|los|las|nos)\s+)?(?:solicit|pedi|pida|compart|comunic|entreg|revel|digit|suministr|dict|facilit|brind|envi|divulg)\w*)"
)
# A code presented as the owner's: «tu código es 123456», «código de verificación: 123456», «clave 123456»,
# «123456 es tu código».
_PRESENTED = re.compile(
    _LABEL.pattern + r"(?:\s+" + _QUALIFIER + r"){0,4}(?:\s+(?:es|is|sera)\b\s*[:=]?|\s*[:=])?\s*"
    r"(?:\d{4,8}(?!\w)|\d{2,4}(?:[" + _GROUP_SEP + r"]\d{2,4}){1,3}(?!\w))"
    r"|(?<!\w)\d{4,8}(?!\w)\s*(?:es|is)?\s*(?:tu|your|su|el)?\s*" + _LABEL.pattern
)
_SENTENCE_END = ".!?\n"


def credential_labels(value: str):
    """The labels of the folded ``value`` that can introduce a credential.

    «código QR» is not one, and neither is a label inside a sentence that only WARNS about codes unless that sentence
    also presents a code as the owner's. Everything else stays: the bias is to treat a real OTP as sensitive.
    ``redaction`` uses the same function, so detection and masking cannot drift apart.
    """
    for label in _LABEL.finditer(value):
        if re.match(r"\s*qr\b", value[label.end():label.end() + 8]) or value[:label.start()].endswith("qr "):
            continue
        start = max(value.rfind(char, 0, label.start()) for char in _SENTENCE_END) + 1
        stops = [value.find(char, label.end()) for char in _SENTENCE_END]
        end = min((stop for stop in stops if stop != -1), default=len(value))
        sentence = value[start:end]
        if _WARNING.search(sentence) and not _PRESENTED.search(sentence):
            continue
        yield label


def digit_code_spans(value: str, start: int = 0, end: int | None = None) -> list[tuple[int, int]]:
    """Spans of 4–8 digit codes in ``value[start:end]``: contiguous or typed in groups.

    ``value`` must already be case/accent folded. A grouped run of 4–8 digits that is not a date
    absorbs the contiguous pieces inside it, so the returned spans never overlap.
    """
    end = len(value) if end is None else end
    grouped = [
        m.span() for m in _SPACED.finditer(value, start, end)
        if 4 <= sum(char.isdigit() for char in m.group()) <= 8 and not _DATE.fullmatch(m.group())
    ]
    spans = list(grouped)
    for match in _DIGITS.finditer(value, start, end):
        if not any(low <= match.start() and match.end() <= high for low, high in grouped):
            spans.append(match.span())
    return sorted(spans)


def credential_spans(value: str) -> list[tuple[int, int]]:
    """Spans of alphanumeric temporary keys written right after a credential label (folded ``value``)."""
    return [match.span("key") for match in _TEMP_KEY.finditer(value)]


def is_sensitive_code(text: str, subject: str = "") -> bool:
    """Detect a credential near an English/Spanish security label.

    Covers 4–8 digit codes (contiguous or grouped, e.g. «123 456» / «123-456») within 80 characters of
    a label, and alphanumeric temporary keys written right after one («clave temporal es Ab12Cd34»).
    Scan subject and body together, including labels in the subject and the code
    in the body. False positives intentionally favor not retaining a credential.
    No secret value is returned or logged.
    """
    value = "".join(
        char for char in unicodedata.normalize("NFKD", subject + "\n" + text).casefold()
        if not unicodedata.combining(char)
    )
    for label in credential_labels(value):
        start = max(0, label.start() - CODE_WINDOW)
        end = min(len(value), label.end() + CODE_WINDOW)
        if digit_code_spans(value, start, end):
            return True
    return bool(credential_spans(value))
