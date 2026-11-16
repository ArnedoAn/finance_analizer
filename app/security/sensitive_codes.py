"""Conservative local-only screening of secrets before persistence or inference."""
import re
import unicodedata

_LABEL = re.compile(
    r"\b(?:otp|codigo|code|clave|token|contrasena|password|passcode|pin|"
    r"one[\s-]+time[\s-]+(?:password|passcode)|security[\s-]+number)\b"
)
_DIGITS = re.compile(r"(?<!\w)\d{4,8}(?!\w)")


def is_sensitive_code(text: str, subject: str = "") -> bool:
    """Detect a 4–8 digit credential near an English/Spanish security label.

    Scan subject and body together, including labels in the subject and the code
    in the body. False positives intentionally favor not retaining a credential.
    No secret value is returned or logged.
    """
    value = "".join(
        char for char in unicodedata.normalize("NFKD", subject + "\n" + text).casefold()
        if not unicodedata.combining(char)
    )
    for label in _LABEL.finditer(value):
        start = max(0, label.start() - 80)
        end = min(len(value), label.end() + 80)
        if _DIGITS.search(value, start, end):
            return True
    return False
