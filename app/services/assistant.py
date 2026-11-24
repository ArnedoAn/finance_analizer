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
from sqlalchemy import and_, func, or_, select, update

from app.core.exceptions import FinanceAnalyzerError
from app.core.logging import get_logger
from app.core.timezone import now_utc
from app.db.models import InboxEvent, InboxItem
from app.db.models_assistant import Audit, Proposal, ProposalBatch
from app.models.schemas import ManualTransaction
from app.services import assistant_actions as actions
from app.services import audit, ledger
from app.services.assistant_errors import AssistantError

logger = get_logger(__name__)

MAX_WINDOW = timedelta(days=31)
MAX_PROPOSALS = 50
MAX_SPLIT_PARTS = 20
REVERT_WINDOW = timedelta(days=7)
# An ``applying`` proposal older than this lost its worker (crash, cancelled request) and may be claimed again.
CLAIM_TIMEOUT = timedelta(minutes=10)
EDIT_FIELDS = frozenset({"category", "merchant", "description", "firefly_account", "destination_account", "occurred_at", "amount", "transaction_type", "tags"})
ALLOWED_FIELDS = EDIT_FIELDS | {"void", "split"}
RISKY_FIELDS = frozenset({"amount", "void", "split", "transaction_type", "firefly_account", "destination_account", "occurred_at"})
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


def _locked_batch_query(batch_id: int):
    return select(ProposalBatch).where(ProposalBatch.id == batch_id).with_for_update().execution_options(populate_existing=True)


async def _lock_batch(session, batch_id: int) -> ProposalBatch:
    """Reload the batch under ``SELECT ... FOR UPDATE`` (PostgreSQL; SQLite already serializes writers)."""
    batch = (await session.scalars(_locked_batch_query(batch_id))).one_or_none()
    if batch is None:
        raise AssistantError(404, "not_found", "Batch not found")
    return batch


async def proposals_of(session, batch_id: int) -> list[Proposal]:
    query = select(Proposal).where(Proposal.batch_id == batch_id).order_by(Proposal.id).execution_options(populate_existing=True)
    return list((await session.scalars(query)).all())


async def batch_contents(session, batch) -> tuple[list[Proposal], dict[int, InboxItem]]:
    """Proposals of ``batch`` and the inbox items they target, keyed by id (action proposals have no item)."""
    proposals = await proposals_of(session, batch.id)
    ids = {proposal.target_id for proposal in proposals if proposal.target_id is not None}
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


async def _open_batch(session, batch_id: int) -> ProposalBatch:
    # The row lock serializes concurrent creators so the 50-proposal cap cannot be overshot.
    batch = await _lock_batch(session, batch_id)
    if batch.status != "open":
        raise AssistantError(409, "conflict", f"Batch is {batch.status}; proposals can only be added to an open batch")
    return batch


async def _check_room(session, batch) -> None:
    count = await session.scalar(select(func.count()).select_from(Proposal).where(Proposal.batch_id == batch.id))
    if count >= MAX_PROPOSALS:
        raise AssistantError(422, "batch_limit", f"A batch holds at most {MAX_PROPOSALS} proposals")


async def _store(session, proposal: Proposal, detail: dict, principal) -> Proposal:
    session.add(proposal)
    await session.flush()
    await audit.record(session, action="assistant.proposal_created", target_type="proposal", target_id=proposal.id, detail=detail, principal=principal)
    await session.commit()
    return proposal


async def add_proposal(session, batch, *, target_id: int, field: str, new_value, reason: str, principal) -> Proposal:
    """Propose a change to one inbox item."""
    batch = await _open_batch(session, batch.id)
    if field not in ALLOWED_FIELDS:
        raise AssistantError(422, "field_not_allowed", f"Field not allowed: {field}")
    await _check_room(session, batch)
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
    return await _store(session, proposal, {"batch_id": batch.id, "target_id": item.id, "field": field, "risky": proposal.risky}, principal)


async def add_action(session, processor, batch, *, field: str, target_ref, new_value, reason: str, principal) -> Proposal:
    """Propose a category, registered-movement, budget or rule action (see ``assistant_actions``)."""
    batch = await _open_batch(session, batch.id)
    await _check_room(session, batch)
    prepared = await actions.prepare(session, processor, batch, await proposals_of(session, batch.id), field, target_ref, new_value)
    proposal = Proposal(
        batch_id=batch.id, target_id=None, target_ref=prepared.target_ref, subject=prepared.subject, field=field,
        old_value=prepared.old_value, new_value=prepared.new_value, reason=reason.strip(), risky=prepared.risky, status="proposed",
    )
    return await _store(session, proposal, {"batch_id": batch.id, "target_ref": prepared.target_ref, "field": field, "risky": prepared.risky}, principal)


