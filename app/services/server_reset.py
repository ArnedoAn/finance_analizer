"""Full reset of everything about movements, to re-import from the mailbox while the system is being tested.

``POST /maintenance/reset`` (admin only). ``preview`` counts what would go (database rows, Firefly movements and accounts)
and changes nothing. ``start`` runs the real reset in the background, one at a time per process (production runs a single
uvicorn worker; a restart only loses the progress handle):

1. Firefly first, in pages and idempotent: the movements Cofre created (``external_id`` starting with ``fa:`` or ``ledger:``,
   or a split tagged ``auto`` / ``manual``, which is what ``Processor._split`` puts), then the expense/revenue accounts that
   are left EMPTY. Firefly does not say who created an account, so an account is only deleted when it holds no movement at all
   (own accounts are never touched). A movement that is already gone (404) counts as deleted, so a run that failed halfway is
   repeated safely: it just finds less to do.
2. The database, in ONE transaction (all or nothing): the inbox and everything that hangs from it, the learned rules and
   mappings unless ``keep_rules``, the anti-noise keys of the deleted alerts and the digest queue; the merchant counters go to
   zero and, with ``reset_mail_cursor``, the IMAP cursors. Kept: own accounts and sources, known senders, manual rules,
   budgets (Firefly) and their limits, assistant memories and mutes, settings (``runtime_flags`` policies), devices, the audit
   log (which gains one ``maintenance.reset`` entry), AI usage counters and scheduler history.

Afterwards the caches (dashboard snapshots, own accounts, descriptions, search) are invalidated. Nothing here commits for the
caller of ``preview``; ``start``'s task owns its transaction.
"""
import asyncio
import copy
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from uuid import uuid4

from sqlalchemy import delete, func, not_, or_, select, update

from app.core.exceptions import FireflyError, PermanentError
from app.core.logging import get_logger
from app.core.timezone import now_utc
from app.db.models import (
    AgentStep,
    AgentTask,
    AiEvent,
    AiItemSuggestion,
    AiJob,
    AiNarrative,
    AiNote,
    AiSuggestion,
    AlertQuestion,
    Bill,
    BillCandidate,
    BudgetAssignment,
    BudgetMap,
    CoachTip,
    CounterpartyMerge,
    DashboardSnapshot,
    InboxEvent,
    InboxItem,
    MailAccountState,
    MerchantCorrection,
    Notification,
    Receipt,
    ServerError,
    Statement,
    StatementRow,
)
from app.db.models_alerts import Alert
from app.db.models_assistant import Proposal, ProposalBatch
from app.db.models_flags import RuntimeFlag
from app.db.models_ledger import LedgerChange
from app.db.models_rules import Merchant, MerchantItem, Rule
from app.services import ai_search, audit, descriptions, digest, own_accounts, snapshots

logger = get_logger(__name__)

CONFIRMATION = "BORRAR MOVIMIENTOS"
ACTION = "maintenance.reset"
EXTERNAL_PREFIXES = ("fa:", "ledger:")
COFRE_TAGS = frozenset({"auto", "manual"})
ACCOUNT_TYPES = ("expense", "revenue")
DELETE_CHUNK = 50
EXAMPLES = 20
MAX_RUNS = 20
_FROM, _TO = date(2000, 1, 1), date(2100, 1, 1)  # every movement, whatever the period


@dataclass(frozen=True)
class ResetOptions:
    keep_rules: bool = False
    reset_mail_cursor: bool = False

    def public(self) -> dict:
        return {"keep_rules": self.keep_rules, "reset_mail_cursor": self.reset_mail_cursor}


class ResetBusyError(Exception):
    """A reset is already running."""


_runs: OrderedDict[str, dict] = OrderedDict()
_task: asyncio.Task | None = None


def running() -> bool:
    return _task is not None and not _task.done()


# --- database ---------------------------------------------------------------------------------------------------------


