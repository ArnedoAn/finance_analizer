"""Single server-side redaction point (contract section 10).

Every text that may leave the server towards the app's assistant/LLM goes through ``redact``.
It is a pure, deterministic function: the same input always yields the same output, and applying it
to its own output changes nothing.

Replaced: account/card numbers (last four kept, ``[CUENTA-1234]``), national IDs and NITs next to an ID
label (``[ID]``), 4-8 digit codes near OTP/password labels (``[CODIGO]``, label detection shared with
``sensitive_codes``), e-mail addresses (``[EMAIL]``), phone numbers (``[TEL]``) and person names after
the markers common in Colombian bank SMS/e-mail (``[NOMBRE]``). Amounts, dates, times and merchant
names are kept.

Known limits (heuristic, favouring merchant preservation): names are only found after the markers
«Sr(a).», «estimado/a», «hola», «titular», «a nombre de» and «para»; after «para» a name needs at
least two Title-case words so that «para Rappi» keeps the merchant. Unlabelled 5-8 digit numbers are
left alone because they cannot be told apart from references.
"""
import re
import unicodedata

from app.security.sensitive_codes import _DIGITS, _LABEL

# --- National IDs (only next to a label: a bare "79.123.456" is indistinguishable from an amount) ---
_ID_LABEL = (
    r"c[eé]dula(?:[ \t]+de[ \t]+ciudadan[ií]a)?|c\.[ \t]?c\.?|cc|n\.?i\.?t\.?|"
    r"documento(?:[ \t]+de[ \t]+identidad)?|identificaci[oó]n"
)
_ID = re.compile(
    r"(?<![\w.])(?P<label>(?i:" + _ID_LABEL + r"))(?![^\W\d_])"
    r"(?P<sep>(?:[ \t]*(?i:n[uú]mero|n[oº°]\.?|nro\.?|num\.?|#))?[ \t:.\-]*)"
    r"(?P<id>\d{1,3}(?:\.\d{3}){1,3}(?:-\d)?|\d{5,12}(?:-\d)?)(?!\w|\.\d)"
)

# --- Codes near OTP/password labels ---
_AMOUNT_BEFORE = re.compile(r"(?:\$|cop|usd)\s*$")
_CODE_WINDOW = 80

# --- Contact data ---
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_NOT_DIGIT_AFTER = r"(?!\d|[.,]\d)"
_PHONES = (
    # Mobile: 3XX XXX XXXX, optionally with +57 / 57.
    re.compile(r"(?<![\w$.,])(?:\+?57[ -]?)?\(?3\d{2}\)?[ -]?\d{3}[ -]?\d{4}" + _NOT_DIGIT_AFTER),
    # Landline with area code: (60X) XXX XXXX.
    re.compile(r"(?<![\w$.,])\(?60[1-8]\)?[ -]?\d{3}[ -]?\d{4}" + _NOT_DIGIT_AFTER),
    # National toll-free: 01 8000 XXXXXX.
    re.compile(r"(?<![\w$.,])01[ -]?8000(?:[ -]?\d{2,4}){1,2}" + _NOT_DIGIT_AFTER),
)

# --- Accounts and cards ---
# Masked forms ("*1234", "**** **** 1234", "4513 **** **** 1234"): only the last four digits remain.
_MASKED = re.compile(
    r"(?<![\w*•])(?:\d{4}[ -])?(?:[*•]|[xX]{2})[*•xX]*(?:[ -][*•xX]+)*[ -]?(\d{4})(?!\d)"
)
_CARD = re.compile(
    r"(?<![\w.,$*])(?:\d{4}(?:[ -]\d{4}){3}|\d{4}[ -]\d{6}[ -]\d{5})" + _NOT_DIGIT_AFTER
)
_DASHED_ACCOUNT = re.compile(r"(?<![\w.,$*])\d{2,4}-\d{4,9}(?:-\d{1,4}){1,2}" + _NOT_DIGIT_AFTER)
_LONG_NUMBER = re.compile(r"(?<![\w.,$*])\d{9,19}(?!\w|[.,]\d)")

