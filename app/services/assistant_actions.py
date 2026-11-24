"""Assistant proposals that are not an edit of an inbox item: categories, registered movements, budgets and rules.

Same flow as every proposal (the model proposes, the owner approves, ``assistant.apply`` applies and reports per
proposal), and every write reuses the service the owner's own screens use: ``categories``, ``ledger.edit`` and
``ledger.delete``, ``budget_edit`` and ``rules``. ``prepare`` validates and snapshots at proposal time; ``apply``
and ``revert`` run later. Nothing here commits proposal rows; the services commit their own work.

====================  =====================  ==========================================================
field                 target_ref             new_value
====================  =====================  ==========================================================
create_category       -                      name
rename_category       current name           new name
delete_category       name                   -
ledger_edit           Firefly transaction    {category, budget, source_account, merchant, description,
                                             date, amount, tags_add, tags_remove} (any subset)
ledger_delete         Firefly transaction    -
create_budget         -                      {name, amount?, month?}
set_budget_limit      budget name            {amount, month}
delete_budget         budget name            -
create_rule           -                      {match, pattern, category, merchant?}
delete_rule           rule id                -
====================  =====================  ==========================================================
"""
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal, NamedTuple

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)

from app.db.models_rules import Rule
from app.parsers.common import fold
from app.services import budget_edit, categories, ledger, rules
from app.services.assistant_errors import AssistantError
from app.services.budget_assign import current_month, valid_month

CREATE_CATEGORY = "create_category"
RENAME_CATEGORY = "rename_category"
DELETE_CATEGORY = "delete_category"
LEDGER_EDIT = "ledger_edit"
LEDGER_DELETE = "ledger_delete"
CREATE_BUDGET = "create_budget"
SET_BUDGET_LIMIT = "set_budget_limit"
DELETE_BUDGET = "delete_budget"
CREATE_RULE = "create_rule"
DELETE_RULE = "delete_rule"

FIELDS = frozenset({
    CREATE_CATEGORY, RENAME_CATEGORY, DELETE_CATEGORY, LEDGER_EDIT, LEDGER_DELETE,
    CREATE_BUDGET, SET_BUDGET_LIMIT, DELETE_BUDGET, CREATE_RULE, DELETE_RULE,
})
# Applied first so what later proposals name already exists; deletions last so nothing still needs what they remove.
_FIRST = frozenset({CREATE_CATEGORY, CREATE_BUDGET})
_SECOND = frozenset({RENAME_CATEGORY, SET_BUDGET_LIMIT})
_LAST = frozenset({DELETE_CATEGORY, DELETE_BUDGET, DELETE_RULE, LEDGER_DELETE})
# "Deshacer lote" restores these through the ledger's own undo; the rest has no automatic undo.
REVERTIBLE = frozenset({LEDGER_EDIT, LEDGER_DELETE})
RISKY = frozenset({DELETE_CATEGORY, LEDGER_DELETE, DELETE_BUDGET, DELETE_RULE})
RISKY_LEDGER_KEYS = frozenset({"amount", "date", "source_account"})

_Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
_Money = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
_Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]


class _LedgerValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: _Name | None = None
    budget: _Name | None = None
    source_account: _Name | None = None
    merchant: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None
    description: _Name | None = None
    date: AwareDatetime | None = None
    amount: _Money | None = None
    tags_add: list[_Tag] = Field(default_factory=list, max_length=10)
    tags_remove: list[_Tag] = Field(default_factory=list, max_length=10)


class _BudgetValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    amount: _Money | None = None
    month: str | None = None


class _LimitValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: _Money
    month: str


class _RuleValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    match: Literal["merchant_exact", "merchant_contains", "text_contains"]
    pattern: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
    category: _Name
    merchant: Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)] | None = None


class Prepared(NamedTuple):
    target_ref: str | None
    subject: str
    old_value: object
    new_value: object
    risky: bool


def rank(field: str) -> int:
    """Application order inside a batch: creations, renames and limits, everything else, deletions."""
    return 0 if field in _FIRST else 1 if field in _SECOND else 3 if field in _LAST else 2


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _invalid(detail: str) -> AssistantError:
    return AssistantError(422, "validation_error", detail)


def _parse(model, field: str, value):
    try:
        return TypeAdapter(model).validate_python(value)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise _invalid(f"Invalid new_value for {field}{' at ' + where if where else ''}: {first['msg']}") from exc