def _targets(options: ResetOptions) -> list[tuple[str, type, object | None]]:
    """``(name, model, condition)`` of every table to delete from, children before parents (foreign keys)."""
    targets: list[tuple[str, type, object | None]] = [
        ("proposals", Proposal, None),
        ("ai_suggestions", AiSuggestion, None),
        ("ai_item_suggestions", AiItemSuggestion, None),
        ("budget_assignments", BudgetAssignment, None),
        ("merchant_items", MerchantItem, None),
        ("inbox_events", InboxEvent, None),
        ("agent_steps", AgentStep, None),
        ("agent_tasks", AgentTask, None),
        ("proposal_batches", ProposalBatch, None),
        ("statement_rows", StatementRow, None),
        ("statements", Statement, None),
        ("alert_questions", AlertQuestion, None),
        ("alerts", Alert, None),
        ("receipts", Receipt, None),
        ("bills", Bill, None),
        ("bill_candidates", BillCandidate, None),
        ("ledger_changes", LedgerChange, None),
        ("counterparty_merges", CounterpartyMerge, None),
        ("coach_tips", CoachTip, None),
        ("ai_narratives", AiNarrative, None),
        ("ai_notes", AiNote, None),
        ("ai_jobs", AiJob, None),
        ("ai_events", AiEvent, None),
        ("dashboard_snapshots", DashboardSnapshot, None),
        ("server_errors", ServerError, None),
        ("inbox_items", InboxItem, None),
    ]
    if not options.keep_rules:
        targets += [
            ("rules", Rule, Rule.source == "learned"),
            ("budget_map", BudgetMap, None),
            ("merchant_corrections", MerchantCorrection, None),
        ]
    targets += [
        # The anti-noise keys of the deleted alerts (the digests' own keys stay: a digest is about time, not about items).
        ("notifications", Notification, (Notification.key.like("alert:%")) & not_(Notification.key.like("alert:digest:%"))),
        ("runtime_flags", RuntimeFlag, RuntimeFlag.key == digest.GROUPED_KEY),
    ]
    return targets


def _updates(options: ResetOptions) -> list[tuple[str, type, object, dict]]:
    """``(name, model, condition, values)`` of the rows that are reset instead of deleted."""
    updates = [("merchants", Merchant, or_(Merchant.count > 0, Merchant.last_seen_at.is_not(None)), {"count": 0, "last_seen_at": None})]
    if options.reset_mail_cursor:
        updates.append(("mail_account_state", MailAccountState, or_(MailAccountState.last_uid != 0, MailAccountState.uidvalidity.is_not(None)), {"last_uid": 0, "uidvalidity": None}))
    return updates


async def _count(session, model, condition) -> int:
    query = select(func.count()).select_from(model)
    if condition is not None:
        query = query.where(condition)
    return int(await session.scalar(query) or 0)


async def _purge_database(session, options: ResetOptions) -> tuple[dict, dict]:
    """Delete and reset in the caller's transaction. Returns ``(tables, updated)`` row counts."""
    tables, updated = {}, {}
    for name, model, condition in _targets(options):
        statement = delete(model) if condition is None else delete(model).where(condition)
        tables[name] = (await session.execute(statement.execution_options(synchronize_session=False))).rowcount or 0
    for name, model, condition, values in _updates(options):
        updated[name] = (await session.execute(update(model).where(condition).values(**values).execution_options(synchronize_session=False))).rowcount or 0
    return tables, updated


# --- Firefly ----------------------------------------------------------------------------------------------------------


def is_cofre_group(group: dict) -> bool:
    """A transaction group Cofre wrote: some split has an ``fa:``/``ledger:`` external id or the ``auto``/``manual`` tag."""
    for split in group.get("attributes", {}).get("transactions", []):
        if str(split.get("external_id") or "").startswith(EXTERNAL_PREFIXES):
            return True
        if COFRE_TAGS & {str(tag) for tag in split.get("tags") or []}:
            return True
    return False


async def find_transactions(firefly, progress: dict | None = None) -> list[str]:
    """Ids of every transaction group Cofre created, read page by page (nothing is deleted while scanning, so no page shifts)."""
    found: dict[str, None] = {}
    page, pages = 1, 1
    while page <= pages:
        groups, pages = await firefly.transaction_groups_page(page, _FROM, _TO)
        if not groups:
            break
        for group in groups:
            if is_cofre_group(group):
                found[str(group["id"])] = None
        if progress is not None:
            progress["found"] = len(found)
        page += 1
    return list(found)


async def find_accounts(firefly, protected: set[str], doomed: set[str]) -> list[dict]:
    """Expense/revenue accounts whose every movement is in ``doomed`` (none at all counts), except the owner's own accounts."""
    accounts = []
    for account in await firefly.accounts():
        if account.get("type") not in ACCOUNT_TYPES or str(account.get("name", "")).casefold() in protected:
            continue
        rows = await firefly.account_transactions(account["id"], _FROM, _TO)
        if all(str(row["firefly_id"]) in doomed for row in rows):
            accounts.append(account)
    return accounts


def _already_gone(exc: PermanentError) -> bool:
    return str(exc).startswith("Firefly 404")


async def _protected(session) -> set[str]:
    return {account.firefly_account.casefold() for account in await own_accounts.load(session)}


# --- preview ----------------------------------------------------------------------------------------------------------


async def preview(session, firefly, options: ResetOptions) -> dict:
    """What a real reset would delete, without changing anything. ``FireflyError`` when Firefly cannot answer."""
    tables = {name: await _count(session, model, condition) for name, model, condition in _targets(options)}
    updated = {name: await _count(session, model, condition) for name, model, condition, _ in _updates(options)}
    transactions = await find_transactions(firefly)
    accounts = await find_accounts(firefly, await _protected(session), set(transactions))
    return {
        "dry_run": True, "options": options.public(), "tables": tables, "updated": updated,
        "firefly": {
            "transactions": len(transactions), "accounts": len(accounts),
            "account_examples": [{"id": str(a["id"]), "name": a["name"], "type": a["type"]} for a in accounts[:EXAMPLES]],
        },
    }


