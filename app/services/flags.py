"""Runtime policy flags (Phase 5 A7, contract section 13).

``policy_reasons`` is called by ``Processor._extract`` and again by ``Processor.process`` right before an unapproved item
is sent to Firefly (items already extracted that retry); it returns extra review reasons;
while auto-approvals are paused after a ``suspicious`` report every non-manual item
must go to human review. Functions never commit; callers own the transaction.
"""
from sqlalchemy import select

from app.core.timezone import now_utc
from app.db.models_flags import RuntimeFlag
from app.services import suggestions

AUTO_APPROVE_PAUSED = "auto_approve_paused"
PAUSED_REASON = "auto-approval paused after suspicious report"


async def _state(session) -> dict:
    value = await session.scalar(select(RuntimeFlag.value).where(RuntimeFlag.key == AUTO_APPROVE_PAUSED))
    return value if value and value.get("paused") else {"paused": False}


async def _write(session, value):
    row = await session.get(RuntimeFlag, AUTO_APPROVE_PAUSED, populate_existing=True)
    if row is None:
        session.add(RuntimeFlag(key=AUTO_APPROVE_PAUSED, value=value))
    else:
        row.value = value
    await session.flush()


async def policy_reasons(session, item) -> list[str]:
    if item.source == "manual":
        return []
    return [PAUSED_REASON] if (await _state(session))["paused"] else []


async def status(session) -> dict:
    state = await _state(session)
    return {
        "auto_approve_paused": state["paused"],
        "paused_at": state.get("paused_at"),
        "paused_reason": state.get("reason"),
    }


async def mark_suspicious(session, item, principal=None) -> None:
    """Pause auto-approvals, emit the ``suspicious`` alert and audit the report."""
    from app.services import alerts, audit

    current = await _state(session)
    if not current["paused"]:
        await _write(session, {
            "paused": True,
            "paused_at": now_utc().isoformat(),
            "reason": f"Cobro no reconocido (elemento {item.id})",
        })
    amount = f" por ${suggestions.format_amount(item.amount)}" if item.amount is not None else ""
    merchant = f" de {item.merchant}" if item.merchant else ""
    await alerts.emit(
        session, "suspicious",
        title="Cobro no reconocido",
        body=f"Marcaste como no reconocido un cobro{merchant}{amount}. Las aprobaciones automáticas quedan en pausa hasta que las reactives.",
        item_id=item.id, amount=item.amount, currency=item.currency,
        dedupe_key=f"suspicious:{item.id}",
    )
    await audit.record(
        session, action="flags.suspicious", target_type="inbox_item", target_id=item.id,
        detail={"auto_approve_paused": True}, principal=principal,
    )


async def resume(session) -> None:
    await _write(session, {"paused": False})
