"""Bank statement reconciliation (phase 9): PDF → redacted text → rows → comparison with the Firefly ledger.

Privacy: the PDF, its text and its password exist only in the memory of ``process`` (the request hands them to a
background task and they are dropped when it ends). What is stored is the extracted rows (descriptions encrypted
with the field-encryption key, like the raw inbox text) and the reconciliation result. Everything is purged after
``RETENTION_DAYS``. The text sent to the LLM goes through ``redact`` first; the model has no tools and its output
only fills rows, it never triggers an action: creating movements is a separate, explicit owner request.

Statuses: ``processing``, ``needs_password``, ``unreadable`` (no text layer), ``needs_review`` (rows extracted and
reconciled, but the arithmetic does not close or rows were discarded), ``ready`` and ``failed`` (``reason`` says why).
"""
import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.exceptions import FinanceAnalyzerError
from app.core.logging import get_logger
from app.core.timezone import now_utc
from app.db.models import Statement, StatementRow
from app.models.schemas import ManualTransaction
from app.parsers import normalize_merchant
from app.parsers.common import fold
from app.parsers.statements import ParsedBlock, parse_known_bank
from app.security.redaction import redact
from app.services import ai_budget, audit, pdf_text

logger = get_logger(__name__)

RETENTION_DAYS = 90
STALE_PROCESSING = timedelta(minutes=30)
DATE_TOLERANCE_DAYS = 3
MISMATCH_SIMILARITY = 0.6
CHUNK_CHARS = 8000
MAX_LLM_CALLS = 30
MAX_ROWS = 3000
BUDGET_KIND = "statement_extract"
LEDGER_TYPES = frozenset({"withdrawal", "deposit", "transfer"})
ASSET_TYPES = frozenset({"asset", "liability", "liabilities"})
MATCH_STATES = ("matched", "mismatched", "missing", "imported")


class StatementError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def _money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


# --- Upload ---------------------------------------------------------------------------------------------------------

async def register(session, own_account, data: bytes, principal) -> tuple[Statement, bool]:
    """Validate the upload and return ``(statement, process)``: ``process`` is False when the very same PDF is already known.

    The same PDF for the same account is idempotent, except that a statement stuck in ``needs_password``, ``failed``
    or ``unreadable`` is reprocessed (the owner retries with the password, or after fixing the cause).
    """
    if not data:
        raise StatementError(422, "empty_file", "El archivo está vacío.")
    if len(data) > pdf_text.MAX_PDF_BYTES:
        raise StatementError(413, "payload_too_large", "El PDF supera los 10 MB.")
    try:
        pdf_text.check_header(data)
        pdf_text.scan_raw(data)
    except pdf_text.PdfRejectedError as exc:
        raise StatementError(422, exc.code, exc.detail) from exc
    digest = hashlib.sha256(data).hexdigest()
    existing = await _find(session, own_account.id, digest)
    if existing is not None:
        return await _reuse(session, existing)
    statement = Statement(
        own_account_id=own_account.id, account_name=own_account.firefly_account, file_sha256=digest, status="processing",
    )
    session.add(statement)
    try:
        await session.flush()
    except IntegrityError:  # concurrent upload of the same file won the unique index
        await session.rollback()
        existing = await _find(session, own_account.id, digest)
        if existing is None:
            raise
        return await _reuse(session, existing)
    await audit.record(
        session, action="statement.upload", target_type="statement", target_id=statement.id,
        detail={"account": own_account.firefly_account, "bytes": len(data)}, principal=principal,
    )
    await session.commit()
    return statement, True


async def _find(session, own_account_id: int, digest: str) -> Statement | None:
    return await session.scalar(select(Statement).where(Statement.own_account_id == own_account_id, Statement.file_sha256 == digest))


async def _reuse(session, statement: Statement) -> tuple[Statement, bool]:
    if statement.status not in {"needs_password", "failed", "unreadable"}:
        return statement, False
    await session.execute(delete(StatementRow).where(StatementRow.statement_id == statement.id))
    statement.status, statement.reason = "processing", None
    statement.rows_count = statement.discarded_rows = statement.llm_calls = 0
    statement.totals = statement.summary = statement.extra_json = statement.reconciled_at = None
    await session.commit()
    return statement, True


# --- Processing -----------------------------------------------------------------------------------------------------

async def _finish(session, statement: Statement, status: str, reason: str | None = None) -> None:
    statement.status, statement.reason = status, reason
    await session.commit()


