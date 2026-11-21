"""Assistant batches: fixed-window proposals, apply and revert (contract sections 9 and 14).

Creating a proposal never touches the ledger. ``apply`` and ``revert`` go through the normal
review path (``Processor.review`` / ``Processor.manual``), so Firefly writes, validation and the
audit trail are identical to a manual correction. Functions commit their own work; ``apply`` and
``revert`` report per-proposal failures instead of aborting the whole batch.
"""
from datetime import UTC, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)
from sqlalchemy import func, select

from app.core.exceptions import FinanceAnalyzerError
from app.core.logging import get_logger
from app.core.timezone import now_utc
from app.db.models import InboxEvent, InboxItem
from app.db.models_assistant import Proposal, ProposalBatch
from app.models.schemas import ManualTransaction
from app.services import audit

logger = get_logger(__name__)

MAX_WINDOW = timedelta(days=31)
MAX_PROPOSALS = 50
MAX_SPLIT_PARTS = 20
REVERT_WINDOW = timedelta(days=7)
EDIT_FIELDS = frozenset({"category", "merchant", "description", "firefly_account", "destination_account", "occurred_at", "amount", "transaction_type", "tags"})
ALLOWED_FIELDS = EDIT_FIELDS | {"void", "split"}
RISKY_FIELDS = frozenset({"amount", "void", "split"})
# Fields whose previous value may be empty and still be restored (to empty).
RESTORABLE_EMPTY = frozenset({"category", "description", "tags"})
LEDGER_STATUSES = frozenset({"created", "needs_review"})

_Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
_Money = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
_Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]


class _SplitPart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: _Money
    category: _Name
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)] | None = None


_VALUE_TYPES = {
    "category": TypeAdapter(_Name),
    "merchant": TypeAdapter(Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]),
    "description": TypeAdapter(Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None),
    "firefly_account": TypeAdapter(_Name),
    "destination_account": TypeAdapter(_Name | None),
    "occurred_at": TypeAdapter(AwareDatetime),
    "amount": TypeAdapter(_Money),
    "transaction_type": TypeAdapter(Literal["withdrawal", "deposit", "transfer", "refund"]),
    "tags": TypeAdapter(Annotated[list[_Tag], Field(max_length=10)]),
    "split": TypeAdapter(Annotated[list[_SplitPart], Field(min_length=2, max_length=MAX_SPLIT_PARTS)]),
}
_ATTRIBUTES = {"firefly_account": "asset_account"}


class AssistantError(Exception):
    """A client-visible failure; the router maps it to ``{"detail", "code"}`` with ``status``."""

    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _normalize(field: str, value):
    """Validate the type of ``new_value`` for ``field`` and return its JSON-storable form."""
    if field == "void":
        if value is not None and value is not True:
            raise AssistantError(422, "validation_error", "Invalid new_value for void: use null or true")
        return None
    try:
        parsed = _VALUE_TYPES[field].validate_python(value)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise AssistantError(422, "validation_error", f"Invalid new_value for {field}{' at ' + where if where else ''}: {first['msg']}") from exc
    if field == "amount":
        return _money(parsed)
    if field == "occurred_at":
        return parsed.astimezone(UTC).isoformat()
    if field == "description":
        return parsed or None
    if field == "tags":
        return list(dict.fromkeys(parsed))
    if field == "split":
        return [{"amount": _money(part.amount), "category": part.category, **({"description": part.description} if part.description else {})} for part in parsed]
    return parsed


def current_value(item, field: str):
    """The item's present value for ``field`` in the same JSON form ``_normalize`` produces."""
    if field == "void":
        return item.status
    if field in {"amount", "split"}:
        return _money(item.amount) if item.amount is not None else None
    value = getattr(item, _ATTRIBUTES.get(field, field))
    if field == "occurred_at":
        return value.astimezone(UTC).isoformat()
    if field == "tags":
        return list(value or [])
    return value


def revert_deadline(batch):
    return batch.applied_at + REVERT_WINDOW if batch.applied_at else None


async def get_batch(session, batch_id: int) -> ProposalBatch:
    batch = await session.get(ProposalBatch, batch_id, populate_existing=True)
    if batch is None:
        raise AssistantError(404, "not_found", "Batch not found")
    return batch


async def proposals_of(session, batch_id: int) -> list[Proposal]:
    query = select(Proposal).where(Proposal.batch_id == batch_id).order_by(Proposal.id).execution_options(populate_existing=True)
    return list((await session.scalars(query)).all())