def _no_value(field: str, value) -> None:
    if value is not None:
        raise _invalid(f"{field} takes no new_value: send null")


def _target(field: str, target) -> str:
    text = str(target).strip() if isinstance(target, str | int) and not isinstance(target, bool) else ""
    if not text or len(text) > 255:
        raise _invalid(f"{field} needs a target_ref")
    return text


def _taken(proposals, field: str, key: str, getter) -> None:
    for proposal in proposals:
        if proposal.field == field and proposal.status != "discarded" and getter(proposal) == key:
            raise AssistantError(409, "conflict", "The batch already has that proposal")


async def canonical_category(processor, name: str) -> str:
    """The Firefly spelling of ``name``; a category that does not exist is a visible failure that says how to fix it."""
    found = categories.find(await processor.categories(), name)
    if found is None:
        raise AssistantError(422, "unknown_category", f"Unknown category: {name}")
    return found


# --- registered movements ---------------------------------------------------------------------------------------------------


async def _movement(session, processor, ref: str) -> dict:
    try:
        return await ledger.get(session, processor, ref)
    except ledger.LedgerError as exc:
        raise AssistantError(exc.status, exc.code, exc.detail) from exc


def _counterparty(version: dict) -> str:
    """The merchant of a withdrawal (its destination) or the payer of a deposit (its source)."""
    first = version["splits"][0]
    return first["source_account"] if version["type"] == "deposit" else first["destination_account"]


def _snapshot(version: dict, keys) -> dict:
    """The current values of ``keys`` (the keys of a ``ledger_edit`` value) in the form a proposal stores them."""
    first = version["splits"][0]
    values = {
        "category": first["category"], "budget": first["budget"], "source_account": first["source_account"], "merchant": _counterparty(version),
        "description": version["description"], "tags": list(first["tags"]),
        "date": datetime.fromisoformat(version["date"]).astimezone(UTC).isoformat(),
        "amount": _money(sum((Decimal(split["amount"]) for split in version["splits"]), Decimal(0))),
    }
    return {_slot(key): values[_slot(key)] for key in keys}


def _slot(key: str) -> str:
    return "tags" if key.startswith("tags_") else key


def _subject(version: dict) -> str:
    total = sum((Decimal(split["amount"]) for split in version["splits"]), Decimal(0))
    text = f"{version['description']} · {_money(total)} · {version['date'][:10]}"
    return text[:255]


def _in_window(version: dict, batch) -> None:
    moment = datetime.fromisoformat(version["date"])
    if not batch.window_from <= moment <= batch.window_to:
        raise AssistantError(422, "outside_window", "The movement is outside the batch window")


def _edit_value(parsed: _LedgerValue) -> dict:
    value = parsed.model_dump(exclude_none=True)
    if "date" in value:
        value["date"] = parsed.date.astimezone(UTC).isoformat()
    if "amount" in value:
        value["amount"] = _money(parsed.amount)
    value = {key: item for key, item in value.items() if item != []}
    for key in ("tags_add", "tags_remove"):
        if key in value:
            value[key] = list(dict.fromkeys(value[key]))
    return value


def _same_value(key: str, current, expected) -> bool:
    if key == "date":
        return datetime.fromisoformat(current) == datetime.fromisoformat(expected)
    return current == expected


def _matches(current: dict, value: dict) -> bool:
    """Whether the movement, as the snapshot ``current``, already has everything ``value`` (a ``ledger_edit``) asks for."""
    for key, item in value.items():
        if key == "tags_add":
            same = set(item) <= set(current["tags"])
        elif key == "tags_remove":
            same = not set(item) & set(current["tags"])
        else:
            same = _same_value(key, current[key], item)
        if not same:
            return False
    return True


# --- prepare (at proposal time) ---------------------------------------------------------------------------------------------