async def process(session, firefly, llm, statement_id: int, data: bytes, password: str | None) -> None:
    """Background pipeline. ``data`` and ``password`` live only in this call; failures end in ``failed``, never in an exception."""
    statement = await session.get(Statement, statement_id)
    if statement is None:
        return
    try:
        await _run(session, firefly, llm, statement, data, password)
    except Exception as exc:
        await session.rollback()
        logger.error("statement_failed", statement_id=statement_id, error_type=type(exc).__name__)
        statement = await session.get(Statement, statement_id)
        if statement is not None:
            await _finish(session, statement, "failed", "internal_error")


def _chunks(pages: list[str]) -> list[str]:
    """Redacted text split on line boundaries into blocks of about ``CHUNK_CHARS`` characters."""
    chunks, current, size = [], [], 0
    for page in pages:
        for line in (redact(page) or "").splitlines():
            line = line.strip()
            if not line:
                continue
            if size + len(line) > CHUNK_CHARS and current:
                chunks.append("\n".join(current))
                current, size = [], 0
            current.append(line[:1000])
            size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


async def _record_budget(session, calls: int) -> None:
    try:
        await ai_budget.record(session, BUDGET_KIND, calls, None)
    except Exception as exc:  # accounting must never fail the owner's request
        logger.warning("statement_budget_record_failed", error_type=type(exc).__name__)


async def _llm_block(session, llm, statement: Statement, chunks: list[str]) -> ParsedBlock | None:
    """Merge the model's answers for every block; ``None`` when the model is unavailable."""
    cycle_id = str(uuid4())
    rows, discarded, opening, closing, start, end = [], 0, None, None, None, None
    for chunk in chunks:
        hint = f"{start.isoformat()}/{end.isoformat() if end else ''}" if start else None
        try:
            block = await llm.statement_rows(chunk, cycle_id, hint)
        except RuntimeError:
            return None
        finally:
            statement.llm_calls += 1
            await _record_budget(session, 1)
        rows.extend(block.rows)
        discarded += block.discarded
        opening = opening if opening is not None else block.opening_balance
        closing = block.closing_balance if block.closing_balance is not None else closing
        start = start or block.period_from
        end = block.period_to or end
        if len(rows) > MAX_ROWS:
            break
    return ParsedBlock(tuple(rows), discarded, opening, closing, start, end)


def _totals(block: ParsedBlock) -> tuple[dict, str]:
    """Arithmetic check: opening + credits - debits must equal the closing balance when the statement prints both."""
    credits = sum((r.amount for r in block.rows if r.direction == "credit"), Decimal(0))
    debits = sum((r.amount for r in block.rows if r.direction == "debit"), Decimal(0))
    totals = {
        "credits": _money(credits), "debits": _money(debits), "opening_balance": _money(block.opening_balance),
        "closing_balance": _money(block.closing_balance), "expected_closing_balance": None, "difference": None,
        "balance_check": "unavailable",
    }
    if block.opening_balance is None or block.closing_balance is None:
        return totals, "ready"
    expected = block.opening_balance + credits - debits
    difference = block.closing_balance - expected
    totals.update(expected_closing_balance=_money(expected), difference=_money(difference),
                  balance_check="ok" if difference == 0 else "mismatch")
    return totals, "ready" if difference == 0 else "needs_review"


async def _run(session, firefly, llm, statement: Statement, data: bytes, password: str | None) -> None:
    try:
        pages = await asyncio.to_thread(pdf_text.extract, data, password)
    except pdf_text.PdfNeedsPasswordError:
        return await _finish(session, statement, "needs_password")
    except pdf_text.PdfRejectedError as exc:
        return await _finish(session, statement, "failed", exc.code)
    if not pages:
        return await _finish(session, statement, "unreadable", "no_text_layer")
    block = parse_known_bank(pages)
    if block is None:
        chunks = _chunks(pages)
        if len(chunks) > MAX_LLM_CALLS:
            return await _finish(session, statement, "failed", "too_long")
        block = await _llm_block(session, llm, statement, chunks)
        if block is None:
            return await _finish(session, statement, "failed", "llm_unavailable")
    if not block.rows or len(block.rows) > MAX_ROWS:
        statement.discarded_rows = block.discarded
        return await _finish(session, statement, "failed", "no_rows" if not block.rows else "too_many_rows")
    days = [row.date for row in block.rows]
    statement.period_from, statement.period_to = block.period_from or min(days), block.period_to or max(days)
    statement.rows_count, statement.discarded_rows = len(block.rows), block.discarded
    statement.totals, status = _totals(block)
    for number, row in enumerate(block.rows, start=1):
        session.add(StatementRow(
            statement_id=statement.id, row_no=number, day=row.date, description=row.description, amount=row.amount,
            direction=row.direction, balance=row.balance,
        ))
    await session.flush()
    try:
        await reconcile_statement(session, firefly, statement)
    except FinanceAnalyzerError:
        return await _finish(session, statement, "failed", "firefly_unavailable")
    except StatementError as exc:
        return await _finish(session, statement, "failed", exc.code)
    reason = None
    if status == "ready" and block.discarded:
        status = "needs_review"
    if status == "needs_review":
        reason = "totals_mismatch" if statement.totals["balance_check"] == "mismatch" else "rows_discarded"
    await _finish(session, statement, status, reason)