async def batch_contents(session, batch) -> tuple[list[Proposal], dict[int, InboxItem]]:
    """Proposals of ``batch`` and their target items keyed by id."""
    proposals = await proposals_of(session, batch.id)
    ids = {proposal.target_id for proposal in proposals}
    items = {}
    if ids:
        query = select(InboxItem).where(InboxItem.id.in_(ids)).execution_options(populate_existing=True)
        items = {item.id: item for item in (await session.scalars(query)).all()}
    return proposals, items


async def create_batch(session, *, window_from, window_to, instruction: str, principal) -> ProposalBatch:
    """Open a batch with a fixed window of at most 31 days that ends no later than now."""
    now = now_utc()
    if window_to <= window_from:
        raise AssistantError(422, "invalid_window", "window.to must be after window.from")
    if window_to - window_from > MAX_WINDOW:
        raise AssistantError(422, "invalid_window", "The window cannot span more than 31 days")
    if window_to > now:
        raise AssistantError(422, "invalid_window", "window.to cannot be in the future")
    batch = ProposalBatch(window_from=window_from, window_to=window_to, instruction=instruction.strip(), status="open", created_at=now)
    session.add(batch)
    await session.flush()
    await audit.record(
        session, action="assistant.batch_created", target_type="batch", target_id=batch.id,
        detail={"window_from": window_from, "window_to": window_to, "instruction_length": len(batch.instruction)}, principal=principal,
    )
    await session.commit()
    return batch


async def add_proposal(session, batch, *, target_id: int, field: str, new_value, reason: str, principal) -> Proposal:
    if batch.status != "open":
        raise AssistantError(409, "conflict", f"Batch is {batch.status}; proposals can only be added to an open batch")
    if field not in ALLOWED_FIELDS:
        raise AssistantError(422, "field_not_allowed", f"Field not allowed: {field}")
    count = await session.scalar(select(func.count()).select_from(Proposal).where(Proposal.batch_id == batch.id))
    if count >= MAX_PROPOSALS:
        raise AssistantError(422, "batch_limit", f"A batch holds at most {MAX_PROPOSALS} proposals")
    item = await session.get(InboxItem, target_id, populate_existing=True)
    if item is None:
        raise AssistantError(404, "not_found", "Inbox item not found")
    if not batch.window_from <= item.occurred_at <= batch.window_to:
        raise AssistantError(422, "outside_window", "The target item is outside the batch window")
    value = _normalize(field, new_value)
    if field == "split":
        if item.amount is None:
            raise AssistantError(422, "validation_error", "The item has no amount to split")
        if sum((Decimal(part["amount"]) for part in value), Decimal(0)) != item.amount:
            raise AssistantError(422, "validation_error", "Split parts must add up to the original amount")
    proposal = Proposal(
        batch_id=batch.id, target_id=item.id, field=field, old_value=current_value(item, field), new_value=value,
        reason=reason.strip(), risky=field in RISKY_FIELDS, status="proposed",
    )
    session.add(proposal)
    await session.flush()
    await audit.record(
        session, action="assistant.proposal_created", target_type="proposal", target_id=proposal.id,
        detail={"batch_id": batch.id, "target_id": item.id, "field": field, "risky": proposal.risky}, principal=principal,
    )
    await session.commit()
    return proposal


def _public_error(exc: Exception) -> str:
    if isinstance(exc, AssistantError):
        return exc.detail
    if isinstance(exc, ValueError | FinanceAnalyzerError):
        return str(exc)[:300]
    return "Unexpected failure; see the server log"


async def _fail(session, model, record_id: int, **values):
    """Discard uncommitted work after a failed step, then record ``values`` on a fresh copy."""
    await session.rollback()
    row = await session.get(model, record_id, populate_existing=True)
    for name, value in values.items():
        setattr(row, name, value)
    await session.commit()


def _manual_copy(item, client_ref: str, amount: Decimal, description: str | None, category: str | None) -> ManualTransaction:
    return ManualTransaction(
        client_ref=client_ref, amount=amount,
        description=(description or item.description or item.subject_or_title or item.merchant or "Restored")[:4000],
        type=item.transaction_type, category=category, account=item.asset_account, merchant=item.merchant,
        destination_account=item.destination_account, date=item.occurred_at,
    )


async def _manual_once(processor, payload: ManualTransaction, label: str):
    """Create a manual movement by ``client_ref``; a copy left ``failed`` by an earlier attempt is retried."""
    existing = await processor.inbox.by_ref("manual:" + payload.client_ref)
    if existing is None:
        item = await processor.manual(payload)
    else:
        item = existing
        if item.status == "failed":
            await processor.inbox.requeue(item.id)
            await processor.session.commit()
            item = await processor.process(item)
    if item.status != "created":
        raise AssistantError(409, "conflict", f"{label} ended as {item.status}")
    return item