async def prepare(session, processor, batch, proposals, field: str, target, value) -> Prepared:
    """Validate one action proposal against Firefly and the batch, and snapshot what it changes."""
    if field == CREATE_CATEGORY:
        name = _parse(categories.CategoryName, field, value)
        _taken(proposals, field, fold(name), lambda p: fold(p.new_value))
        return Prepared(None, name, None, name, False)
    if field in {RENAME_CATEGORY, DELETE_CATEGORY}:
        current = await canonical_category(processor, _target(field, target))
        if field == DELETE_CATEGORY:
            _no_value(field, value)
            _taken(proposals, field, current, lambda p: p.target_ref)
            return Prepared(current, current, current, None, True)
        new = _parse(categories.CategoryName, field, value)
        if fold(new) == fold(current):
            raise _invalid("The new name is the same as the current one")
        clash = categories.find(await processor.categories(), new)
        if clash is not None:
            raise AssistantError(409, "conflict", f"A category named «{clash}» already exists")
        _taken(proposals, field, current, lambda p: p.target_ref)
        return Prepared(current, f"{current} → {new}", current, new, False)
    if field in {LEDGER_EDIT, LEDGER_DELETE}:
        ref = _target(field, target)
        version = await _movement(session, processor, ref)
        _in_window(version, batch)
        _taken(proposals, field, ref, lambda p: p.target_ref)
        if field == LEDGER_DELETE:
            _no_value(field, value)
            old = _snapshot(version, ("description", "amount", "date"))
            return Prepared(ref, _subject(version), old, None, True)
        parsed = _parse(_LedgerValue, field, value)
        new = _edit_value(parsed)
        if not new:
            raise _invalid("ledger_edit needs at least one change")
        if "amount" in new and len(version["splits"]) != 1:
            raise _invalid("The amount can only change on a movement with a single split")
        if "merchant" in new and version["type"] == "transfer":
            raise _invalid("A transfer has no merchant")
        old = _snapshot(version, new)
        if _matches(old, new):
            raise _invalid("The proposal changes nothing")
        return Prepared(ref, _subject(version), old, new, bool(new.keys() & RISKY_LEDGER_KEYS))
    if field in {CREATE_BUDGET, SET_BUDGET_LIMIT, DELETE_BUDGET}:
        return await _prepare_budget(processor, proposals, field, target, value)
    return await _prepare_rule(session, proposals, field, target, value)


async def _prepare_budget(processor, proposals, field: str, target, value) -> Prepared:
    firefly = processor.firefly
    if field == CREATE_BUDGET:
        parsed = _parse(_BudgetValue, field, value)
        month = parsed.month
        if parsed.amount is None and month is not None:
            raise _invalid("A month needs an amount")
        if parsed.amount is not None:
            month = month or current_month(processor.zone)
            if not valid_month(month):
                raise _invalid("The month must look like YYYY-MM")
        if await budget_edit.find(firefly, parsed.name) is not None:
            raise AssistantError(409, "conflict", f"A budget named «{parsed.name}» already exists; propose set_budget_limit")
        _taken(proposals, field, parsed.name.casefold(), lambda p: p.new_value["name"].casefold())
        new = {"name": parsed.name, **({"amount": _money(parsed.amount), "month": month} if parsed.amount is not None else {})}
        return Prepared(None, parsed.name + (f" · {new['amount']} · {month}" if "amount" in new else ""), None, new, False)
    name = _target(field, target)
    budget = await budget_edit.find(firefly, name)
    if field == DELETE_BUDGET:
        _no_value(field, value)
        if budget is None:
            raise AssistantError(422, "unknown_budget", f"Unknown budget: {name}")
        _taken(proposals, field, budget["name"], lambda p: p.target_ref)
        return Prepared(budget["name"], budget["name"], budget["name"], None, True)
    parsed = _parse(_LimitValue, field, value)
    if not valid_month(parsed.month):
        raise _invalid("The month must look like YYYY-MM")
    pending = next((p for p in proposals if p.field == CREATE_BUDGET and p.status != "discarded" and p.new_value["name"].casefold() == name.casefold()), None)
    if budget is None and pending is None:
        raise AssistantError(422, "unknown_budget", f"Unknown budget: {name}")
    label = budget["name"] if budget else pending.new_value["name"]
    limit = await budget_edit.current_limit(firefly, budget, parsed.month) if budget else None
    old = {"amount": _money(limit["amount"]), "month": parsed.month} if limit else None
    return Prepared(label, f"{label} · {parsed.month}", old, {"amount": _money(parsed.amount), "month": parsed.month}, False)