# --- Reconciliation -------------------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Movement:
    firefly_id: str
    day: date
    description: str
    amount: Decimal
    direction: str


def similarity(left: str, right: str) -> float:
    """0..1 closeness of two descriptions, ignoring case, accents and word order."""
    a, b = " ".join(fold(left).split()), " ".join(fold(right).split())
    if not a or not b:
        return 0.0
    tokens_a, tokens_b = set(a.split()), set(b.split())
    overlap = len(tokens_a & tokens_b) / min(len(tokens_a), len(tokens_b))
    return max(SequenceMatcher(None, a, b).ratio(), overlap)


def movements(raw: list[dict], account_id: str, zone: ZoneInfo) -> list[Movement]:
    """Firefly journals of an account as signed-by-direction movements; reconciliations and opening balances are not movements."""
    result = []
    for journal in raw:
        if journal.get("type") not in LEDGER_TYPES:
            continue
        if str(journal.get("source_id")) == account_id:
            direction = "debit"
        elif str(journal.get("destination_id")) == account_id:
            direction = "credit"
        else:
            continue
        moment = datetime.fromisoformat(str(journal["date"]).replace("Z", "+00:00"))
        moment = moment if moment.tzinfo else moment.replace(tzinfo=zone)
        result.append(Movement(
            firefly_id=str(journal["firefly_id"]), day=moment.astimezone(zone).date(), description=journal.get("description") or "",
            amount=abs(Decimal(str(journal["amount"]))).quantize(Decimal("0.01")), direction=direction,
        ))
    return result


def match(rows: list[StatementRow], ledger: list[Movement], start: date, end: date):
    """Pair statement rows with ledger movements.

    1. Exact amount, same direction, date within ±3 days. Among the candidates the closest date wins and the
       description similarity breaks ties; pairs are taken greedily in that order so one movement serves one row.
    2. What is left: same direction, date within ±3 days and a clearly similar description but another amount
       is the same movement with a wrong amount (``mismatched``).
    Returns ``(matched, mismatched, missing, extra)``; ``extra`` only holds movements inside ``[start, end]``.
    """
    index: dict[tuple[str, Decimal], list[int]] = {}
    for position, movement in enumerate(ledger):
        index.setdefault((movement.direction, movement.amount), []).append(position)
    pairs = []
    for row_position, row in enumerate(rows):
        for position in index.get((row.direction, row.amount), ()):
            gap = abs((row.day - ledger[position].day).days)
            if gap <= DATE_TOLERANCE_DAYS:
                pairs.append((gap, -similarity(row.description, ledger[position].description), row_position, position))
    matched, used_rows, used_ledger = [], set(), set()
    for _, _, row_position, position in sorted(pairs):
        if row_position not in used_rows and position not in used_ledger:
            used_rows.add(row_position)
            used_ledger.add(position)
            matched.append((rows[row_position], ledger[position]))
    near = []
    for row_position, row in enumerate(rows):
        if row_position in used_rows:
            continue
        for position, movement in enumerate(ledger):
            if position in used_ledger or movement.direction != row.direction:
                continue
            gap = abs((row.day - movement.day).days)
            score = similarity(row.description, movement.description)
            if gap <= DATE_TOLERANCE_DAYS and score >= MISMATCH_SIMILARITY:
                near.append((-score, gap, row_position, position))
    mismatched = []
    for _, _, row_position, position in sorted(near):
        if row_position not in used_rows and position not in used_ledger:
            used_rows.add(row_position)
            used_ledger.add(position)
            mismatched.append((rows[row_position], ledger[position]))
    missing = [row for position, row in enumerate(rows) if position not in used_rows]
    extra = [m for position, m in enumerate(ledger) if position not in used_ledger and start <= m.day <= end]
    return matched, mismatched, missing, extra