async def _apply_edit(processor, item, proposal, principal):
    corrections = {proposal.field: proposal.new_value}
    if item.status == "created":
        await processor.review(item, "edit", corrections, principal=principal)
    elif item.status == "needs_review":
        result = await processor.review(item, "approve", corrections, principal=principal)
        if result.status != "created":
            raise AssistantError(409, "conflict", f"The item ended as {result.status} after approval")
    else:
        raise AssistantError(409, "conflict", f"An item in status {item.status} cannot be edited")


async def _apply_void(processor, item, principal):
    if item.status not in LEDGER_STATUSES:
        raise AssistantError(409, "conflict", f"An item in status {item.status} cannot be voided")
    await processor.review(item, "void", principal=principal)


async def _apply_split(processor, item, proposal, principal):
    if item.status not in LEDGER_STATUSES:
        raise AssistantError(409, "conflict", f"An item in status {item.status} cannot be split")
    if item.amount is None or not item.asset_account or not item.merchant:
        raise AssistantError(409, "conflict", "The item is incomplete and cannot be split")
    parts = proposal.new_value
    if sum((Decimal(part["amount"]) for part in parts), Decimal(0)) != item.amount:
        raise AssistantError(409, "conflict", "The item amount changed; the split no longer adds up")
    known = {name.casefold() for name in await processor.categories()}
    for part in parts:
        if part["category"].casefold() not in known:
            raise AssistantError(422, "validation_error", f"Unknown category: {part['category']}")
    # Children first (idempotent by client_ref) so a failure never loses the original; then void it.
    for number, part in enumerate(parts, 1):
        payload = _manual_copy(item, f"split:{proposal.id}:{number}", Decimal(part["amount"]), part.get("description"), part["category"])
        await _manual_once(processor, payload, f"Split part {number}")
    await processor.review(item, "void", principal=principal)


async def _apply_one(processor, proposal, principal):
    session = processor.session
    item = await processor.inbox.get(proposal.target_id)
    if item is None:
        raise AssistantError(404, "not_found", "Inbox item not found")
    # Marked applied before the work so the review commit persists both together.
    proposal.old_value = current_value(item, proposal.field)
    proposal.status, proposal.error, proposal.applied_at = "applied", None, now_utc()
    if proposal.field == "void":
        await _apply_void(processor, item, principal)
    elif proposal.field == "split":
        await _apply_split(processor, item, proposal, principal)
    else:
        await _apply_edit(processor, item, proposal, principal)
    await session.commit()


async def apply(session, processor, batch_id: int, *, approved_ids: list[int], discard_others: bool, principal):
    """Apply the approved proposals through the normal review path.

    Returns ``(applied, failed)``: ids applied by this call and ``[{"id", "detail"}]`` for the rest.
    Proposals that are already applied are ignored, which makes retries safe.
    """
    batch = await get_batch(session, batch_id)
    ids = list(dict.fromkeys(approved_ids))
    if not ids and not discard_others:
        raise AssistantError(422, "validation_error", "approved_ids is empty")
    known = {proposal.id: proposal for proposal in await proposals_of(session, batch.id)}
    missing = [pid for pid in ids if pid not in known]
    if missing:
        raise AssistantError(422, "validation_error", f"Proposals not in this batch: {missing}")
    if len(ids) > 1 and any(known[pid].risky for pid in ids):
        raise AssistantError(422, "risky_requires_single", "A risky proposal (amount, void, split) must be applied on its own")
    if batch.status in {"reverted", "discarded"}:
        raise AssistantError(409, "conflict", f"Batch is {batch.status}")
    applied, failed = [], []
    for pid in ids:
        proposal = await session.get(Proposal, pid, populate_existing=True)
        if proposal.status == "applied":
            continue
        if proposal.status in {"discarded", "reverted"}:
            failed.append({"id": pid, "detail": f"Proposal is {proposal.status}"})
            continue
        try:
            await _apply_one(processor, proposal, principal)
        except Exception as exc:
            logger.warning("assistant_apply_failed", proposal_id=pid, error_type=type(exc).__name__)
            detail = _public_error(exc)
            await _fail(session, Proposal, pid, status="failed", error=detail, applied_at=None)
            failed.append({"id": pid, "detail": detail})
        else:
            applied.append(pid)
    proposals = await proposals_of(session, batch_id)
    discarded = []
    if discard_others:
        for proposal in proposals:
            if proposal.id not in ids and proposal.status in {"proposed", "failed"}:
                proposal.status = "discarded"
                discarded.append(proposal.id)
    statuses = {proposal.status for proposal in proposals}
    batch = await get_batch(session, batch_id)
    if applied:
        batch.applied_at = now_utc()
    if "applied" in statuses:
        batch.status = "partially_applied" if statuses & {"proposed", "failed"} else "applied"
    elif statuses == {"discarded"}:
        batch.status = "discarded"
    await audit.record(
        session, action="assistant.apply", target_type="batch", target_id=batch.id,
        detail={"applied": applied, "failed": [entry["id"] for entry in failed], "discarded": discarded}, principal=principal,
    )
    await session.commit()
    return applied, failed