# --- Person names ---
_WORD = r"[^\W\d_]+(?:-[^\W\d_]+)*"
_HONORIFIC = r"sr\(a\)\.?|sr(?:a|es)?\.?|se[ñn]or(?:a|es)?(?:\(a\))?"
_MARKER = (
    r"estimad[oa](?:\(a\))?s?|hola|titular(?:\s+de\s+la\s+(?:cuenta|tarjeta))?|a\s+nombre\s+de|para"
)
_NAME = re.compile(
    r"(?<![\w@])(?P<prefix>(?i:(?:" + _HONORIFIC + "|" + _MARKER + r")(?:[ \t]+(?:" + _HONORIFIC + r"))*)"
    r"(?:[ \t]*:[ \t]*|[ \t]+))(?P<name>" + _WORD + r"(?:[ \t]+" + _WORD + r"){0,5})"
)
_AFTER_PARA = re.compile(r"(?i)\bpara[ \t:]*$")
_CONNECTORS = frozenset({"de", "del", "la", "las", "los"})
_NOT_NAMES = _CONNECTORS | frozenset({
    "cliente", "clientes", "usuario", "usuaria", "titular", "senor", "señor", "senora", "señora",
    "sr", "sra", "sres", "tu", "su", "te", "se", "le", "nos", "el", "un", "una", "ud", "usted", "ti",
    "mi", "hemos", "ha", "has", "si", "no", "por", "con", "en", "mas", "más", "mayor", "cualquier",
    "consultas", "informacion", "información", "seguridad", "ver", "pagar", "favor", "gracias",
    "bienvenido", "bienvenida", "bancolombia", "davivienda", "nequi", "daviplata", "banco", "cuenta",
    "tarjeta", "compra", "pago", "transferencia", "retiro", "recibiste", "enviaste", "compraste",
    "pagaste", "realizaste", "retiraste", "transferiste",
})
_MAX_NAME_WORDS = 4


def _last4(match: re.Match) -> str:
    return "[CUENTA-" + re.sub(r"\D", "", match.group(0))[-4:] + "]"


def _redact_ids(text: str) -> str:
    return _ID.sub(lambda m: m.group("label") + m.group("sep") + "[ID]", text)


def _fold(text: str) -> str:
    """Lowercase and strip accents one character at a time so offsets match the original text."""
    return "".join(unicodedata.normalize("NFD", char)[0].lower()[:1] for char in text)


def _is_plain_code(folded: str, match: re.Match) -> bool:
    """Skip digit runs that are really dates, amounts, masked cards or an earlier placeholder."""
    start, end = match.span()
    before = folded[start - 1:start] if start else ""
    after = folded[end:end + 1]
    if after.isalnum() or before in ("/", ".", ","):
        return False
    if after == "/" or (after in (".", ",", "-") and folded[end + 1:end + 2].isdigit()):
        return False
    if before == "-" and folded[start - 2:start - 1].isdigit():
        return False
    lead = folded[max(0, start - 8):start]
    return not (_AMOUNT_BEFORE.search(lead) or lead == "[cuenta-")


def _redact_codes(text: str) -> str:
    folded = _fold(text)
    spans = set()
    for label in _LABEL.finditer(folded):
        low, high = max(0, label.start() - _CODE_WINDOW), min(len(folded), label.end() + _CODE_WINDOW)
        spans.update(m.span() for m in _DIGITS.finditer(folded, low, high) if _is_plain_code(folded, m))
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + "[CODIGO]" + text[end:]
    return text


def _name_end(match: re.Match) -> int | None:
    """Absolute end of the accepted name after a marker, or None when it is not a person name."""
    after_para = _AFTER_PARA.search(match.group("prefix")) is not None
    end = count = 0
    for word in re.finditer(r"[^ \t]+", match.group("name")):
        token = word.group()
        lowered = token.casefold()
        if count and lowered in _CONNECTORS:
            continue
        if not token[0].isupper() or lowered in _NOT_NAMES or (after_para and token.isupper()):
            break
        end, count = word.end(), count + 1
        if count == _MAX_NAME_WORDS:
            break
    if count < (2 if after_para else 1):
        return None
    return match.start("name") + end


def _redact_names(text: str) -> str:
    parts, copied, search_from = [], 0, 0
    while (match := _NAME.search(text, search_from)) is not None:
        end = _name_end(match)
        if end is None:
            search_from = match.end("prefix")
            continue
        parts.append(text[copied:match.start("name")])
        parts.append("[NOMBRE]")
        copied = search_from = end
    parts.append(text[copied:])
    return "".join(parts)


def redact(text: str | None) -> str | None:
    if text is None:
        return None
    text = _redact_ids(text)
    text = _redact_codes(text)
    text = _EMAIL.sub("[EMAIL]", text)
    text = _MASKED.sub(lambda m: "[CUENTA-" + m.group(1) + "]", text)
    text = _CARD.sub(_last4, text)
    text = _DASHED_ACCOUNT.sub(_last4, text)
    for phone in _PHONES:
        text = phone.sub("[TEL]", text)
    text = _LONG_NUMBER.sub(_last4, text)
    return _redact_names(text)
