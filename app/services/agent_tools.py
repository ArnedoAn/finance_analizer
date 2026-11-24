"""Tools of the long-task agent (``app.services.agent``): read-only views of the existing services plus ONE write tool.

Scope is fixed by the server, never by the model: every read tool is clamped to the task window (``ToolContext.first`` ..
``last``), takes no dates, and returns bounded, REDACTED text (third-party text such as merchants, descriptions and bank
statement lines is data, not instructions). The only write tool, ``propose_change``, adds a proposal to the task's
assistant batch (``proposal_batches`` / ``proposals``); it never touches the ledger, only for movements the agent has
already read in this task, only inside the window, at most ``assistant.MAX_PROPOSALS`` per batch.

Every tool returns ``(data, summary)``: ``data`` goes back to the model, ``summary`` (short, Spanish) is what the task stores.
``ToolError`` is a recoverable mistake of the model (bad argument, unknown id): the loop reports it as a failed step.
"""
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.core.timezone import now_utc
from app.db.models import AgentTask, InboxItem
from app.db.models_rules import Rule
from app.security.redaction import redact_identifiers
from app.services import (
    ai_features,
    anomalies,
    assistant,
    assistant_errors,
    budget_status,
    counterparties,
    own_accounts,
    reports,
    snapshots,
    statements,
)

MAX_RESULT_CHARS = 6000
MAX_PAGE = 50
GAP_DAYS = 3
RULE_LIMIT = 100


class ToolError(Exception):
    """A mistake of the model's call (the step fails, the task goes on)."""


@dataclass
class AgentActor:
    """The principal recorded in the audit for what the agent does."""

    name: str = "agent"
    device_id: int | None = None


@dataclass
class ToolContext:
    session: object
    firefly: object
    settings: object
    task: AgentTask
    first: date
    last: date
    batch_title: str
    actor: AgentActor = field(default_factory=AgentActor)
    seen: set[int] = field(default_factory=set)
    proposed: set[tuple[int, str]] = field(default_factory=set)
    proposals: int = 0

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.settings.timezone)


# --- argument helpers -------------------------------------------------------------------------------------------------


def _int(args: dict, name: str, default: int, low: int, high: int) -> int:
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ToolError(f"{name} debe ser un número entero.")
    try:
        number = int(value)
    except ValueError:
        raise ToolError(f"{name} debe ser un número entero.") from None
    return max(low, min(high, number))


def _text(args: dict, name: str, limit: int = 100) -> str | None:
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError(f"{name} debe ser texto.")
    return " ".join(value.split())[:limit] or None


def _money(value) -> str:
    return f"{Decimal(value):.2f}"


def _clean(value, limit: int = 120) -> str | None:
    return ai_features.clean(value, limit)


