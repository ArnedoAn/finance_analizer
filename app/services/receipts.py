"""Receipts by photo (phase 15): image → metadata-free bytes → vision model → structured result → match → optional split.

Privacy: the image exists only in the memory of the request and of the background task; its EXIF/GPS/XMP are removed
before anything is sent (``image_clean``) and it is never written anywhere. What is stored is the structured reading,
encrypted with the field-encryption key (``Receipt.data``: merchant, NIT, items, payment method, suggestions) plus a
few plain columns (status, total, date, linked movement). Free text coming from the model is redacted first.
Reading a receipt is an owner-requested task: it is recorded in ``ai_budget`` (``receipt_vision``), never braked.

Statuses: ``processing``, ``ready``, ``needs_review`` (``reason``: ``items_total_mismatch`` or ``date_missing``) and
``failed`` (``reason``: ``vision_unavailable``, ``not_a_receipt``, ``interrupted``, ``internal_error``).
"""
import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.clients import vision
from app.core.config import get_settings
from app.core.exceptions import FinanceAnalyzerError, PermanentError
from app.core.logging import get_logger
from app.core.timezone import now_utc
from app.db.models import InboxItem, Receipt
from app.models.schemas import ManualTransaction
from app.parsers import normalize_merchant
from app.services import ai_budget, audit, image_clean, ledger, own_accounts
from app.services.ledger import LedgerDraft, LedgerSplit
from app.services.statements import similarity

logger = get_logger(__name__)

RETENTION_DAYS = 180
STALE_PROCESSING = timedelta(minutes=30)
DATE_TOLERANCE_DAYS = 2
MERCHANT_SIMILARITY = 0.6
AMBIGUOUS_GAP = 0.02
TIP_RATIO = Decimal("0.20")
BUDGET_KIND = "receipt_vision"
_CENTS = Decimal("0.01")


class ReceiptError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def _money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


def _zone() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


# --- Pure helpers ---------------------------------------------------------------------------------------------------

def check_items(items: list[dict], total: Decimal, tax: Decimal | None, tip: Decimal | None) -> dict:
    """Do the lines add up to the total? Prices may include tax and tip or not, so the sum is also tried plus tax, plus tip
    and plus both. The tolerance covers rounding: 0.5 % of the total, at least $1 and at most $100."""
    tolerance = min(max(total * Decimal("0.005"), Decimal("1.00")), Decimal("100.00")).quantize(_CENTS)
    if not items:
        return {"sum": None, "difference": None, "ok": None, "tolerance": _money(tolerance)}
    subtotal = sum((item["amount"] for item in items), Decimal(0))
    extras = {Decimal(0), tax or Decimal(0), tip or Decimal(0), (tax or Decimal(0)) + (tip or Decimal(0))}
    difference = min((subtotal + extra - total for extra in extras), key=abs)
    return {"sum": _money(subtotal), "difference": _money(difference), "ok": abs(difference) <= tolerance, "tolerance": _money(tolerance)}


def propose_split(items: list[dict], movement_total: Decimal, fallback: str | None = None) -> list[dict] | None:
    """Group the lines by category and scale the groups to ``movement_total`` (so tax, tip and rounding are shared pro rata);
    the amounts add up to the movement to the cent. Lines without category go to ``fallback`` (the movement's own category).
    None when there are fewer than two groups or a group does not come out positive."""
    groups: dict[str | None, list[int]] = {}
    for index, item in enumerate(items):
        groups.setdefault(item.get("category") or fallback, []).append(index)
    if len(groups) < 2 or movement_total <= 0:
        return None
    sums = [sum((items[i]["amount"] for i in indexes), Decimal(0)) for indexes in groups.values()]
    subtotal = sum(sums, Decimal(0))
    if subtotal <= 0 or any(value <= 0 for value in sums):
        return None
    amounts = [(value * movement_total / subtotal).quantize(_CENTS, rounding=ROUND_DOWN) for value in sums]
    amounts[amounts.index(max(amounts))] += movement_total - sum(amounts, Decimal(0))
    return [{"category": category, "amount": amount, "items": indexes} for (category, indexes), amount in zip(groups.items(), amounts, strict=True)]


def _split_out(split: list[dict] | None, total: Decimal) -> dict | None:
    if split is None:
        return None
    return {"total": _money(total), "splits": [{"category": row["category"], "amount": _money(row["amount"]), "items": row["items"]} for row in split]}


def _single_category(items: list[dict]) -> str | None:
    names = {item["category"] for item in items}
    return next(iter(names)) if len(names) == 1 and None not in names else None


# --- Upload ---------------------------------------------------------------------------------------------------------

