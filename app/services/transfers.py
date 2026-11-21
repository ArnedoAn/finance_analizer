"""Detect and confirm transfers between own accounts (Phase 5 A6, contract section 7).

A transfer between two own accounts is announced twice: a bank notifies the outgoing leg
(``withdrawal``) and the other bank the incoming leg (``deposit``). Left alone the ledger
counts the money as an expense plus an income. ``pending`` finds the probable pairs and
``pair`` merges a confirmed pair into one ledger transfer.
"""
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select

from app.db.models import InboxEvent, InboxItem

OPEN_STATUSES = ("created", "needs_review")


async def _candidates(session):
    rows = (await session.scalars(select(InboxItem).where(
        InboxItem.status.in_(OPEN_STATUSES), InboxItem.transaction_type.in_(("withdrawal", "deposit")),
        InboxItem.amount.is_not(None), InboxItem.asset_account.is_not(None),
    ))).all()
    return list(rows)


async def pending(session, settings) -> list[dict]:
    """Probable pairs: ``created``/``needs_review`` withdrawal + deposit of the same amount and
    currency in different own accounts, at most ``TRANSFER_PAIR_WINDOW_MINUTES`` apart.

    Each item appears in at most one pair; the closest pairs in time win. Items that are already
    ``transfer`` never qualify, so a paired transfer disappears from this list."""
    window = timedelta(minutes=settings.transfer_pair_window_minutes)
    groups = defaultdict(lambda: ([], []))
    for item in await _candidates(session):
        groups[(item.amount, item.currency)][0 if item.transaction_type == "withdrawal" else 1].append(item)
    found = []
    for outs, ins in groups.values():
        for out in outs:
            for incoming in ins:
                gap = abs(incoming.occurred_at - out.occurred_at)
                if out.asset_account.casefold() != incoming.asset_account.casefold() and gap <= window:
                    found.append((gap, out.id, incoming.id, out, incoming))
    found.sort(key=lambda pair: pair[:3])
    used, result = set(), []
    for gap, _, _, out, incoming in found:
        if out.id in used or incoming.id in used:
            continue
        used.update((out.id, incoming.id))
        result.append({"out_item": out, "in_item": incoming, "amount": out.amount, "currency": out.currency, "minutes_apart": round(gap.total_seconds() / 60, 1)})
    result.sort(key=lambda pair: (pair["out_item"].occurred_at, pair["out_item"].id))
    return result


async def pair(service, out_id: int, in_id: int):
    """Turn a confirmed outgoing/incoming pair into one transfer and return ``(out_item, in_item)``.

    1. The outgoing item becomes ``transaction_type="transfer"`` with ``destination_account`` set
       to the incoming item's own account, through ``Processor.review``: ``edit`` when it is
       already ``created`` (the Firefly transaction is updated in place) or ``approve`` when it
       is ``needs_review`` (the transfer is created in Firefly).
    2. The incoming item is marked ``duplicate`` with ``duplicate_of_id`` = outgoing item. If it
       was ``created`` in Firefly its transaction is voided first, so the money counts once.

    The outgoing item is converted first: if voiding the incoming leg then fails, the outgoing
    transfer is already correct and repeating the request only retries the void.

    Raises LookupError (unknown item) or ValueError (pair not valid); Firefly failures propagate.
    """
    session = service.session
    out = await session.get(InboxItem, out_id, populate_existing=True)
    incoming = await session.get(InboxItem, in_id, populate_existing=True)
    if out is None or incoming is None:
        raise LookupError("Inbox item not found")
    if incoming.status not in OPEN_STATUSES or out.status not in OPEN_STATUSES:
        raise ValueError("Both items must be created or needs_review")
    if incoming.transaction_type != "deposit" or not incoming.asset_account:
        raise ValueError("in_id must be a deposit in an own account")
    if (out.amount, out.currency) != (incoming.amount, incoming.currency):
        raise ValueError("Amount and currency must match")
    if not out.asset_account or out.asset_account.casefold() == incoming.asset_account.casefold():
        raise ValueError("Transfers need two different own accounts")
    resumed = out.transaction_type == "transfer" and (out.destination_account or "").casefold() == incoming.asset_account.casefold()
    if out.transaction_type != "withdrawal" and not resumed:
        raise ValueError("out_id must be a withdrawal")
    if not resumed:
        corrections = {"transaction_type": "transfer", "destination_account": incoming.asset_account}
        out = await service.review(out, "edit" if out.status == "created" else "approve", corrections) or out
    if incoming.status == "created":
        await service.review(incoming, "void")
    incoming.status, incoming.duplicate_of_id = "duplicate", out.id
    incoming.review_reason = None
    session.add(InboxEvent(inbox_id=incoming.id, action="paired", detail=f"transfer pair with {out.id}"))
    await session.commit()
    return out, incoming
