"""Single server-side redaction point (contract section 10).

Every text that may leave the server towards the app's assistant/LLM goes through ``redact``.
It is a pure, deterministic function: the same input always yields the same output, and applying it
to its own output changes nothing.

Replaced: account/card numbers (last four kept, ``[CUENTA-1234]``), national IDs and NITs next to an ID
label (``[ID]``), codes near OTP/password labels (``[CODIGO]``: 4-8 digits, also typed in groups like
«123 456» or «123-456», and alphanumeric temporary keys written right after the label; detection is
shared with ``sensitive_codes``), URLs carrying parameters, fragments, credentials or token-like path
segments (``[URL]``), e-mail addresses (``[EMAIL]``), phone numbers (``[TEL]``, separated by spaces,
NBSP, dots or hyphens) and person names (``[NOMBRE]``). Amounts, dates, times and merchant names are kept.

Person names are found in two ways:

* After the markers common in Colombian bank SMS/e-mail: «Sr(a).», «estimado/a», «hola», «titular»,
  «a nombre de» and «para» (after «para» a name needs at least two Title-case words so that
  «para Rappi» keeps the merchant).
* Next to a P2P money verb, which tells a person from a merchant: «enviaste|recibiste|transferiste|
  pagaste|giro|transferencia … a|de <Name>» and «<Name> te envió|transfirió|giró|pagó». The name needs
  two or more words, each in Title case or ALL CAPS. Purchases («Compraste $5.000 en EXITO») have no such
  verb, so their merchant stays.

Known limits (heuristic, favouring merchant preservation): a single-word counterparty («Enviaste $12.000 a
MARIA») is kept because it cannot be told from a merchant; with «pagaste … a» two or more capitalised words
are treated as a person, so a two-word company paid that way («Pagaste $15.000 a CLARO HOGAR») is masked
unless it carries a legal suffix (SA, SAS, LTDA…) or a bank/product word (Bancolombia, Tarjeta, Bolsillo…).
Unlabelled 5-8 digit numbers are left alone because they cannot be told apart from references.
"""
import re
import unicodedata

from app.security.sensitive_codes import _LABEL, CODE_WINDOW, credential_spans, digit_code_spans

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

# --- URLs: only those that can carry a session, a token or a personal identifier ---
_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"'\[\]]+")
_SHORT_URL = re.compile(
    r"(?i)(?<![\w@.])(?:https?://)?(?:www\.)?"
    r"(?:bit\.ly|tinyurl\.com|t\.co|goo\.gl|ow\.ly|cutt\.ly|is\.gd|rb\.gy)/[^\s<>\"'\[\]]+"
)
_URL_TRAILING = ".,;:!?)]}"
_TOKEN_SEGMENT = re.compile(r"(?=[\w~%+-]*\d)(?=[\w~%+-]*[^\W\d_])[\w~%+-]{6,}|[\w~%+-]{24,}")

# --- Contact data ---
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_NOT_DIGIT_AFTER = r"(?!\d|[.,]\d)"
_PHONE_SEP = r"[ \u00a0\u202f.-]"
_PHONES = (
    # Mobile: 3XX XXX XXXX, optionally with +57 / 57.
    re.compile(
        r"(?<![\w$.,])(?:\+?57" + _PHONE_SEP + r"?)?\(?3\d{2}\)?" + _PHONE_SEP + r"?\d{3}" + _PHONE_SEP + r"?\d{4}"
        + _NOT_DIGIT_AFTER
    ),
    # Landline with area code: (60X) XXX XXXX.
    re.compile(
        r"(?<![\w$.,])\(?60[1-8]\)?" + _PHONE_SEP + r"?\d{3}" + _PHONE_SEP + r"?\d{4}" + _NOT_DIGIT_AFTER
    ),
    # National toll-free: 01 8000 XXXXXX.
    re.compile(
        r"(?<![\w$.,])01" + _PHONE_SEP + r"?8000(?:" + _PHONE_SEP + r"?\d{2,4}){1,2}" + _NOT_DIGIT_AFTER
    ),
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
    # Products, networks and labels that follow a counterparty in bank messages.
    "ahorros", "corriente", "credito", "crédito", "debito", "débito", "bolsillo", "bolsillos", "meta",
    "colchon", "colchón", "visa", "mastercard", "amex", "pse", "nu", "lulo", "movii", "dale",
    "ref", "referencia", "saldo", "fecha", "hora", "valor", "disponible", "total", "costo", "comision",
    "comisión", "iva", "hola",
})
_COMPANY = frozenset({"sa", "sas", "ltda", "cia", "esp", "inc", "llc", "corp", "sucursal"})
_MAX_NAME_WORDS = 4