async def _linkable(session, item_id: int, *, receipt_id: int | None = None) -> InboxItem:
    item = await session.get(InboxItem, item_id, populate_existing=True)
    if item is None:
        raise ReceiptError(404, "item_not_found", "No existe ese movimiento.")
    if item.status != "created" or item.transaction_type != "withdrawal" or not item.firefly_transaction_id:
        raise ReceiptError(422, "item_not_linkable", "El recibo solo se puede enlazar a un gasto ya registrado.")
    other = await session.scalar(select(Receipt.id).where(Receipt.item_id == item_id, Receipt.id != (receipt_id or 0)))
    if other is not None:
        raise ReceiptError(409, "item_has_receipt", "Ese movimiento ya tiene un recibo.")
    return item


async def _find(session, digest: str) -> Receipt | None:
    return await session.scalar(select(Receipt).where(Receipt.image_sha256 == digest).execution_options(populate_existing=True))


async def register(session, data: bytes, item_id: int | None, principal) -> tuple[Receipt, bool, bytes, str]:
    """Validate the upload and return ``(receipt, process, clean_image, mime)``.

    The same image (its metadata-free bytes) is idempotent: it returns the known receipt without spending AI, except a
    ``failed`` one, which is reprocessed. ``item_id`` links the receipt to an existing expense.
    """
    if not data:
        raise ReceiptError(422, "empty_file", "El archivo está vacío.")
    if len(data) > image_clean.MAX_IMAGE_BYTES:
        raise ReceiptError(413, "payload_too_large", "La imagen supera los 6 MB.")
    try:
        clean, mime = image_clean.sanitize(data)
    except image_clean.ImageRejectedError as exc:
        raise ReceiptError(422, exc.code, exc.detail) from exc
    if item_id is not None:
        await _linkable(session, item_id)
    digest = hashlib.sha256(clean).hexdigest()
    existing = await _find(session, digest)
    if existing is not None:
        return await _reuse(session, existing, item_id, clean, mime)
    receipt = Receipt(
        status="processing", image_sha256=digest, item_id=item_id, match_kind="linked" if item_id is not None else None,
    )
    session.add(receipt)
    try:
        await session.flush()
    except IntegrityError:  # a concurrent upload of the same image won the unique index
        await session.rollback()
        existing = await _find(session, digest)
        if existing is None:
            raise
        return await _reuse(session, existing, item_id, clean, mime)
    await audit.record(
        session, action="receipt.upload", target_type="receipt", target_id=receipt.id,
        detail={"bytes": len(clean), "mime": mime, "linked": item_id is not None}, principal=principal,
    )
    await session.commit()
    return receipt, True, clean, mime


async def _reuse(session, receipt: Receipt, item_id: int | None, clean: bytes, mime: str) -> tuple[Receipt, bool, bytes, str]:
    if receipt.status == "failed":
        receipt.status, receipt.reason, receipt.data, receipt.llm_calls, receipt.model = "processing", None, None, 0, None
        receipt.total = receipt.receipt_date = receipt.applied = None
        if item_id is not None:
            receipt.item_id, receipt.match_kind = item_id, "linked"
        await session.commit()
        return receipt, True, clean, mime
    if item_id is not None and receipt.item_id is None:
        receipt.item_id, receipt.match_kind = item_id, "linked"
        await session.commit()
    return receipt, False, clean, mime


# --- Processing -----------------------------------------------------------------------------------------------------

async def _finish(session, receipt: Receipt, status: str, reason: str | None = None) -> None:
    receipt.status, receipt.reason = status, reason
    await session.commit()


async def process(session, firefly, receipt_id: int, image: bytes, mime: str) -> None:
    """Background pipeline. ``image`` lives only in this call; failures end in ``failed``, never in an exception."""
    receipt = await session.get(Receipt, receipt_id)
    if receipt is None:
        return
    try:
        await _run(session, firefly, receipt, image, mime)
    except Exception as exc:
        await session.rollback()
        logger.error("receipt_failed", receipt_id=receipt_id, error_type=type(exc).__name__)
        receipt = await session.get(Receipt, receipt_id)
        if receipt is not None:
            await _finish(session, receipt, "failed", "internal_error")


async def _record_budget(session, usage: dict) -> None:
    try:
        await ai_budget.record(session, BUDGET_KIND, usage.get("calls", 1), usage, auto=False)
    except Exception as exc:  # accounting must never fail the work
        logger.warning("receipt_budget_record_failed", error_type=type(exc).__name__)