# --- real run ---------------------------------------------------------------------------------------------------------


def get(run_id: str) -> dict | None:
    state = _runs.get(run_id)
    return copy.deepcopy(state) if state is not None else None


def start(firefly, session_factory, options: ResetOptions, principal) -> str:
    """Begin a reset in the background and return its id. ``ResetBusyError`` when one is already running."""
    global _task
    if running():
        raise ResetBusyError
    run_id = uuid4().hex
    _runs[run_id] = {
        "id": run_id, "status": "running", "phase": "queued", "started_at": now_utc().isoformat(), "finished_at": None,
        "options": options.public(),
        "progress": {"firefly_transactions": {"found": 0, "deleted": 0}, "firefly_accounts": {"found": 0, "deleted": 0, "skipped": 0}},
        "result": None, "error": None,
    }
    while len(_runs) > MAX_RUNS:
        _runs.popitem(last=False)
    _task = asyncio.get_running_loop().create_task(_runner(_runs[run_id], firefly, session_factory, options, principal))
    return run_id


async def _delete_firefly(state: dict, firefly, session_factory, options: ResetOptions) -> dict:
    progress = state["progress"]
    state["phase"] = "firefly_transactions"
    ids = await find_transactions(firefly, progress["firefly_transactions"])
    for offset in range(0, len(ids), DELETE_CHUNK):
        for group_id in ids[offset:offset + DELETE_CHUNK]:
            try:
                await firefly.delete_transaction(group_id)
            except PermanentError as exc:
                if not _already_gone(exc):
                    raise
            progress["firefly_transactions"]["deleted"] += 1
    state["phase"] = "firefly_accounts"
    async with session_factory() as session:
        protected = await _protected(session)
    accounts = await find_accounts(firefly, protected, set())  # re-read: only what is empty NOW is deleted
    progress["firefly_accounts"]["found"] = len(accounts)
    skipped = []
    for account in accounts:
        try:
            await firefly.delete_account(account["id"])
            progress["firefly_accounts"]["deleted"] += 1
        except PermanentError as exc:
            if not _already_gone(exc):
                skipped.append({"id": str(account["id"]), "name": account["name"]})
                progress["firefly_accounts"]["skipped"] += 1
    return {
        "transactions": progress["firefly_transactions"]["deleted"], "accounts": progress["firefly_accounts"]["deleted"],
        "skipped_accounts": skipped,
    }


async def _runner(state: dict, firefly, session_factory, options: ResetOptions, principal) -> None:
    try:
        await snapshots.drop_pending()
        firefly_result = await _delete_firefly(state, firefly, session_factory, options)
        state["phase"] = "database"
        async with session_factory() as session:
            tables, updated = await _purge_database(session, options)
            await audit.record(
                session, action=ACTION, target_type="maintenance", target_id=state["id"], principal=principal,
                detail={"status": "done", "run_id": state["id"], **options.public(), "tables": tables, "updated": updated, "firefly": firefly_result},
            )
            await session.commit()
            session.expire_all()
        await snapshots.drop_pending()
        own_accounts.invalidate()
        descriptions.invalidate()
        ai_search.clear_cache()
        state["result"] = {"tables": tables, "updated": updated, "firefly": firefly_result}
        state["status"], state["phase"] = "done", "finished"
        logger.info("server_reset_done", run_id=state["id"], inbox_items=tables["inbox_items"], firefly_transactions=firefly_result["transactions"])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        failed_phase = state["phase"]
        logger.error("server_reset_failed", run_id=state["id"], phase=failed_phase, error_type=type(exc).__name__)
        if isinstance(exc, FireflyError):
            state["error"] = {"detail": "Firefly no respondió o rechazó un borrado; revisa y repite (es idempotente).", "code": "firefly_unavailable", "phase": failed_phase}
        else:
            state["error"] = {"detail": "El borrado falló; la base no se tocó si la fase no es «database». Repite la operación.", "code": "reset_failed", "phase": failed_phase}
        state["status"] = "failed"
        try:
            async with session_factory() as session:
                await audit.record(
                    session, action=ACTION, target_type="maintenance", target_id=state["id"], principal=principal,
                    detail={"status": "failed", "run_id": state["id"], **options.public(), "phase": failed_phase, "progress": state["progress"], "error": type(exc).__name__},
                )
                await session.commit()
        except Exception as audit_exc:
            logger.error("server_reset_audit_failed", error_type=type(audit_exc).__name__)
    finally:
        state["finished_at"] = now_utc().isoformat()
