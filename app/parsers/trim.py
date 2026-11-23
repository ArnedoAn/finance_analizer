"""Cut the fixed advertising and legal text that banks append to every transaction e-mail.

Bancolombia ends all its alerts with «consejos de seguridad», «Por tu seguridad, desde Bancolombia nunca te
solicitaremos… códigos de seguridad…», support lines and the supervision notice. That text says nothing about the
movement, costs tokens, leaks nothing useful and, worse, trips the credential detector. ``trim_template`` runs on the
body before anything reads or stores it (sensitivity check, parser, LLM, ``raw_text``).

It is conservative: text is only cut AFTER the first amount (the transaction sentence is never touched), only at a
sentence boundary when there is one, and the original text is returned whenever the result would lose the amount or
be empty. It is idempotent. Bank e-mails often arrive as one line (HTML cells joined by spaces), so nothing here depends
on line breaks.
"""
import re
import unicodedata

# Folded (lower case, no accents) markers: everything from the sentence that contains one to the end is dropped.
_CUT = tuple(re.compile(pattern) for pattern in (
    r"para que enviar plata siempre sea",
    r"consejos de seguridad",
    r"por tu seguridad",
    r"nunca te solicitaremos",
    r"nunca te pediremos",
    r"tienes (?:alguna )?(?:dudas?|inquietudes?|preguntas?)",
    r"te quedaron dudas",
    r"necesitas ayuda\?",
    r"llamanos",
    r"comunicate con nosotros",
    r"lineas? de atencion",
    r"canales de atencion",
    r"linea (?:amiga|nacional|bancolombia)",
    r"vigilad[oa]\b[^.]{0,60}superintendencia",
    r"superintendencia financiera",
    r"tu equipo bancolombia",
    r"este mensaje fue enviado",
    r"este (?:correo|mensaje) (?:es|fue) (?:generado|enviado|automatic)",
    r"no (?:responda|respondas|respondan) (?:a )?este (?:correo|mensaje)",
    r"bancolombia s\.a\.",
    r"derechos reservados",
    r"sede principal",
    r"direccion de (?:la )?sede",
))
# Short promotional or support sentences that sit inside the kept part.
_REMOVE = tuple(re.compile(pattern) for pattern in (
    r"dudas?\s+(?:al|llama\w*|comunic\w+)\s+(?:al\s+)?[\d\s().+-]{6,}\.?",
    r"con codigo qr es facil[^.!?]*[.!?]?",
    r"\b0?18000\d{6,7}\b",
))
_MONEY = re.compile(r"(?:us\$|usd|cop|\$)\s*\d[\d.,]*")
_BOUNDARY = ".!?\n"


def _fold(text: str) -> str:
    """Lower case without accents, one character per character so offsets match the original text."""
    return "".join((unicodedata.normalize("NFD", char)[:1] or char).lower()[:1] or char for char in text)


def trim_template(text: str | None) -> str | None:
    """``text`` without the bank's advertising and legal footer, or ``text`` itself when nothing safe can be cut."""
    if not text:
        return text
    folded = _fold(text)
    amount = _MONEY.search(folded)
    floor = amount.end() if amount else 0
    cut = len(text)
    for pattern in _CUT:
        for match in pattern.finditer(folded):
            if match.start() < floor:
                continue
            start = match.start()
            boundary = max(folded.rfind(char, floor, start) for char in _BOUNDARY)
            cut = min(cut, boundary + 1 if boundary >= floor else start)
            break
    kept, folded_kept = text[:cut], folded[:cut]
    spans = sorted(match.span() for pattern in _REMOVE for match in pattern.finditer(folded_kept))
    if cut == len(text) and not spans:
        return text  # no bank footer: the text is returned exactly as it came, whitespace included
    parts, copied = [], 0
    for start, end in spans:
        if start < copied:
            continue
        parts.append(kept[copied:start])
        copied = end
    parts.append(kept[copied:])
    result = re.sub(r"[ \t]{2,}", " ", "".join(parts)).strip()
    if not result or (amount and not _MONEY.search(_fold(result))):
        return text
    return result