# P2P money verbs: the verb, not the capitalisation, tells a person from a merchant ("Compraste" is not here).
_P2P = re.compile(
    r"(?<!\w)(?i:enviaste|recibiste|transferiste|pagaste|giraste|giro|transferencia"
    r"|(?:te|le)[ \t]+(?:enviaron|transfirieron|giraron|pagaron)"
    r"|has[ \t]+(?:enviado|recibido|transferido))(?![^\W\d_])"
)
_P2P_WINDOW = 100
_P2P_STOP = re.compile(r"(?<!\d)[.!?](?:\s|$)")
_P2P_CONNECTOR = re.compile(r"[ \t](?i:a|de)[ \t]+")
_NAME_RUN = re.compile(_WORD + r"(?:[ \t]+" + _WORD + r"){0,5}")
_P2P_BEFORE = re.compile(r"(?i:\bte[ \t]+(?:envi[oó]|transfiri[oó]|gir[oó]|pag[oó]))(?![^\W\d_])")
_PURE_WORD = re.compile(_WORD)
_TOKEN = re.compile(r"[^ \t]+")
_TOKEN_NO_NEWLINE = re.compile(r"[^ \t\n]+")


def _last4(match: re.Match) -> str:
    return "[CUENTA-" + re.sub(r"\D", "", match.group(0))[-4:] + "]"


def _redact_ids(text: str) -> str:
    return _ID.sub(lambda m: m.group("label") + m.group("sep") + "[ID]", text)


def _url_has_secret(url: str) -> bool:
    rest = url.split("://", 1)[-1]
    if any(char in rest for char in "?#=@;"):
        return True
    return any(_TOKEN_SEGMENT.fullmatch(segment) for segment in rest.partition("/")[2].split("/") if segment)


def _mask_url(url: str, *, always: bool) -> str:
    core = url.rstrip(_URL_TRAILING)
    return "[URL]" + url[len(core):] if always or _url_has_secret(core) else url


def _redact_urls(text: str) -> str:
    text = _SHORT_URL.sub(lambda m: _mask_url(m.group(), always=True), text)
    return _URL.sub(lambda m: _mask_url(m.group(), always=False), text)


def _fold(text: str) -> str:
    """Lowercase and strip accents one character at a time so offsets match the original text."""
    return "".join(unicodedata.normalize("NFD", char)[0].lower()[:1] for char in text)


def _is_plain_code(folded: str, span: tuple[int, int]) -> bool:
    """Skip digit runs that are really dates, amounts, masked cards or an earlier placeholder."""
    start, end = span
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


def _replace_spans(text: str, spans, placeholder: str) -> str:
    """Replace each span (sorted by start; overlapping ones after the first are skipped) with ``placeholder``."""
    parts, copied = [], 0
    for start, end in sorted(set(spans)):
        if start < copied:
            continue
        parts.append(text[copied:start])
        parts.append(placeholder)
        copied = end
    parts.append(text[copied:])
    return "".join(parts)


def _redact_codes(text: str) -> str:
    folded = _fold(text)
    spans = []
    for label in _LABEL.finditer(folded):
        low, high = max(0, label.start() - CODE_WINDOW), min(len(folded), label.end() + CODE_WINDOW)
        spans.extend(span for span in digit_code_spans(folded, low, high) if _is_plain_code(folded, span))
    spans.extend(credential_spans(folded))
    return _replace_spans(text, spans, "[CODIGO]")