async def propose(session, processor, batch, *, field: str, target_id: int | None, target_ref, new_value, reason: str, principal) -> Proposal:
    """One proposal, either an action (``assistant_actions``) or an edit of an inbox item; shared by the single and the bulk endpoints."""
    if field in actions.FIELDS:
        return await add_action(session, processor, batch, field=field, target_ref=target_ref, new_value=new_value, reason=reason, principal=principal)
    if target_id is None:
        raise AssistantError(422, "validation_error", "target_id is required")
    return await add_proposal(session, batch, target_id=target_id, field=field, new_value=new_value, reason=reason, principal=principal)


async def propose_bulk(session, processor, batch_id: int, entries: list[dict | AssistantError], principal) -> list[Proposal | AssistantError]:
    """``propose`` for each entry in order; an entry that is already an ``AssistantError`` (it failed validation) or fails here does not stop the rest.

    A missing batch (404) or one that is not open (409) fails the whole call. Every ``propose`` commits its own
    proposal, so when one fails the session is rolled back: that drops only its partial work and releases the
    batch row lock, and nothing already created is lost (a savepoint would not survive the commit inside ``_store``).
    Errors that are not ``AssistantError`` propagate.
    """
    await _open_batch(session, batch_id)
    await session.rollback()
    outcomes: list[int | AssistantError] = []  # a proposal id, kept apart from the object because a rollback expires it
    for entry in entries:
        if isinstance(entry, AssistantError):
            outcomes.append(entry)
            continue
        try:
            # Reloaded each time: a rollback expires every loaded object.
            outcomes.append((await propose(session, processor, await get_batch(session, batch_id), **entry, principal=principal)).id)
        except AssistantError as exc:
            await session.rollback()
            outcomes.append(exc)
    ids = [outcome for outcome in outcomes if isinstance(outcome, int)]
    query = select(Proposal).where(Proposal.id.in_(ids)).execution_options(populate_existing=True)
    created = {proposal.id: proposal for proposal in await session.scalars(query)} if ids else {}
    return [created[outcome] if isinstance(outcome, int) else outcome for outcome in outcomes]


def _public_error(exc: Exception) -> str:
    if isinstance(exc, AssistantError):
        return exc.detail
    if isinstance(exc, ledger.LedgerError):
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


def _require_created(item, verb: str):
    if item.status == "created":
        return
    if item.status == "needs_review":
        raise AssistantError(409, "conflict", f"The item is pending review and cannot be {verb}: approve it in the inbox first")
    raise AssistantError(409, "conflict", f"An item in status {item.status} cannot be {verb}")


def _check_applicable(item, field: str):
    """Edits and splits only touch ``created`` items; ``void`` also accepts ``needs_review`` (a rejection)."""
    if field == "void":
        if item.status not in LEDGER_STATUSES:
            raise AssistantError(409, "conflict", f"An item in status {item.status} cannot be voided")
    else:
        _require_created(item, "split" if field == "split" else "edited")


def _already_effective(item, proposal) -> bool:
    """Whether a reclaimed proposal's work already reached the item (the worker died before the final commit)."""
    if proposal.field == "void":
        return item.status in {"voided", "rejected"}
    if proposal.field == "split":
        return item.status == "voided"
    return current_value(item, proposal.field) == proposal.new_value


async def _apply_edit(processor, item, proposal, principal):
    value = await actions.canonical_category(processor, proposal.new_value) if proposal.field == "category" else proposal.new_value
    await processor.review(item, "edit", {proposal.field: value}, principal=principal)


async def _apply_void(processor, item, principal):
    # An item that never reached the ledger is not voided but rejected.
    pending = item.status == "needs_review" and not item.firefly_transaction_id
    await processor.review(item, "reject" if pending else "void", principal=principal)


