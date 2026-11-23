"""Safe opening of an uploaded PDF and extraction of its text layer (no OCR).

Library: ``pypdf``. It is pure Python (no native code to attack), actively maintained, decrypts RC4 and AES
(AES through ``cryptography``, already a dependency) and exposes the object tree, which is what lets us prove the
file carries no script or attachment before reading a single page. ``pdfplumber`` would add ``pdfminer.six`` and
``pypdfium2`` (a native renderer) for layout analysis this feature does not need.

Nothing embedded in the PDF is ever executed: the only operations are parsing, decrypting and text extraction.
Inactivity is checked twice, because an object stream hides names from a raw scan and a crafted name can hide
from a structural one: a raw scan of the bytes (names with ``#xx`` escapes decoded) for the unmistakable
markers, then a walk over the catalog, the pages and their annotations once the file is decrypted.

The password lives only as the argument of ``extract``; this module never stores or logs it.
"""
import io
import re

from pypdf import PasswordType, PdfReader
from pypdf.generic import ArrayObject, DictionaryObject

from app.core.logging import get_logger

logger = get_logger(__name__)

MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_PAGES = 60
PAGE_TEXT_CHARS = 60_000
HEADER = b"%PDF-"

# Names that make a file active content. ``/JS`` is left to the structural walk: three bytes appear by chance in compressed streams.
_RAW_ACTIVE = re.compile(rb"/(?:JavaScript|Launch|EmbeddedFile|RichMedia)")
_NAME_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
_ACTIVE_ACTIONS = frozenset({"/JavaScript", "/Launch", "/SubmitForm", "/ImportData", "/GoToE", "/Rendition", "/RichMediaExecute"})
_MAX_DEPTH = 32


class PdfRejectedError(Exception):
    """The upload is not an acceptable PDF. ``code`` is the API error code; the message is safe to show."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


class PdfNeedsPasswordError(Exception):
    """Encrypted and the password is missing or wrong."""


def check_header(data: bytes) -> None:
    if not data.startswith(HEADER):
        raise PdfRejectedError("not_a_pdf", "El archivo no es un PDF.")


def _reject_unsafe() -> PdfRejectedError:
    return PdfRejectedError("pdf_unsafe", "El PDF trae JavaScript o archivos adjuntos; por seguridad no se procesa.")


def scan_raw(data: bytes) -> None:
    """Reject unmistakable active-content markers anywhere in the bytes."""
    if _RAW_ACTIVE.search(_NAME_ESCAPE.sub(lambda match: bytes([int(match.group(1), 16)]), data)):
        raise _reject_unsafe()


def _resolve(value):
    return value.get_object() if hasattr(value, "get_object") else value


def _is_active_action(action, depth: int = 0) -> bool:
    """True when an action, or any action chained after it (``/Next``), runs code or reaches outside the document."""
    action = _resolve(action)
    if depth > _MAX_DEPTH:
        return True
    if isinstance(action, ArrayObject):  # /Next may be an array of actions; an array as /OpenAction is just a destination
        return any(isinstance(_resolve(item), DictionaryObject) and _is_active_action(item, depth + 1) for item in action)
    if not isinstance(action, DictionaryObject):
        return False
    return (
        action.get("/S") in _ACTIVE_ACTIONS or "/JS" in action
        or ("/Next" in action and _is_active_action(action["/Next"], depth + 1))
    )


def _has_active_actions(container) -> bool:
    """An object's ``/A`` action or any action in its ``/AA`` additional-actions dictionary is active."""
    if "/A" in container and _is_active_action(container["/A"]):
        return True
    extra = _resolve(container.get("/AA"))
    return isinstance(extra, DictionaryObject) and any(_is_active_action(action) for action in extra.values())


def _field_is_active(field, depth: int = 0) -> bool:
    field = _resolve(field)
    if not isinstance(field, DictionaryObject):
        return False
    if depth > _MAX_DEPTH or _has_active_actions(field):
        return True
    kids = _resolve(field.get("/Kids"))
    return isinstance(kids, ArrayObject) and any(_field_is_active(kid, depth + 1) for kid in kids)


def _assert_inert(reader: PdfReader) -> None:
    """Walk the decrypted catalog, form fields, pages and annotations; reject scripts, launches and attachments."""
    root = _resolve(reader.trailer["/Root"])
    if ("/OpenAction" in root and _is_active_action(root["/OpenAction"])) or _has_active_actions(root):
        raise _reject_unsafe()
    names = _resolve(root.get("/Names"))
    if isinstance(names, DictionaryObject) and ("/JavaScript" in names or "/EmbeddedFiles" in names):
        raise _reject_unsafe()
    form = _resolve(root.get("/AcroForm"))
    if isinstance(form, DictionaryObject):
        if "/XFA" in form:
            raise _reject_unsafe()
        fields = _resolve(form.get("/Fields"))
        if isinstance(fields, ArrayObject) and any(_field_is_active(field) for field in fields):
            raise _reject_unsafe()
    for page in reader.pages:
        if _has_active_actions(page):
            raise _reject_unsafe()
        annotations = _resolve(page.get("/Annots"))
        for annotation in annotations if isinstance(annotations, ArrayObject) else ():
            annotation = _resolve(annotation)
            if isinstance(annotation, DictionaryObject) and (
                annotation.get("/Subtype") == "/FileAttachment" or "/FS" in annotation or _has_active_actions(annotation)
            ):
                raise _reject_unsafe()


def _open(data: bytes, password: str | None) -> PdfReader:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # An empty user password is common (owner-password-only protection); try it before asking for one.
            for candidate in (password, "") if password else ("",):
                if reader.decrypt(candidate) != PasswordType.NOT_DECRYPTED:
                    break
            else:
                raise PdfNeedsPasswordError
        # Touching the page list forces the cross-reference table to parse, so a corrupt file fails here and not later.
        if len(reader.pages) > MAX_PAGES:
            raise PdfRejectedError("pdf_too_long", f"El PDF tiene más de {MAX_PAGES} páginas.")
        return reader
    except (PdfNeedsPasswordError, PdfRejectedError):
        raise
    except Exception as exc:  # a parser fed with untrusted bytes: any failure means "not a readable PDF"
        logger.warning("statement_pdf_unreadable", error_type=type(exc).__name__)
        raise PdfRejectedError("pdf_corrupt", "No se pudo leer el PDF; está dañado o tiene un formato no soportado.") from None


def extract(data: bytes, password: str | None = None) -> list[str]:
    """Text of every page (empty pages dropped) of a validated PDF.

    Raises ``PdfRejectedError`` (not a PDF, corrupt, too long, active content) or ``PdfNeedsPasswordError``. Blocking and CPU-bound:
    call it from a worker thread. An empty list means the file has no text layer (a scan).
    """
    check_header(data)
    scan_raw(data)
    reader = _open(data, password)
    try:
        _assert_inert(reader)
        pages = [(page.extract_text() or "")[:PAGE_TEXT_CHARS] for page in reader.pages]
    except PdfRejectedError:
        raise
    except Exception as exc:
        logger.warning("statement_pdf_unreadable", error_type=type(exc).__name__)
        raise PdfRejectedError("pdf_corrupt", "No se pudo leer el PDF; está dañado o tiene un formato no soportado.") from None
    return [text for text in pages if text.strip()]