async def _categories(firefly) -> list[str]:
    try:
        return [name for name in await firefly.categories() if isinstance(name, str) and name.strip()]
    except Exception as exc:  # without the owner's categories the receipt is still read, just not categorized
        logger.warning("receipt_categories_unavailable", error_type=type(exc).__name__)
        return []


async def _run(session, firefly, receipt: Receipt, image: bytes, mime: str) -> None:
    zone = _zone()
    uploaded = receipt.created_at.astimezone(zone).date()
    usage: dict = {}
    try:
        parsed, model = await vision.read_receipt(image, mime, await _categories(firefly), uuid4().hex, ai_budget.today(), usage)
    except vision.VisionUnavailableError:
        await _record_budget(session, usage)
        await _finish(session, receipt, "failed", "vision_unavailable")
        return
    await _record_budget(session, usage)
    receipt.llm_calls, receipt.model = usage.get("calls", 1), model
    if parsed.get("not_a_receipt"):
        await _finish(session, receipt, "failed", "not_a_receipt")
        return
    check = check_items(parsed["items"], parsed["total"], parsed["tax"], parsed["tip"])
    receipt.total, receipt.receipt_date = parsed["total"], parsed["date"]
    candidates: list[int] = []
    if receipt.item_id is None:
        matched, candidates = await find_match(session, parsed, parsed["date"] or uploaded, zone)
        if matched is not None:
            receipt.item_id, receipt.match_kind = matched.id, "matched"
    item = await session.get(InboxItem, receipt.item_id) if receipt.item_id is not None else None
    split = propose_split(parsed["items"], item.amount if item is not None and item.amount else parsed["total"]) if check["ok"] else None
    receipt.data = json.dumps({
        "merchant": parsed["merchant"], "nit": parsed["nit"], "date": parsed["date"].isoformat() if parsed["date"] else None,
        "total": _money(parsed["total"]), "tax": _money(parsed["tax"]), "tip": _money(parsed["tip"]),
        "payment_method": parsed["payment_method"],
        "items": [{"description": row["description"], "quantity": format(row["quantity"].normalize(), "f"), "amount": _money(row["amount"]), "category": row["category"]} for row in parsed["items"]],
        "items_check": check, "suggested_category": _single_category(parsed["items"]) if parsed["items"] else None,
        "split_suggestion": _split_out(split, item.amount if item is not None and item.amount else parsed["total"]),
        "candidates": candidates,
    }, ensure_ascii=False)
    if check["ok"] is False:
        await _finish(session, receipt, "needs_review", "items_total_mismatch")
    elif parsed["date"] is None:
        await _finish(session, receipt, "needs_review", "date_missing")
    else:
        await _finish(session, receipt, "ready")


async def find_match(session, parsed: dict, day: date, zone: ZoneInfo) -> tuple[InboxItem | None, list[int]]:
    """The registered expense this receipt belongs to, or ``(None, candidate ids)`` when it is ambiguous.

    Same amount, or the amount plus a tip (up to 20 % of the total, or exactly the printed tip); date within two days
    (Bogotá calendar); merchant clearly similar. Movements already linked to another receipt do not count.
    """
    merchant, total = parsed["merchant"], parsed["total"]
    if not merchant:
        return None, []
    upper = max(total * (1 + TIP_RATIO), total + (parsed["tip"] or Decimal(0))).quantize(_CENTS)
    start = datetime.combine(day - timedelta(days=DATE_TOLERANCE_DAYS), time.min, zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=DATE_TOLERANCE_DAYS + 1), time.min, zone).astimezone(UTC)
    linked = select(Receipt.item_id).where(Receipt.item_id.is_not(None))
    query = select(InboxItem).where(
        InboxItem.status == "created", InboxItem.transaction_type == "withdrawal", InboxItem.amount >= total,
        InboxItem.amount <= upper, InboxItem.occurred_at >= start, InboxItem.occurred_at < end, InboxItem.id.not_in(linked),
    )
    scored = []
    for item in (await session.scalars(query)).all():
        if (item.currency or "COP") != "COP" or not item.merchant:
            continue
        name = similarity(item.merchant, merchant)
        if name < MERCHANT_SIMILARITY:
            continue
        distance = abs((item.occurred_at.astimezone(zone).date() - day).days)
        tip = item.amount - total
        if tip > 0 and tip != (parsed["tip"] or Decimal(-1)) and tip > total * TIP_RATIO:
            continue
        scored.append((name - 0.05 * distance - (0.02 if tip > 0 else 0.0), item))
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    if not scored:
        return None, []
    if len(scored) > 1 and scored[0][0] - scored[1][0] < AMBIGUOUS_GAP:
        return None, [item.id for _, item in scored[:3]]
    return scored[0][1], []