async def _ledger_account(firefly, name: str) -> dict:
    for account in await firefly.accounts():
        if account["type"] in ASSET_TYPES and account["name"].casefold() == name.casefold():
            return account
    raise StatementError(422, "firefly_account_missing", "Firefly no tiene una cuenta con el nombre de la cuenta propia.")


async def reconcile_statement(session, firefly, statement: Statement) -> None:
    """Compare the stored rows with the account's Firefly movements and store the verdict on the rows and the statement."""
    zone = ZoneInfo(get_settings().timezone)
    account = await _ledger_account(firefly, statement.account_name)
    margin = timedelta(days=DATE_TOLERANCE_DAYS)
    raw = await firefly.account_transactions(account["id"], statement.period_from - margin, statement.period_to + margin)
    rows = list((await session.scalars(select(StatementRow).where(StatementRow.statement_id == statement.id).order_by(StatementRow.row_no))).all())
    matched, mismatched, missing, extra = match(rows, movements(raw, str(account["id"]), zone), statement.period_from, statement.period_to)
    for row in rows:
        row.match_status = row.firefly_id = row.firefly_day = row.firefly_amount = row.firefly_description = None
    for row, movement in matched:
        row.match_status, row.firefly_id = "matched", movement.firefly_id
    for row, movement in mismatched:
        row.match_status, row.firefly_id = "mismatched", movement.firefly_id
        row.firefly_day, row.firefly_amount, row.firefly_description = movement.day, movement.amount, movement.description
    for row in missing:
        row.match_status = "missing"
    statement.extra_json = json.dumps([
        {"firefly_id": m.firefly_id, "date": m.day.isoformat(), "description": m.description, "amount": _money(m.amount), "direction": m.direction}
        for m in sorted(extra, key=lambda m: (m.day, m.firefly_id))
    ], ensure_ascii=False)
    statement.reconciled_at = now_utc()
    statement.summary = {"matched": len(matched), "missing": len(missing), "extra": len(extra), "mismatched": len(mismatched), "imported": 0}


# --- Reads ----------------------------------------------------------------------------------------------------------

def _row_out(row: StatementRow) -> dict:
    return {
        "row_id": row.id, "date": row.day.isoformat(), "description": row.description, "amount": _money(row.amount),
        "direction": row.direction, "balance": _money(row.balance),
    }


def _mismatch_out(row: StatementRow) -> dict:
    return {
        **_row_out(row), "firefly_id": row.firefly_id, "firefly_date": row.firefly_day.isoformat() if row.firefly_day else None,
        "firefly_description": row.firefly_description, "firefly_amount": _money(row.firefly_amount),
        "difference": _money(row.amount - row.firefly_amount) if row.firefly_amount is not None else None,
    }


def _head(statement: Statement) -> dict:
    return {
        "id": statement.id, "account": {"id": statement.own_account_id, "name": statement.account_name},
        "period": {"from": statement.period_from.isoformat() if statement.period_from else None,
                   "to": statement.period_to.isoformat() if statement.period_to else None},
        "status": statement.status, "reason": statement.reason, "totals": statement.totals,
        "rows_count": statement.rows_count, "discarded_rows": statement.discarded_rows,
        "created_at": statement.created_at, "reconciled_at": statement.reconciled_at,
    }


async def get(session, statement_id: int) -> Statement:
    statement = await session.get(Statement, statement_id, populate_existing=True)
    if statement is None:
        raise StatementError(404, "statement_not_found", "Extracto no encontrado.")
    return statement


async def _rows(session, statement_id: int) -> list[StatementRow]:
    query = select(StatementRow).where(StatementRow.statement_id == statement_id).order_by(StatementRow.row_no)
    return list((await session.scalars(query.execution_options(populate_existing=True))).all())


async def detail(session, statement: Statement) -> dict:
    rows = await _rows(session, statement.id)
    by_status = {state: [row for row in rows if row.match_status == state] for state in MATCH_STATES}
    extra = json.loads(statement.extra_json) if statement.extra_json else []
    return {
        **_head(statement),
        "result": {
            "matched": len(by_status["matched"]), "missing": [_row_out(row) for row in by_status["missing"]], "extra": extra,
            "mismatched": [_mismatch_out(row) for row in by_status["mismatched"]], "imported": len(by_status["imported"]),
        },
    }


async def listing(session, *, limit: int, offset: int) -> tuple[list[dict], int]:
    total = await session.scalar(select(func.count()).select_from(Statement))
    rows = await session.scalars(select(Statement).order_by(Statement.id.desc()).limit(limit).offset(offset))
    return [{**_head(statement), "result": statement.summary} for statement in rows.all()], total