async def _recreate(processor, item, client_ref: str, principal):
    """Put a voided ledger movement back as a manual one (``client_ref`` makes it idempotent)."""
    created = await _manual_once(processor, _manual_copy(item, client_ref, item.amount, None, item.category), "The restored movement")
    carried = {name: getattr(item, name) for name in ("description", "tags") if getattr(item, name)}
    if carried:
        await processor.review(created, "edit", carried, principal=principal)


async def _restore_original(processor, item, proposal, principal):
    """Undo a void: a movement that was in the ledger is recreated, one that never was returns to review."""
    if item.firefly_transaction_id:
        await _recreate(processor, item, f"revert:{proposal.id}", principal)
        return
    session = processor.session
    if item.status == "voided":
        item.status = "needs_review"
        session.add(InboxEvent(inbox_id=item.id, action="reverted_void"))
        await audit.record(session, action="inbox.restore", target_type="inbox_item", target_id=item.id, detail={"changes": {"status": {"old": "voided", "new": "needs_review"}}}, principal=principal)


async def _revert_one(processor, proposal, principal):
    session = processor.session
    item = await processor.inbox.get(proposal.target_id)
    if item is None:
        raise AssistantError(404, "not_found", "Inbox item not found")
    if proposal.field == "void":
        await _restore_original(processor, item, proposal, principal)
    elif proposal.field == "split":
        await _restore_original(processor, item, proposal, principal)
        for number in range(1, len(proposal.new_value) + 1):
            child = await processor.inbox.by_ref(f"manual:split:{proposal.id}:{number}")
            if child is not None and child.status != "voided":
                await processor.review(child, "void", principal=principal)
    else:
        if item.status != "created":
            raise AssistantError(409, "conflict", f"An item in status {item.status} cannot be restored")
        if proposal.old_value is None and proposal.field not in RESTORABLE_EMPTY:
            raise AssistantError(409, "conflict", "The previous value was empty and cannot be restored automatically")
        await processor.review(item, "edit", {proposal.field: proposal.old_value}, principal=principal)
    proposal.status, proposal.error, proposal.reverted_at = "reverted", None, now_utc()
    await session.commit()


async def revert(session, processor, batch_id: int, *, principal):
    """Restore the values saved by ``apply`` (newest first) within 7 days of ``applied_at``.

    Returns ``(reverted, failed)`` like ``apply``. Reverting a reverted batch is a no-op.
    """
    batch = await get_batch(session, batch_id)
    if batch.status == "reverted":
        return [], []
    applied = [proposal for proposal in await proposals_of(session, batch.id) if proposal.status == "applied"]
    if not applied or batch.applied_at is None:
        raise AssistantError(409, "conflict", "The batch has nothing to revert")
    if now_utc() > batch.applied_at + REVERT_WINDOW:
        raise AssistantError(409, "revert_expired", "The 7-day revert window has passed")
    reverted, failed = [], []
    for pid in [p.id for p in sorted(applied, key=lambda p: (p.applied_at, p.id), reverse=True)]:
        proposal = await session.get(Proposal, pid, populate_existing=True)
        try:
            await _revert_one(processor, proposal, principal)
        except Exception as exc:
            logger.warning("assistant_revert_failed", proposal_id=pid, error_type=type(exc).__name__)
            detail = _public_error(exc)
            await _fail(session, Proposal, pid, error=detail)
            failed.append({"id": pid, "detail": detail})
        else:
            reverted.append(pid)
    batch = await get_batch(session, batch_id)
    if not any(proposal.status == "applied" for proposal in await proposals_of(session, batch_id)):
        batch.status = "reverted"
    await audit.record(
        session, action="assistant.revert", target_type="batch", target_id=batch.id,
        detail={"reverted": reverted, "failed": [entry["id"] for entry in failed]}, principal=principal,
    )
    await session.commit()
    return reverted, failed