# --- Reads ----------------------------------------------------------------------------------------------------------

async def get(session, receipt_id: int) -> Receipt:
    receipt = await session.get(Receipt, receipt_id, populate_existing=True)
    if receipt is None:
        raise ReceiptError(404, "receipt_not_found", "No existe ese recibo.")
    return receipt


def _data(receipt: Receipt) -> dict:
    try:
        return json.loads(receipt.data) if receipt.data else {}
    except ValueError:
        return {}


def _head(receipt: Receipt, merchant: str | None) -> dict:
    return {
        "id": receipt.id, "status": receipt.status, "reason": receipt.reason, "merchant": merchant,
        "date": receipt.receipt_date.isoformat() if receipt.receipt_date else None, "total": _money(receipt.total),
        "item_id": receipt.item_id, "match_kind": receipt.match_kind, "applied": receipt.applied is not None,
        "created_at": receipt.created_at,
    }


async def _movement(session, receipt: Receipt) -> dict | None:
    item = await session.get(InboxItem, receipt.item_id) if receipt.item_id is not None else None
    if item is None:
        return None
    return {
        "id": item.id, "amount": _money(item.amount), "merchant": item.merchant, "firefly_id": item.firefly_transaction_id,
        "date": item.occurred_at.astimezone(_zone()).date().isoformat(),
    }


async def detail(session, receipt: Receipt) -> dict:
    data = _data(receipt)
    return {
        **_head(receipt, data.get("merchant")), "model": receipt.model, "nit": data.get("nit"), "tax": data.get("tax"),
        "tip": data.get("tip"), "payment_method": data.get("payment_method"), "items": data.get("items", []),
        "items_check": data.get("items_check"), "suggested_category": data.get("suggested_category"),
        "match": {"kind": receipt.match_kind, "item": await _movement(session, receipt), "candidates": data.get("candidates", [])},
        "split_suggestion": data.get("split_suggestion") if receipt.applied is None else None,
        "applied_result": receipt.applied,
    }