def _leading_name_end(run: str, *, min_words: int, allow_upper: bool, reject_company: bool = False) -> int | None:
    """End offset, inside ``run``, of the person name that opens it; None when it is not a person name."""
    end = count = 0
    for word in _TOKEN.finditer(run):
        token = word.group()
        lowered = token.casefold()
        if count and lowered in _CONNECTORS:
            continue
        if reject_company and (lowered in _COMPANY or (len(token) == 1 and token.isupper())):
            return None
        if not token[0].isupper() or lowered in _NOT_NAMES or (token.isupper() and not allow_upper):
            break
        end, count = word.end(), count + 1
        if count == _MAX_NAME_WORDS:
            break
    return end if count >= min_words else None


def _name_end(match: re.Match) -> int | None:
    """Absolute end of the accepted name after a marker, or None when it is not a person name."""
    after_para = _AFTER_PARA.search(match.group("prefix")) is not None
    end = _leading_name_end(match.group("name"), min_words=2 if after_para else 1, allow_upper=not after_para)
    return None if end is None else match.start("name") + end


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


def _p2p_name_after(text: str, trigger: re.Match) -> tuple[int, int] | None:
    """Span of the counterparty in «<verb> … a|de <Name>»: first connector in the sentence followed by a name."""
    limit = min(len(text), trigger.end() + _P2P_WINDOW)
    if (stop := _P2P_STOP.search(text, trigger.end(), limit)) is not None:
        limit = stop.start()
    if (newline := text.find("\n", trigger.end(), limit)) != -1:
        limit = newline
    for connector in _P2P_CONNECTOR.finditer(text, trigger.end(), limit):
        run = _NAME_RUN.match(text, connector.end())
        if run is None:
            continue
        end = _leading_name_end(run.group(), min_words=2, allow_upper=True, reject_company=True)
        if end is not None:
            return connector.end(), connector.end() + end
    return None


def _p2p_name_before(text: str, trigger: re.Match) -> tuple[int, int] | None:
    """Span of the counterparty in «<Name> te envió|transfirió|giró|pagó», read backwards on the same line."""
    line_start = max(text.rfind("\n", 0, trigger.start()) + 1, trigger.start() - 80)
    tokens = list(_TOKEN_NO_NEWLINE.finditer(text, line_start, trigger.start()))
    words = 0
    first = last = None
    for token in reversed(tokens):
        value = token.group()
        lowered = value.casefold()
        if _PURE_WORD.fullmatch(value) is None:
            break
        if words and lowered in _CONNECTORS:
            continue
        if not value[0].isupper() or lowered in _NOT_NAMES or lowered in _COMPANY:
            break
        first = token.start()
        last = token.end() if last is None else last
        words += 1
        if words == _MAX_NAME_WORDS:
            break
    return (first, last) if words >= 2 else None


def _redact_p2p(text: str) -> str:
    spans = []
    for trigger in _P2P.finditer(text):
        if (span := _p2p_name_after(text, trigger)) is not None:
            spans.append(span)
    for trigger in _P2P_BEFORE.finditer(text):
        if (span := _p2p_name_before(text, trigger)) is not None:
            spans.append(span)
    return _replace_spans(text, spans, "[NOMBRE]")


def redact(text: str | None) -> str | None:
    if text is None:
        return None
    text = _redact_urls(text)
    text = _redact_ids(text)
    text = _redact_codes(text)
    text = _EMAIL.sub("[EMAIL]", text)
    text = _MASKED.sub(lambda m: "[CUENTA-" + m.group(1) + "]", text)
    text = _CARD.sub(_last4, text)
    text = _DASHED_ACCOUNT.sub(_last4, text)
    for phone in _PHONES:
        text = phone.sub("[TEL]", text)
    text = _LONG_NUMBER.sub(_last4, text)
    return _redact_p2p(_redact_names(text))