async def _apply_split(processor, item, proposal, principal):
    if item.amount is None or not item.asset_account or not item.merchant:
        raise AssistantError(409, "conflict", "The item is incomplete and cannot be split")
    parts = proposal.new_value
    if sum((Decimal(part["amount"]) for part in parts), Decimal(0)) != item.amount:
        raise AssistantError(409, "conflict", "The item amount changed; the split no longer adds up")
    canonical = [await actions.canonical_category(processor, part["category"]) for part in parts]
    # Children first (idempotent by client_ref) so a failure never loses the original; then void it.
    for number, (part, category) in enumerate(zip(parts, canonical, strict=True), 1):
        payload = _manual_copy(item, f"split:{proposal.id}:{number}", Decimal(part["amount"]), part.get("description"), category)
        await _manual_once(processor, payload, f"Split part {number}")
    await processor.review(item, "void", principal=principal)


async def _claim(session, proposal_id: int) -> bool:
    """Atomically move a proposal to ``applying``; False when another request holds it or it is not claimable.

    While ``applying``, ``applied_at`` holds the claim time (the stale-claim clock); it becomes the real
    application time only when all the work is done.
    """
    now = now_utc()
    result = await session.execute(
        update(Proposal)
        .where(
            Proposal.id == proposal_id,
            or_(Proposal.status.in_(("proposed", "failed")), and_(Proposal.status == "applying", Proposal.applied_at < now - CLAIM_TIMEOUT)),
        )
        .values(status="applying", applied_at=now, error=None)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount == 1


async def _apply_one(processor, proposal_id: int, principal, *, resumed: bool):
    session = processor.session
    proposal = await session.get(Proposal, proposal_id, populate_existing=True)
    result = None
    if proposal.field in actions.FIELDS:
        result = await actions.apply(session, processor, proposal, principal, resumed=resumed)
    else:
        item = await session.get(InboxItem, proposal.target_id, populate_existing=True)
        if item is None:
            raise AssistantError(404, "not_found", "Inbox item not found")
        if not (resumed and _already_effective(item, proposal)):
            _check_applicable(item, proposal.field)
            if current_value(item, proposal.field) != proposal.old_value:
                raise AssistantError(409, "conflict", "The item changed after the proposal was created; the proposal is stale")
            if proposal.field == "void":
                await _apply_void(processor, item, principal)
            elif proposal.field == "split":
                await _apply_split(processor, item, proposal, principal)
            else:
                await _apply_edit(processor, item, proposal, principal)
    proposal = await session.get(Proposal, proposal_id, populate_existing=True)
    proposal.status, proposal.error, proposal.applied_at, proposal.result = "applied", None, now_utc(), result
    await session.commit()


async def _applied_under(session, batch_id: int, key: str) -> list[int]:
    """Ids earlier ``apply`` calls on this batch applied under the same ``Idempotency-Key`` (kept in the audit detail)."""
    query = select(Audit).where(Audit.action == "assistant.apply", Audit.target_type == "batch", Audit.target_id == str(batch_id)).order_by(Audit.id)
    details = [row.detail or {} for row in await session.scalars(query)]
    return [pid for detail in details if detail.get("idempotency_key") == key for pid in detail.get("applied", [])]


async def apply(session, processor, batch_id: int, *, approved_ids: list[int], discard_others: bool, principal, idempotency_key: str | None = None):
    """Apply the approved proposals through the normal review path.

    Returns ``(applied, failed)``: ids applied by this call and ``[{"id", "detail"}]`` for the rest.
    Each proposal is claimed with an atomic ``proposed|failed -> applying`` update and becomes ``applied``
    only after all its work finished, so concurrent or repeated requests never run it twice. Proposals
    that are already applied are ignored, which makes retries safe. A retry with the same ``idempotency_key``
    also reports what the earlier calls under that key applied (as long as it is still applied).

    Phases run in order (creations, renames and limits, everything else, deletions) and proposals run one at a
    time on purpose: every one writes Firefly and then the single ``AsyncSession`` (claim, ledger change, audit,
    commit), and the ledger/category services interleave both, so running them concurrently would share that
    session between tasks. Firefly latency is what a batch pays, and the app waits 180 s for it.
    """
    batch = await get_batch(session, batch_id)
    prior = await _applied_under(session, batch_id, idempotency_key) if idempotency_key else []
    ids = list(dict.fromkeys(approved_ids))
    if not ids and not discard_others:
        raise AssistantError(422, "validation_error", "approved_ids is empty")
    known = {proposal.id: proposal for proposal in await proposals_of(session, batch.id)}
    missing = [pid for pid in ids if pid not in known]
    if missing:
        raise AssistantError(422, "validation_error", f"Proposals not in this batch: {missing}")
    if len(ids) > 1 and any(known[pid].risky for pid in ids):
        raise AssistantError(422, "risky_requires_single", "A risky proposal (amount, void, split, type, accounts, date or a deletion) must be applied on its own")
    # Stable: what others name (a new category or budget) exists before they run; deletions go last.
    ids.sort(key=lambda pid: actions.rank(known[pid].field))
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
        resumed = proposal.status == "applying"
        if not await _claim(session, pid):
            status = (await session.get(Proposal, pid, populate_existing=True)).status
            if status != "applied":
                failed.append({"id": pid, "detail": "Proposal is being applied by another request" if status == "applying" else f"Proposal is {status}"})
            continue
        try:
            await _apply_one(processor, pid, principal, resumed=resumed)
        except Exception as exc:
            logger.warning("assistant_apply_failed", proposal_id=pid, error_type=type(exc).__name__)
            detail = _public_error(exc)
            await _fail(session, Proposal, pid, status="failed", error=detail, applied_at=None)
            failed.append({"id": pid, "detail": detail})
        else:
            applied.append(pid)
    batch = await _lock_batch(session, batch_id)
    proposals = await proposals_of(session, batch_id)
    discarded = []
    if discard_others:
        for proposal in proposals:
            if proposal.id not in ids and proposal.status in {"proposed", "failed"}:
                proposal.status = "discarded"
                discarded.append(proposal.id)
    statuses = {proposal.status for proposal in proposals}
    first = min((proposal.applied_at for proposal in proposals if proposal.status == "applied"), default=None)
    if first is not None and batch.applied_at is None:
        batch.applied_at = first  # the revert deadline counts from the first application
    if "applied" in statuses:
        batch.status = "partially_applied" if statuses & {"proposed", "failed", "applying"} else "applied"
    elif statuses == {"discarded"}:
        batch.status = "discarded"
    detail = {"applied": applied, "failed": [entry["id"] for entry in failed], "discarded": discarded}
    if idempotency_key:
        detail["idempotency_key"] = idempotency_key
    await audit.record(session, action="assistant.apply", target_type="batch", target_id=batch.id, detail=detail, principal=principal)
    live = {proposal.id for proposal in proposals if proposal.status == "applied"}
    await session.commit()
    return [pid for pid in dict.fromkeys([*prior, *applied]) if pid in live], failed


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
    if item.status in {"voided", "rejected"}:
        old = item.status
        item.status = "needs_review"
        session.add(InboxEvent(inbox_id=item.id, action="reverted_void"))
        await audit.record(session, action="inbox.restore", target_type="inbox_item", target_id=item.id, detail={"changes": {"status": {"old": old, "new": "needs_review"}}}, principal=principal)


async def _revert_one(processor, proposal, principal):
    session = processor.session
    if proposal.field in actions.FIELDS:
        await actions.revert(session, processor, proposal, principal)
    else:
        await _revert_item_change(processor, proposal, principal)
    proposal.status, proposal.error, proposal.reverted_at = "reverted", None, now_utc()
    await session.commit()


async def _revert_item_change(processor, proposal, principal):
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


async def revert(session, processor, batch_id: int, *, principal):
    """Restore the values saved by ``apply`` (newest first), each within 7 days of its own ``applied_at``.

    Returns ``(reverted, failed)`` like ``apply``; proposals past their window are reported in ``failed``.
    Reverting a reverted batch is a no-op.
    """
    batch = await get_batch(session, batch_id)
    if batch.status == "reverted":
        return [], []
    applied = [proposal for proposal in await proposals_of(session, batch.id) if proposal.status == "applied" and actions.revertible(proposal)]
    if not applied:
        raise AssistantError(409, "conflict", "The batch has nothing to revert")
    now = now_utc()
    eligible = [proposal for proposal in applied if now <= proposal.applied_at + REVERT_WINDOW]
    if not eligible:
        raise AssistantError(409, "revert_expired", "The 7-day revert window has passed")
    reverted = []
    failed = [{"id": proposal.id, "detail": "The 7-day revert window has passed"} for proposal in applied if proposal not in eligible]
    for pid in [p.id for p in sorted(eligible, key=lambda p: (p.applied_at, p.id), reverse=True)]:
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
    batch = await _lock_batch(session, batch_id)
    if not any(proposal.status == "applied" and actions.revertible(proposal) for proposal in await proposals_of(session, batch_id)):
        batch.status = "reverted"
    await audit.record(
        session, action="assistant.revert", target_type="batch", target_id=batch.id,
        detail={"reverted": reverted, "failed": [entry["id"] for entry in failed]}, principal=principal,
    )
    await session.commit()
    return reverted, failed