async def listing(session, *, status: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    query = select(Receipt)
    if status:
        query = query.where(Receipt.status == status)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.scalars(query.order_by(Receipt.id.desc()).limit(limit).offset(offset).execution_options(populate_existing=True))
    return [_head(row, _data(row).get("merchant")) for row in rows.all()], total


# --- Apply ----------------------------------------------------------------------------------------------------------

async def _cash_account(session, processor) -> str | None:
    accounts = await own_accounts.load(session)
    return next((account.firefly_account for account in accounts if account.kind == "cash"), None)


async def _create_cash(session, processor, receipt: Receipt, data: dict) -> InboxItem:
    if receipt.total is None:
        raise ReceiptError(409, "receipt_not_ready", "El recibo no tiene un total legible.")
    merchant = normalize_merchant(data.get("merchant") or "") or None
    when = datetime.combine(receipt.receipt_date, time(12, 0)) if receipt.receipt_date else None
    payload = ManualTransaction(
        client_ref=f"receipt:{receipt.id}", amount=receipt.total, description=f"Recibo {data.get('merchant') or 'sin comercio'}"[:200],
        type="withdrawal", category=data.get("suggested_category"), account=await _cash_account(session, processor),
        merchant=merchant, date=when,
    )
    try:
        item = await processor.manual(payload)
    except ValueError as exc:
        await session.rollback()
        raise ReceiptError(422, "cash_invalid", str(exc)[:200]) from exc
    except FinanceAnalyzerError as exc:
        await session.rollback()
        raise ReceiptError(502, "upstream_unavailable", "Firefly no respondió; inténtalo de nuevo.") from exc
    if item.status != "created":
        await session.commit()
        raise ReceiptError(409, "cash_not_created", "El movimiento en efectivo quedó en la bandeja para revisión.")
    return item


async def _apply_split(session, processor, receipt: Receipt, data: dict, firefly_id: str, principal) -> int:
    transaction = await ledger.get(session, processor, firefly_id)
    if len(transaction["splits"]) != 1:
        raise ReceiptError(409, "already_split", "El movimiento ya está dividido.")
    base = transaction["splits"][0]
    if transaction["type"] != "withdrawal" or base["currency"] != "COP" or base["foreign_amount"]:
        raise ReceiptError(409, "not_splittable", "Este movimiento no se puede dividir desde un recibo.")
    items = [{**row, "amount": Decimal(row["amount"])} for row in data.get("items", [])]
    rows = propose_split(items, Decimal(base["amount"]), base["category"]) if (data.get("items_check") or {}).get("ok") else None
    if rows is None:
        raise ReceiptError(409, "no_split_suggestion", "El recibo no permite proponer una división por categoría.")
    splits = []
    for row in rows:
        names = [items[index]["description"] for index in row["items"]][:4]
        splits.append(LedgerSplit(
            amount=row["amount"], currency="COP", source_account=base["source_account"], destination_account=base["destination_account"],
            category=row["category"], budget=base["budget"], bill=base["bill"], tags=base["tags"], notes=base["notes"],
            description=", ".join(names)[:255] or None,
        ))
    draft = LedgerDraft(
        type="withdrawal", date=datetime.fromisoformat(transaction["date"]), description=transaction["description"],
        notes=transaction["notes"], splits=splits,
    )
    _, change = await ledger.update(session, processor, firefly_id, draft, principal)
    return change.id


async def apply(session, processor, receipt: Receipt, principal, *, split: bool, create_cash: bool, item_id: int | None) -> dict:
    """Link the receipt to a movement, create the cash expense and/or split the movement by category.

    ``item_id`` (re)links the receipt to that expense. ``create_cash`` creates a cash expense through ``Processor.manual``
    (idempotent by ``receipt:<id>``) and only when the receipt has no movement. ``split`` rewrites the movement as one
    split per category through ``ledger.update`` (undoable with the returned ``change_id``).
    """
    if receipt.status not in {"ready", "needs_review"}:
        raise ReceiptError(409, "receipt_not_ready", "El recibo aún no está leído.")
    if not (split or create_cash or item_id is not None):
        raise ReceiptError(422, "nothing_to_apply", "Indica split, create_cash o item_id.")
    if receipt.applied is not None and (split or create_cash):
        raise ReceiptError(409, "already_applied", "Este recibo ya se aplicó.")
    data = _data(receipt)
    if item_id is not None:
        await _linkable(session, item_id, receipt_id=receipt.id)
        receipt.item_id, receipt.match_kind = item_id, "linked"
        await session.commit()
    created = None
    if create_cash:
        if receipt.item_id is not None:
            raise ReceiptError(409, "movement_exists", "El recibo ya tiene un movimiento; no se crea otro en efectivo.")
        item = await _create_cash(session, processor, receipt, data)
        receipt.item_id, receipt.match_kind = item.id, "cash"
        created = {"inbox_id": item.id, "firefly_id": item.firefly_transaction_id}
        await session.commit()
    change_id = None
    if split:
        item = await session.get(InboxItem, receipt.item_id, populate_existing=True) if receipt.item_id is not None else None
        if item is None or not item.firefly_transaction_id:
            raise ReceiptError(409, "no_movement", "El recibo no está enlazado a un movimiento.")
        try:
            change_id = await _apply_split(session, processor, receipt, data, item.firefly_transaction_id, principal)
        except ledger.LedgerError as exc:
            await session.rollback()
            raise ReceiptError(exc.status, exc.code, exc.detail) from exc
        except PermanentError as exc:
            await session.rollback()
            error = ledger.firefly_rejection(exc)
            raise ReceiptError(error.status, error.code, error.detail) from exc
        except FinanceAnalyzerError as exc:
            await session.rollback()
            raise ReceiptError(502, "upstream_unavailable", "Firefly no respondió; inténtalo de nuevo.") from exc
    if split or create_cash:
        receipt.applied = {"split": split, "create_cash": create_cash, "item_id": receipt.item_id, "change_id": change_id, "applied_at": now_utc().isoformat()}
    await audit.record(
        session, action="receipt.apply", target_type="receipt", target_id=receipt.id,
        detail={"split": split, "create_cash": create_cash, "item_id": receipt.item_id, "change_id": change_id}, principal=principal,
    )
    await session.commit()
    return {"receipt": await detail(session, receipt), "created": created, "change_id": change_id}


# --- Deletion and retention -----------------------------------------------------------------------------------------

async def delete_receipt(session, receipt: Receipt, principal) -> None:
    await audit.record(session, action="receipt.delete", target_type="receipt", target_id=receipt.id, detail={"status": receipt.status}, principal=principal)
    await session.delete(receipt)
    await session.commit()


async def purge(session, now: datetime | None = None) -> int:
    """Delete receipts older than ``RETENTION_DAYS`` and fail the ones stuck in ``processing`` (the server restarted)."""
    now = now or now_utc()
    await session.execute(
        update(Receipt).where(Receipt.status == "processing", Receipt.created_at < now - STALE_PROCESSING)
        .values(status="failed", reason="interrupted").execution_options(synchronize_session=False)
    )
    result = await session.execute(delete(Receipt).where(Receipt.created_at < now - timedelta(days=RETENTION_DAYS)))
    await session.commit()
    return result.rowcount or 0
