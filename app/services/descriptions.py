"""What an automatic movement is called: the description that goes to Firefly and shows in the inbox.

E-mail subjects of several banks are always the same («Alertas y Notificaciones»), so every movement looked alike.
``derive`` builds the description from the extraction instead, in this order: the LLM ``summary`` (without the
amount), a template by type («Compra en EXITO», «Pago PSE a RAPPI», «Transferencia a MARIA», «Recibiste de ANA»,
«Devolución de TIENDA», «Retiro en CAJERO»), and nothing, in which case ``title`` falls back to the subject when it is
not generic and to the merchant otherwise.

A subject is generic (``is_generic_subject``) when it is in the known list, equals the sender (SMS titles), is empty,
or the very same subject came from the same sender at least ``REPEAT_MIN`` times in the last ``REPEAT_DAYS`` days.
The repetition set is one cheap grouped query cached per process (``refresh``, ``CACHE_TTL``), so
``is_generic_subject`` itself is synchronous and callers refresh first.

The owner's own description (``item.description`` set by a correction) always wins: ``derive`` is only used while it is empty.
"""
import re
import time
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import func, select

from app.core.logging import get_logger
from app.core.timezone import now_utc
from app.db.models import InboxItem
from app.parsers.common import KEY_MERCHANT, QR_MERCHANT, fold
from app.security.redaction import redact

logger = get_logger(__name__)

REPEAT_MIN = 5
REPEAT_DAYS = 60
CACHE_TTL = 3600
MAX_DESCRIPTION = 200
TRANSFER_TAG = "transferencia"

# Folded, punctuation-free subjects that say nothing about the movement.
GENERIC_SUBJECTS = frozenset({
    "alertas y notificaciones", "alertas y notificaciones bancolombia", "bancolombia te informa", "notificacion de transaccion",
    "notificacion bancolombia", "notificaciones bancolombia", "alerta de transaccion", "alerta bancolombia", "alertas bancolombia",
    "transaccion exitosa", "transaccion realizada", "transaccion aprobada", "nequi te cuenta", "nequi te informa",
    "davivienda te informa", "notificacion davivienda", "alerta davivienda", "daviplata te informa", "notificacion daviplata",
    "nu te informa", "nu te cuenta", "notificacion de nu", "lulo bank te informa", "lulo te informa", "notificacion lulo",
    "notificacion de compra", "notificacion de movimiento", "alerta de compra", "alerta de movimiento", "aviso de transaccion",
    "informacion de tu transaccion", "informacion de transaccion", "resumen de transaccion", "comprobante de transaccion",
    "movimiento en tu cuenta", "movimientos de tu cuenta", "tu transaccion fue exitosa", "compra realizada", "compra exitosa",
    "compra aprobada", "pago exitoso", "pago realizado", "transferencia exitosa", "transferencia realizada",
})
_PREFIX = re.compile(r"^(?:(?:re|rv|fw|fwd)\s*:\s*)+")
_AMOUNT = re.compile(
    r"\s*(?:\b(?:por|de|un)\s+(?:valor\s+de\s+)?)?(?:US\$|USD|COP|\$)\s*\d[\d.,]*(?:\s*(?:COP|USD|pesos))?", re.I
)
_PSE = re.compile(r"\bpse\b")
_QR = re.compile(r"codigo\s+qr")
_WITHDRAWAL = re.compile(r"\b(?:retiraste|retiro)\b")
_PERSON_TEMPLATES = frozenset({"transferiste", "enviaste", "recibiste", "metiste", "sacaste"})
DERIVED_PREFIXES = ("Compra en ", "Pago a ", "Pago PSE a ", "Retiro en ", "Transferencia a ", "Recibiste de ", "Devolución de ")

_repeated: set[tuple[str, str]] = set()
_loaded_at: float | None = None


def normalize_subject(value: str | None) -> str:
    """Folded, punctuation-free, single-spaced subject without reply/forward prefixes."""
    text = _PREFIX.sub("", fold(value or "").strip())
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _sender_key(sender: str | None) -> str:
    return (sender or "").casefold().strip()


def is_generic_subject(sender: str | None, subject: str | None) -> bool:
    """True when the subject cannot tell one movement from another (see the module docstring)."""
    key = normalize_subject(subject)
    if not key or key in GENERIC_SUBJECTS or key == normalize_subject(sender):
        return True
    return (_sender_key(sender), key) in _repeated


def invalidate() -> None:
    """Forget the cached repetition set (tests, and after a mass purge of subjects)."""
    global _loaded_at
    _repeated.clear()
    _loaded_at = None


async def refresh(session, *, force: bool = False) -> None:
    """Load the repeated (sender, subject) pairs of the last ``REPEAT_DAYS`` days unless the cache is fresh."""
    global _loaded_at
    if not force and _loaded_at is not None and time.monotonic() - _loaded_at < CACHE_TTL:
        return
    rows = await session.execute(
        select(InboxItem.sender, InboxItem.subject_or_title)
        .where(InboxItem.source != "manual", InboxItem.subject_or_title != "", InboxItem.created_at >= now_utc() - timedelta(days=REPEAT_DAYS))
        .group_by(InboxItem.sender, InboxItem.subject_or_title)
        .having(func.count() >= REPEAT_MIN)
    )
    found = {(_sender_key(sender), normalize_subject(subject)) for sender, subject in rows}
    _repeated.clear()
    _repeated.update(found)
    _loaded_at = time.monotonic()