def _fit(data: dict) -> dict:
    """Shrink the longest list of ``data`` until its JSON fits ``MAX_RESULT_CHARS`` (marks ``truncated``)."""
    data = dict(data)
    while len(json.dumps(data, ensure_ascii=False, default=str)) > MAX_RESULT_CHARS:
        key = max((k for k, v in data.items() if isinstance(v, list) and len(v) > 1), key=lambda k: len(json.dumps(data[k], default=str)), default=None)
        if key is None:
            break
        data[key] = data[key][: max(1, len(data[key]) // 2)]
        data["truncated"] = True
    return data


def _window(ctx: ToolContext) -> tuple[datetime, datetime]:
    return ctx.task.window_from, ctx.task.window_to


def _row(item: InboxItem, zone: ZoneInfo) -> dict:
    return {
        "id": item.id, "date": item.occurred_at.astimezone(zone).date().isoformat(),
        "amount": _money(item.amount) if item.amount is not None else None, "currency": item.currency,
        "merchant": _clean(item.merchant, 80), "category": item.category, "own_account": item.asset_account,
        "type": item.transaction_type, "source": item.source, "tags": list(item.tags or []),
        "description": _clean(item.description, 80), "status": item.status,
    }


def _remember(ctx: ToolContext, ids) -> None:
    ctx.seen.update(int(i) for i in ids if i is not None)


# --- read tools -------------------------------------------------------------------------------------------------------


async def list_inbox(ctx: ToolContext, args: dict):
    status = _text(args, "status", 20) or "needs_review"
    if status not in {"needs_review", "failed", "pending"}:
        raise ToolError("status debe ser needs_review, failed o pending.")
    limit, offset = _int(args, "limit", 30, 1, MAX_PAGE), _int(args, "offset", 0, 0, 100000)
    begin, end = _window(ctx)
    where = (InboxItem.status == status, InboxItem.occurred_at >= begin, InboxItem.occurred_at <= end)
    total = await ctx.session.scalar(select(func.count()).select_from(InboxItem).where(*where))
    rows = (await ctx.session.scalars(select(InboxItem).where(*where).order_by(InboxItem.occurred_at, InboxItem.id).limit(limit).offset(offset))).all()
    items = [{**_row(r, ctx.zone), "review_reason": _clean(r.review_reason, 120)} for r in rows]
    _remember(ctx, [r.id for r in rows])
    return {"status": status, "total": total, "items": items}, f"{total} movimientos en «{status}» (se leyeron {len(items)})."


async def list_ledger(ctx: ToolContext, args: dict):
    limit, offset = _int(args, "limit", 30, 1, MAX_PAGE), _int(args, "offset", 0, 0, 100000)
    category, merchant = _text(args, "category", 100), _text(args, "merchant", 100)
    begin, end = _window(ctx)
    where = [InboxItem.status == "created", InboxItem.occurred_at >= begin, InboxItem.occurred_at <= end]
    if category:
        if category.casefold() in {"none", "sin categoría", "sin categoria"}:
            where.append(InboxItem.category.is_(None) | (InboxItem.category == ""))
        else:
            where.append(func.lower(InboxItem.category) == category.casefold())
    if merchant:
        where.append(func.lower(InboxItem.merchant).contains(merchant.casefold(), autoescape=True))
    minimum = args.get("min_amount")
    if minimum is not None:
        try:
            where.append(InboxItem.amount >= Decimal(str(minimum)))
        except Exception:
            raise ToolError("min_amount debe ser un número.") from None
    total = await ctx.session.scalar(select(func.count()).select_from(InboxItem).where(*where))
    rows = (await ctx.session.scalars(select(InboxItem).where(*where).order_by(InboxItem.occurred_at, InboxItem.id).limit(limit).offset(offset))).all()
    _remember(ctx, [r.id for r in rows])
    return {"total": total, "items": [_row(r, ctx.zone) for r in rows]}, f"{total} movimientos del libro (se leyeron {len(rows)})."


async def ledger_summary(ctx: ToolContext, args: dict):
    group = _text(args, "group_by", 20) or "category"
    if group not in {"category", "merchant", "account"}:
        raise ToolError("group_by debe ser category, merchant o account.")
    rows = await reports._ledger(ctx.session, ctx.settings, ctx.first, ctx.last, None, reports.SPENDING + reports.INCOME, cop_only=True)
    spent = income = Decimal(0)
    groups: dict[str, list] = defaultdict(lambda: [0, Decimal(0)])
    for row in rows:
        if row.transaction_type in reports.INCOME:
            income += row.amount
            continue
        spent += row.amount
        name = {"category": row.category, "merchant": row.merchant, "account": row.asset_account}[group] or "(sin dato)"
        groups[_clean(name, 80) or "(sin dato)"][0] += 1
        groups[_clean(name, 80) or "(sin dato)"][1] += row.amount
    top = sorted(groups.items(), key=lambda kv: (-kv[1][1], kv[0]))[:25]
    data = {
        "currency": "COP", "from": ctx.first.isoformat(), "to": ctx.last.isoformat(), "spent": _money(spent), "income": _money(income),
        "balance": _money(income - spent), "group_by": group, "groups": [{"name": n, "count": c, "total": _money(t)} for n, (c, t) in top],
    }
    return data, f"Gasto {_money(spent)}, ingresos {_money(income)} COP; {len(groups)} grupos por {group}."


async def list_rules(ctx: ToolContext, args: dict):
    rows = (await ctx.session.scalars(select(Rule).where(Rule.active.is_(True)).order_by(Rule.hits.desc(), Rule.id).limit(RULE_LIMIT))).all()
    items = [{"match": r.match, "pattern": _clean(r.pattern, 80), "category": r.category, "source": r.source, "hits": r.hits} for r in rows]
    return {"items": items}, f"{len(items)} reglas activas."


async def get_budgets(ctx: ToolContext, args: dict):
    month = snapshots.month_of(ctx.last)
    try:
        data = await budget_status.status(ctx.firefly, month, ctx.zone)
    except Exception:
        return {"available": False}, "Firefly no respondió: sin presupuestos."
    return data, f"{len(data['budgets'])} presupuestos de {month}; estado total {data['totals']['state']}."


async def list_own_accounts(ctx: ToolContext, args: dict):
    items = [{"name": a.firefly_account, "bank": a.bank, "kind": a.kind} for a in await own_accounts.load(ctx.session)]
    return {"items": items}, f"{len(items)} cuentas propias."


async def get_anomalies(ctx: ToolContext, args: dict):
    begin, end = _window(ctx)
    found = [e for e in await anomalies.detect(ctx.session, ctx.settings, since=begin) if e["occurred_at"] <= end.isoformat()]
    items = [
        {"kind": e["kind"], "item_id": e["item_id"], "merchant": _clean(e["merchant"], 80), "category": e["category"], "amount": e["amount"],
         "currency": e["currency"], "date": e["occurred_at"][:10], "detail": _clean(e["detail"], 200)}
        for e in found[:40]
    ]
    _remember(ctx, [e["item_id"] for e in found[:40]])
    return {"total": len(found), "items": items}, f"{len(found)} anomalías en el periodo."


def _overlaps(head: dict, first: date, last: date) -> bool:
    start, end = head["period"]["from"], head["period"]["to"]
    if not start or not end:
        return False
    return start <= last.isoformat() and end >= first.isoformat()


async def list_statements(ctx: ToolContext, args: dict):
    heads, _ = await statements.listing(ctx.session, limit=100, offset=0)
    items = [
        {"id": h["id"], "account": h["account"]["name"], "period": h["period"], "status": h["status"], "rows": h["rows_count"], "result": h["result"]}
        for h in heads if _overlaps(h, ctx.first, ctx.last)
    ]
    return {"items": items}, f"{len(items)} extractos subidos que tocan el periodo."


async def get_statement(ctx: ToolContext, args: dict):
    ident = _int(args, "statement_id", 0, 0, 10**9)
    try:
        statement = await statements.get(ctx.session, ident)
    except statements.StatementError:
        raise ToolError("Ese extracto no existe.") from None
    body = await statements.detail(ctx.session, statement)

    def line(row: dict) -> dict:
        return {k: (_clean(v, 100) if k.endswith("description") else v) for k, v in row.items()}

    result = body["result"]
    data = {
        "id": statement.id, "account": body["account"]["name"], "period": body["period"], "status": body["status"],
        "matched": result["matched"], "imported": result["imported"],
        "missing": [line(r) for r in result["missing"][:30]], "mismatched": [line(r) for r in result["mismatched"][:30]],
        "extra": [line(r) for r in result["extra"][:30]] if isinstance(result["extra"], list) else [],
        "counts": {"missing": len(result["missing"]), "mismatched": len(result["mismatched"]), "extra": len(result["extra"])},
    }
    return data, f"Extracto {statement.id}: {data['counts']['missing']} faltan en el libro, {data['counts']['mismatched']} con monto distinto, {data['counts']['extra']} de más."


async def list_bills(ctx: ToolContext, args: dict):
    try:
        bills = await ctx.firefly.bills_status(ctx.first, ctx.last)
    except Exception:
        return {"available": False}, "Firefly no respondió: sin facturas."
    items = []
    for bill in bills:
        expected, paid = len(bill["pay_dates"]), len(bill["paid_dates"])
        items.append({
            "name": _clean(bill["name"], 80), "amount_min": _money(bill["amount_min"]), "amount_max": _money(bill["amount_max"]),
            "currency": bill["currency"], "repeat": bill["repeat_freq"], "expected": bill["pay_dates"], "paid": bill["paid_dates"],
            "unpaid": max(0, expected - paid),
        })
    unpaid = [i for i in items if i["unpaid"]]
    return {"items": items, "unpaid": [i["name"] for i in unpaid]}, f"{len(items)} facturas; {len(unpaid)} con pagos pendientes en el periodo."


async def counterparty_duplicates(ctx: ToolContext, args: dict):
    groups = await counterparties.suggestions(ctx.session, ctx.firefly, None)
    items = [
        {"confidence": g["confidence"], "reason": g["reason"],
         "target": redact_identifiers(g["target"]["name"])[:80],
         "sources": [{"name": redact_identifiers(s["name"])[:80], "transactions": s["transactions"]} for s in g["sources"]]}
        for g in groups[:20]
    ]
    return {"total": len(groups), "items": items}, f"{len(groups)} grupos de contrapartes que parecen duplicadas."


async def capture_gaps(ctx: ToolContext, args: dict):
    begin, end = _window(ctx)
    today = now_utc().astimezone(ctx.zone).date()
    last = min(ctx.last, today)
    stamps = (await ctx.session.scalars(select(InboxItem.occurred_at).where(InboxItem.occurred_at >= begin, InboxItem.occurred_at <= end))).all()
    days = {stamp.astimezone(ctx.zone).date() for stamp in stamps}
    gaps, run_start, cursor = [], None, ctx.first
    while cursor <= last + timedelta(days=1):
        empty = cursor <= last and cursor not in days
        if empty and run_start is None:
            run_start = cursor
        if not empty and run_start is not None:
            length = (cursor - run_start).days
            if length >= GAP_DAYS:
                gaps.append({"from": run_start.isoformat(), "to": (cursor - timedelta(days=1)).isoformat(), "days": length})
            run_start = None
        cursor += timedelta(days=1)
    before = begin - timedelta(days=60)
    silent = []
    for account in await own_accounts.load(ctx.session):
        inside = await ctx.session.scalar(select(func.count()).select_from(InboxItem).where(InboxItem.asset_account == account.firefly_account, InboxItem.occurred_at >= begin, InboxItem.occurred_at <= end))
        earlier = await ctx.session.scalar(select(func.count()).select_from(InboxItem).where(InboxItem.asset_account == account.firefly_account, InboxItem.occurred_at >= before, InboxItem.occurred_at < begin))
        if not inside and earlier:
            silent.append(account.firefly_account)
    return {"gaps": gaps, "accounts_without_movements": silent}, f"{len(gaps)} huecos de {GAP_DAYS}+ días sin movimientos; {len(silent)} cuentas sin movimientos."


# --- the write tool ---------------------------------------------------------------------------------------------------


async def _ensure_batch(ctx: ToolContext):
    if ctx.task.batch_id is not None:
        return await assistant.get_batch(ctx.session, ctx.task.batch_id)
    batch = await assistant.create_batch(
        ctx.session, window_from=ctx.task.window_from, window_to=ctx.task.window_to, instruction=ctx.batch_title, principal=ctx.actor,
    )
    task = await ctx.session.get(AgentTask, ctx.task.id, populate_existing=True)
    task.batch_id = batch.id
    await ctx.session.commit()
    ctx.task.batch_id = batch.id
    return batch


async def propose_change(ctx: ToolContext, args: dict):
    """Add one proposal to the task's batch. The owner reviews and applies the batch; nothing is written to the ledger."""
    target = args.get("target_id")
    if isinstance(target, bool) or not isinstance(target, int):
        raise ToolError("target_id debe ser el id numérico de un movimiento.")
    name = _text(args, "field", 30)
    if name not in assistant.ALLOWED_FIELDS:
        raise ToolError("field debe ser uno de: " + ", ".join(sorted(assistant.ALLOWED_FIELDS)) + ".")
    reason = _clean(args.get("reason"), 300)
    if not reason:
        raise ToolError("reason es obligatorio (máximo 300 caracteres).")
    if ctx.proposals >= assistant.MAX_PROPOSALS:
        raise ToolError(f"El lote ya tiene {assistant.MAX_PROPOSALS} propuestas: termina la tarea.")
    if target not in ctx.seen:
        raise ToolError("Solo puedes proponer sobre movimientos que leíste antes en esta tarea (list_inbox, list_ledger, get_anomalies).")
    if (target, name) in ctx.proposed:
        raise ToolError("Ya propusiste ese campo para ese movimiento.")
    item = await ctx.session.get(InboxItem, target, populate_existing=True)
    if item is None:
        raise ToolError("Ese movimiento no existe.")
    if not ctx.task.window_from <= item.occurred_at <= ctx.task.window_to:
        raise ToolError("Ese movimiento está fuera del periodo de la tarea.")
    if name == "void":
        if item.status not in assistant.LEDGER_STATUSES:
            raise ToolError("Ese movimiento ya no se puede anular.")
    elif item.status != "created":
        raise ToolError("Solo se pueden proponer cambios sobre movimientos ya registrados (status created).")
    value = args.get("new_value")
    if name == "category":
        known = {c.casefold(): c for c in await ctx.firefly.categories()}
        if not isinstance(value, str) or value.strip().casefold() not in known:
            raise ToolError("La categoría no existe en Firefly; usa una existente (list_rules o ledger_summary muestran las usadas).")
        value = known[value.strip().casefold()]
    elif name == "firefly_account":
        own = {a.firefly_account.casefold(): a.firefly_account for a in await own_accounts.load(ctx.session)}
        if not isinstance(value, str) or value.strip().casefold() not in own:
            raise ToolError("La cuenta debe ser una de tus cuentas propias (list_own_accounts).")
        value = own[value.strip().casefold()]
    try:
        if name != "void" and assistant.current_value(item, name) == assistant._normalize(name, value):
            raise ToolError("La propuesta no cambia nada.")
        batch = await _ensure_batch(ctx)
        proposal = await assistant.add_proposal(ctx.session, batch, target_id=target, field=name, new_value=value, reason=reason, principal=ctx.actor)
    except assistant_errors.AssistantError as exc:
        raise ToolError(exc.detail) from exc
    ctx.proposed.add((target, name))
    ctx.proposals += 1
    return (
        {"proposal_id": proposal.id, "batch_id": batch.id, "risky": proposal.risky, "proposals_in_batch": ctx.proposals},
        f"Propuesta {proposal.id}: {name} del movimiento {target}.",
    )


# --- registry ---------------------------------------------------------------------------------------------------------

READ_TOOLS = {
    "list_inbox": (list_inbox, "Movimientos de la bandeja del periodo. args: status (needs_review|failed|pending, por defecto needs_review), limit (1-50), offset."),
    "list_ledger": (list_ledger, "Movimientos ya registrados en el libro en el periodo. args opcionales: category (nombre o \"none\"), merchant (texto), min_amount, limit, offset."),
    "ledger_summary": (ledger_summary, "Totales de gasto e ingreso del periodo en COP y top de grupos. args: group_by (category|merchant|account)."),
    "list_rules": (list_rules, "Reglas de categorización activas. Sin args."),
    "get_budgets": (get_budgets, "Presupuestos de Firefly del mes del periodo: límite, gastado y proyección. Sin args."),
    "list_own_accounts": (list_own_accounts, "Cuentas propias del dueño. Sin args."),
    "get_anomalies": (get_anomalies, "Anomalías detectadas en el periodo. Sin args."),
    "list_statements": (list_statements, "Extractos subidos que tocan el periodo con su resultado de conciliación. Sin args."),
    "get_statement": (get_statement, "Detalle de conciliación de un extracto: filas que faltan, con monto distinto y de más. args: statement_id."),
    "list_bills": (list_bills, "Facturas (bills de Firefly) del periodo y cuáles están sin pagar. Sin args."),
    "counterparty_duplicates": (counterparty_duplicates, "Contrapartes que parecen duplicadas (solo lectura). Sin args."),
    "capture_gaps": (capture_gaps, "Días seguidos sin movimientos capturados y cuentas propias sin movimientos en el periodo. Sin args."),
}
WRITE_TOOLS = {
    "propose_change": (propose_change, "ÚNICA herramienta que escribe: agrega una propuesta al lote que el dueño revisará (nunca se aplica sola). "
                       "args: target_id (id leído antes), field (category|merchant|description|firefly_account|destination_account|occurred_at|amount|transaction_type|tags|void|split), "
                       "new_value, reason (por qué, máx. 300 caracteres)."),
}
TOOLS = {**READ_TOOLS, **WRITE_TOOLS}


def catalog() -> list[dict]:
    return [{"tool": name, "description": description} for name, (_, description) in TOOLS.items()]


async def run(ctx: ToolContext, name: str, args) -> tuple[dict, str]:
    """Run one tool; ``ToolError`` for an unknown tool, bad args or a refused proposal."""
    if name not in TOOLS:
        raise ToolError("Herramienta desconocida: " + str(name)[:40])
    if not isinstance(args, dict):
        raise ToolError("args debe ser un objeto JSON.")
    data, summary = await TOOLS[name][0](ctx, args)
    return _fit(data), summary


def local_bounds(first: date, last: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC instants of the start of ``first`` and the end of ``last`` in the server zone, ending no later than now."""
    begin = datetime.combine(first, time.min, zone).astimezone(UTC)
    end = datetime.combine(last + timedelta(days=1), time.min, zone).astimezone(UTC) - timedelta(microseconds=1)
    return begin, min(end, now_utc())