# --- Import of missing rows -----------------------------------------------------------------------------------------

def _manual(statement: Statement, row: StatementRow) -> ManualTransaction:
    merchant = normalize_merchant(row.description)
    return ManualTransaction(
        client_ref=f"stmt:{statement.id}:{row.id}", amount=row.amount, description=row.description,
        type="withdrawal" if row.direction == "debit" else "deposit", account=statement.account_name,
        merchant=merchant or None, date=datetime(row.day.year, row.day.month, row.day.day, 12, 0),
    )


async def import_missing(session, processor, statement: Statement, row_ids: list[int], principal, *, confirm_unbalanced: bool) -> dict:
    """Create the chosen missing rows through ``Processor.manual`` (the ``transactions/manual`` path).

    The idempotency key is ``stmt:<statement>:<row>``, so repeating the request never duplicates a movement. Each row
    commits on its own: one Firefly failure does not undo the movements already created.
    """
    if statement.status == "needs_review" and not confirm_unbalanced:
        raise StatementError(409, "statement_needs_review", "El extracto no cuadra con sus saldos; confirma con confirm_unbalanced=true.")
    if statement.status not in {"ready", "needs_review"} or statement.reconciled_at is None:
        raise StatementError(409, "statement_not_ready", "El extracto aún no está conciliado.")
    wanted = list(dict.fromkeys(row_ids))
    rows = {row.id: row for row in await _rows(session, statement.id)}
    created, skipped, failed = [], [], []
    for row_id in wanted:
        row = rows.get(row_id)
        if row is None:
            skipped.append({"row_id": row_id, "reason": "not_found"})
        elif row.match_status == "imported":
            skipped.append({"row_id": row_id, "reason": "already_imported"})
        elif row.match_status != "missing":
            skipped.append({"row_id": row_id, "reason": "not_missing"})
        else:
            try:
                item = await processor.manual(_manual(statement, row))
            except (ValueError, FinanceAnalyzerError) as exc:
                await session.rollback()
                failed.append({"row_id": row_id, "reason": str(exc)[:200] if isinstance(exc, ValueError) else "upstream_unavailable"})
                continue
            if item.status != "created":
                await session.commit()
                # ``failed``: Firefly did not take it (calling again returns the same inbox item until it is reprocessed);
                # ``needs_review``: the pipeline held it (for example a possible duplicate), the owner decides in the inbox.
                failed.append({"row_id": row_id, "reason": "upstream_unavailable" if item.status == "failed" else f"inbox_{item.status}", "inbox_id": item.id})
                continue
            row.match_status, row.firefly_id, row.inbox_id = "imported", item.firefly_transaction_id, item.id
            await session.commit()
            created.append({"row_id": row_id, "inbox_id": item.id, "firefly_id": item.firefly_transaction_id})
    summary = dict(statement.summary or {})
    summary["missing"] = sum(1 for row in rows.values() if row.match_status == "missing")
    summary["imported"] = sum(1 for row in rows.values() if row.match_status == "imported")
    statement.summary = summary
    await audit.record(
        session, action="statement.import_missing", target_type="statement", target_id=statement.id,
        detail={"created": len(created), "skipped": len(skipped), "failed": len(failed)}, principal=principal,
    )
    await session.commit()
    return {"created": created, "skipped": skipped, "failed": failed}


# --- Deletion and retention -----------------------------------------------------------------------------------------

async def delete_statement(session, statement: Statement, principal) -> None:
    await audit.record(
        session, action="statement.delete", target_type="statement", target_id=statement.id,
        detail={"account": statement.account_name, "rows": statement.rows_count}, principal=principal,
    )
    await session.execute(delete(StatementRow).where(StatementRow.statement_id == statement.id))
    await session.delete(statement)
    await session.commit()


async def purge(session, now: datetime | None = None) -> int:
    """Delete statements older than ``RETENTION_DAYS`` with their rows and fail the ones stuck in ``processing``.

    Called from the processing cycle's retention step. Does not commit; returns the number of statements deleted.
    """
    now = now or now_utc()
    cutoff = now - timedelta(days=RETENTION_DAYS)
    old = select(Statement.id).where(Statement.created_at < cutoff)
    await session.execute(delete(StatementRow).where(StatementRow.statement_id.in_(old)))
    result = await session.execute(delete(Statement).where(Statement.created_at < cutoff))
    await session.execute(
        update(Statement).where(Statement.status == "processing", Statement.updated_at < now.astimezone(UTC) - STALE_PROCESSING)
        .values(status="failed", reason="interrupted")
    )
    return result.rowcount or 0