async def _prepare_rule(session, proposals, field: str, target, value) -> Prepared:
    if field == CREATE_RULE:
        parsed = _parse(_RuleValue, field, value)
        new = parsed.model_dump(exclude_none=True)
        return Prepared(None, f"{parsed.pattern} → {parsed.category}", None, new, False)
    ref = _target(field, target)
    _no_value(field, value)
    rule = await session.get(Rule, int(ref)) if ref.isdigit() else None
    if rule is None:
        raise AssistantError(404, "not_found", "Rule not found")
    _taken(proposals, field, str(rule.id), lambda p: p.target_ref)
    old = {"match": rule.match, "pattern": rule.pattern, "category": rule.category}
    return Prepared(str(rule.id), f"{rule.pattern} → {rule.category}", old, None, True)


# --- apply and revert (at approval time) ---------------------------------------------------------------------------------


async def apply(session, processor, proposal, principal, *, resumed: bool) -> dict | None:
    """Do what ``proposal`` says through the owner's own service; the dict is kept as ``proposal.result``."""
    field, ref, value = proposal.field, proposal.target_ref, proposal.new_value
    if field == CREATE_CATEGORY:
        await categories.create(session, processor, value, principal)
    elif field == RENAME_CATEGORY:
        await categories.rename(session, processor, ref, value, principal)
    elif field == DELETE_CATEGORY:
        await categories.delete(session, processor, ref, principal)
    elif field == LEDGER_EDIT:
        return await _apply_ledger_edit(session, processor, proposal, principal, resumed=resumed)
    elif field == LEDGER_DELETE:
        return await _apply_ledger_delete(session, processor, proposal, principal, resumed=resumed)
    elif field == CREATE_BUDGET:
        await budget_edit.create(session, processor.firefly, value["name"], Decimal(value["amount"]) if "amount" in value else None, value.get("month"), principal)
    elif field == SET_BUDGET_LIMIT:
        await budget_edit.set_limit(session, processor.firefly, ref, Decimal(value["amount"]), value["month"], principal)
    elif field == DELETE_BUDGET:
        await budget_edit.delete(session, processor.firefly, ref, principal)
    elif field == CREATE_RULE:
        await rules.add_for_owner(session, processor, match=value["match"], pattern=value["pattern"], category=value["category"], merchant=value.get("merchant"), principal=principal)
    elif field == DELETE_RULE:
        if await rules.remove_for_owner(session, int(ref), principal) is None and not resumed:
            raise AssistantError(404, "not_found", "Rule not found")
    return None


async def _apply_ledger_edit(session, processor, proposal, principal, *, resumed: bool) -> dict:
    version = await _movement(session, processor, proposal.target_ref)
    value = proposal.new_value
    current = _snapshot(version, value)
    if resumed and _matches(current, value):
        return {"change_id": None}
    if any(not _same_value(key, current[key], proposal.old_value[key]) for key in proposal.old_value):
        raise AssistantError(409, "conflict", "The movement changed after the proposal was created; the proposal is stale")
    if "category" in value:
        value = {**value, "category": await canonical_category(processor, value["category"])}
    try:
        change = await ledger.edit(session, processor, proposal.target_ref, ledger.LedgerEdit(**value), principal)
    except ledger.LedgerError as exc:
        raise AssistantError(exc.status, exc.code, exc.detail) from exc
    return {"change_id": change.id if change else None}


async def _apply_ledger_delete(session, processor, proposal, principal, *, resumed: bool) -> dict:
    try:
        version = await ledger.get(session, processor, proposal.target_ref)
    except ledger.LedgerError as exc:
        if resumed and exc.status == 404:
            return {"change_id": None}
        raise AssistantError(exc.status, exc.code, exc.detail) from exc
    if any(not _same_value(key, _snapshot(version, (key,))[key], item) for key, item in proposal.old_value.items()):
        raise AssistantError(409, "conflict", "The movement changed after the proposal was created; the proposal is stale")
    try:
        change = await ledger.delete(session, processor, proposal.target_ref, principal)
    except ledger.LedgerError as exc:
        raise AssistantError(exc.status, exc.code, exc.detail) from exc
    return {"change_id": change.id}


async def revert(session, processor, proposal, principal) -> None:
    """Undo a ``ledger_edit`` or ``ledger_delete`` through the ledger's own undo (nothing to undo when it changed nothing)."""
    change_id = (proposal.result or {}).get("change_id")
    if change_id is None:
        return
    try:
        await ledger.undo(session, processor, change_id, principal)
    except ledger.LedgerError as exc:
        raise AssistantError(exc.status, exc.code, exc.detail) from exc


def revertible(proposal) -> bool:
    return proposal.field not in FIELDS or proposal.field in REVERTIBLE