def clean_summary(summary: str | None) -> str | None:
    """The LLM sentence as a description: redacted, without the amount when it can be taken out, bounded."""
    if not summary:
        return None
    text = " ".join((redact(summary) or "").split())
    stripped = " ".join(_AMOUNT.sub("", text).split()).strip(" ,.;:-")
    text = stripped or text.strip(" ,.;:-")
    return (text[:1].upper() + text[1:])[:MAX_DESCRIPTION] or None


def is_derived(description: str | None) -> bool:
    return bool(description) and description.startswith(DERIVED_PREFIXES)


def derive(item, extraction, text: str = "") -> str | None:
    """Description for an automatic item whose fields (merchant, final type, accounts) were just decided, or None."""
    summary = clean_summary(extraction.summary)
    if summary:
        return summary
    merchant = item.merchant
    template = (extraction.template_id or "").rsplit(".", 1)[-1]
    folded = fold(text or "")
    if extraction.kind == "refund":
        return f"Devolución de {merchant}" if merchant else None
    if item.transaction_type == "transfer":
        # Between own accounts: after the processor swapped them, the counterparty is the asset account of a deposit
        # and the destination of everything else.
        if extraction.kind in {"deposit", "refund"}:
            return f"Recibiste de {item.asset_account}" if item.asset_account else None
        return f"Transferencia a {item.destination_account}" if item.destination_account else None
    if merchant == QR_MERCHANT:
        return "Pago QR"
    if merchant and merchant.startswith(KEY_MERCHANT):
        # A Bre-B / Bancolombia «llave»: only its last four characters are known.
        label = "llave ···" + merchant[len(KEY_MERCHANT):]
        if item.transaction_type == "deposit":
            return f"Recibiste de {label}"
        if _QR.search(folded):
            return f"Pago QR a {label}"
        return f"Transferencia a {label}" if template in _PERSON_TEMPLATES else f"Pago a {label}"
    if not merchant:
        return None
    person = extraction.kind == "transfer" or TRANSFER_TAG in (item.tags or []) or template in _PERSON_TEMPLATES
    if item.transaction_type == "deposit":
        return f"Recibiste de {merchant}"
    if person:
        return f"Transferencia a {merchant}"
    if _PSE.search(folded):
        return f"Pago PSE a {merchant}"
    if template == "retiro" or (_WITHDRAWAL.search(folded) and "compra" not in folded):
        return f"Retiro en {merchant}"
    if template == "pagaste":
        return f"Pago a {merchant}"
    return f"Compra en {merchant}"


def title(item) -> str | None:
    """What Firefly receives as the description: the stored one, else a non-generic subject, else the merchant."""
    if item.description:
        return item.description
    if item.subject_or_title and not is_generic_subject(item.sender, item.subject_or_title):
        return item.subject_or_title
    return item.merchant


# ---- fixing what was already created ---------------------------------------------------------------------------

EXAMPLES = 10
SUMMARY_LIMIT = 300  # the length of an LLM summary: a compacted e-mail keeps one in ``raw_text`` only when it is that short and single-line


def rebuild(item) -> str | None:
    """The description of a created e-mail item recomputed from what it keeps (no extraction is stored)."""
    summary = item.raw_text if item.method == "llm" and item.raw_text and "\n" not in item.raw_text and len(item.raw_text) <= SUMMARY_LIMIT else None
    guessed = SimpleNamespace(summary=summary, template_id=item.template_id or "", kind=item.original_kind or item.transaction_type or "")
    return derive(item, guessed, item.raw_text or "")


async def candidates(session, ids=None) -> list[tuple[InboxItem, str]]:
    """Created e-mail items whose Firefly description is their generic subject (no description of their own was ever
    stored), with the description that replaces it. Items for which nothing better can be derived are left out."""
    await refresh(session, force=True)
    query = select(InboxItem).where(
        InboxItem.status == "created", InboxItem.source == "email", InboxItem.firefly_transaction_id.is_not(None),
        InboxItem.description.is_(None), InboxItem.subject_or_title != "",
    ).order_by(InboxItem.id)
    if ids is not None:
        query = query.where(InboxItem.id.in_(list(ids)))
    found = []
    for item in (await session.scalars(query)).all():
        if is_generic_subject(item.sender, item.subject_or_title) and (new := rebuild(item)):
            found.append((item, new))
    return found


def example(item, new: str) -> dict:
    return {"id": item.id, "before": item.subject_or_title, "after": new}


async def fix(session, processor, ids, principal, group_id: str) -> dict:
    """Retitle the items in Firefly through the ledger edit path (audited, one change each, undoable as ``group_id``).

    A movement whose Firefly description is no longer the subject (the owner edited it) is skipped. Each item is
    independent: a failure is logged and counted, never aborts the rest. Commits per item."""
    from app.services import ledger

    done = {"fixed": 0, "skipped": 0, "failed": 0}
    for item, new in await candidates(session, ids):
        try:
            changed = await ledger.retitle(session, processor, item.firefly_transaction_id, item.subject_or_title, new, principal, group_id)
            done["fixed" if changed else "skipped"] += 1
        except Exception as exc:
            await session.rollback()
            done["failed"] += 1
            logger.warning("description_fix_item_failed", error_type=type(exc).__name__)
    return done
