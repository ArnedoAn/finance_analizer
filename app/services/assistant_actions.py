"""Assistant proposals that are not an edit of an inbox item: categories, movements, budgets, rules, debts and incomes.

Same flow as every proposal (the model proposes, the owner approves, ``assistant.apply`` applies and reports per
proposal), and every write reuses the service the owner's own screens use: ``categories``, ``ledger``, ``budget_edit``,
``rules``, ``debts`` and ``incomes``. ``prepare`` validates and snapshots at proposal time; ``apply`` and ``revert`` run
later. Nothing here commits proposal rows; the services commit their own work.

====================  =====================  ==========================================================
field                 target_ref             new_value
====================  =====================  ==========================================================
create_category       -                      name
rename_category       current name           new name
delete_category       name                   -
ledger_edit           Firefly transaction    {category, budget, source_account, merchant, description,
                                             date, amount, tags_add, tags_remove} (any subset)
ledger_delete         Firefly transaction    -
ledger_create         -                      {type, amount, date, description, source_account,
                                             destination_account, category?, budget?, tags?}
debt_payment          debt id (or name)      {amount, date, source_account}
create_budget         -                      {name, amount?, month?}
set_budget_limit      budget name            {amount, month}
rename_budget         current name           {name} (a plain text is accepted too)
delete_budget         budget name            -
create_rule           -                      {match, pattern, category, merchant?, tags?}
delete_rule           rule id                -
create_debt           -                      body of ``POST /debts`` (``debts.DebtIn``)
update_debt           debt id                the fields of ``PUT /debts/{id}`` that change
delete_debt           debt id                -
create_income         -                      body of ``POST /incomes`` (``incomes.IncomeIn``)
update_income         income id              the fields of ``PUT /incomes/{id}`` that change
delete_income         income id              -
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

from app.core.exceptions import PermanentError
from app.db.models_ledger import LedgerChange
from app.db.models_rules import Rule
from app.parsers.common import fold
from app.services import budget_edit, categories, debts, incomes, ledger, rules
from app.services import debt_math as math
from app.services.alerts import money as format_money
from app.services.assistant_errors import AssistantError
from app.services.budget_assign import current_month, valid_month

CREATE_CATEGORY = "create_category"
RENAME_CATEGORY = "rename_category"
DELETE_CATEGORY = "delete_category"
LEDGER_EDIT = "ledger_edit"
LEDGER_DELETE = "ledger_delete"
LEDGER_CREATE = "ledger_create"
DEBT_PAYMENT = "debt_payment"
CREATE_BUDGET = "create_budget"
SET_BUDGET_LIMIT = "set_budget_limit"
RENAME_BUDGET = "rename_budget"
DELETE_BUDGET = "delete_budget"
CREATE_RULE = "create_rule"
DELETE_RULE = "delete_rule"
CREATE_DEBT = "create_debt"
UPDATE_DEBT = "update_debt"
DELETE_DEBT = "delete_debt"
CREATE_INCOME = "create_income"
UPDATE_INCOME = "update_income"
DELETE_INCOME = "delete_income"

FIELDS = frozenset({
    CREATE_CATEGORY, RENAME_CATEGORY, DELETE_CATEGORY, LEDGER_EDIT, LEDGER_DELETE, LEDGER_CREATE, DEBT_PAYMENT,
    CREATE_BUDGET, SET_BUDGET_LIMIT, RENAME_BUDGET, DELETE_BUDGET, CREATE_RULE, DELETE_RULE,
    CREATE_DEBT, UPDATE_DEBT, DELETE_DEBT, CREATE_INCOME, UPDATE_INCOME, DELETE_INCOME,
})
_DEBT_FIELDS = frozenset({CREATE_DEBT, UPDATE_DEBT, DELETE_DEBT})
_INCOME_FIELDS = frozenset({CREATE_INCOME, UPDATE_INCOME, DELETE_INCOME})
# Applied first so what later proposals name (a category, budget, debt or income) already exists; deletions last so nothing
# still needs what they remove. A payment or a new movement that names something created in the batch runs after it.
_FIRST = frozenset({CREATE_CATEGORY, CREATE_BUDGET, CREATE_DEBT, CREATE_INCOME})
_SECOND = frozenset({RENAME_CATEGORY, SET_BUDGET_LIMIT, RENAME_BUDGET, UPDATE_DEBT, UPDATE_INCOME})
_LAST = frozenset({DELETE_CATEGORY, DELETE_BUDGET, DELETE_RULE, LEDGER_DELETE, DELETE_DEBT, DELETE_INCOME})
# "Deshacer lote" restores these through the ledger's own undo (a created movement is deleted); the rest has no automatic undo.
REVERTIBLE = frozenset({LEDGER_EDIT, LEDGER_DELETE, LEDGER_CREATE, DEBT_PAYMENT})
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
    tags: list[_Tag] | None = Field(default=None, max_length=10)

class _MovementValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["withdrawal", "deposit", "transfer"]
    amount: _Money
    date: AwareDatetime
    description: _Name
    source_account: _Name
    destination_account: _Name
    category: _Name | None = None
    budget: _Name | None = None
    tags: list[_Tag] = Field(default_factory=list, max_length=10)


class _PaymentValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: _Money
    date: AwareDatetime
    source_account: _Name


class _RenameValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]



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
    if field == LEDGER_CREATE:
        return await _prepare_movement(processor, batch, proposals, field, target, value)
    if field == DEBT_PAYMENT:
        return await _prepare_debt_payment(session, processor, batch, proposals, field, target, value)
    if field in _DEBT_FIELDS:
        return await _prepare_debt(session, processor, proposals, field, target, value)
    if field in _INCOME_FIELDS:
        return await _prepare_income(session, proposals, field, target, value)
    if field in {CREATE_BUDGET, SET_BUDGET_LIMIT, RENAME_BUDGET, DELETE_BUDGET}:
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
    if field == RENAME_BUDGET:
        if budget is None:
            raise AssistantError(422, "unknown_budget", f"Unknown budget: {name}")
        new = _parse(_RenameValue, field, {"name": value} if isinstance(value, str) else value).name
        if new == budget["name"]:
            raise _invalid("The new name is the same as the current one")
        clash = await budget_edit.find(processor.firefly, new)
        if clash is not None and clash["id"] != budget["id"]:
            raise AssistantError(409, "conflict", f"A budget named «{clash['name']}» already exists")
        if new.casefold() in _pending_names(proposals, CREATE_BUDGET, RENAME_BUDGET):
            raise AssistantError(409, "conflict", "The batch already creates or renames a budget to that name")
        _taken(proposals, field, budget["name"], lambda p: p.target_ref)
        return Prepared(budget["name"], f"{budget['name']} → {new}", budget["name"], {"name": new}, False)
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
        subject = f"{parsed.pattern} → {parsed.category}"
        if parsed.tags:
            new["tags"] = rules.clean_tags(parsed.tags)
            subject += f" · {', '.join(new['tags'])}"
        else:
            new.pop("tags", None)
        return Prepared(None, subject[:255], None, new, False)
    ref = _target(field, target)
    _no_value(field, value)
    rule = await session.get(Rule, int(ref)) if ref.isdigit() else None
    if rule is None:
        raise AssistantError(404, "not_found", "Rule not found")
    _taken(proposals, field, str(rule.id), lambda p: p.target_ref)
    old = {"match": rule.match, "pattern": rule.pattern, "category": rule.category}
    return Prepared(str(rule.id), f"{rule.pattern} → {rule.category}", old, None, True)


# --- new movements and debt payments -------------------------------------------------------------------------------------


def _within_window(moment: datetime, batch) -> None:
    if not batch.window_from <= moment <= batch.window_to:
        raise AssistantError(422, "outside_window", "The date is outside the batch window")


def _live(proposals, *fields):
    return [proposal for proposal in proposals if proposal.status != "discarded" and proposal.field in fields]


def _pending_names(proposals, *fields) -> set[str]:
    """Casefolded names that the batch's own creations and renames will make exist (``new_value`` is a name or ``{name}``)."""
    return {(p.new_value["name"] if isinstance(p.new_value, dict) else p.new_value).casefold() for p in _live(proposals, *fields)}


def _pending_accounts(proposals) -> set[str]:
    """Casefolded names of the liabilities that a ``create_debt`` of the batch will create (a linked account already exists)."""
    return {p.new_value["name"].casefold() for p in _live(proposals, CREATE_DEBT) if not p.new_value.get("firefly_account_id")}


def _ledger_failure(exc: ledger.LedgerError) -> AssistantError:
    return AssistantError(exc.status, exc.code, exc.detail)


def _domain_failure(exc: debts.DebtError | incomes.IncomeError) -> AssistantError:
    return AssistantError(exc.status, exc.code, exc.detail)


async def _dry_run(processor, draft: ledger.LedgerDraft) -> dict:
    """Validate ``draft`` against the ledger exactly as ``ledger.create`` will, writing nothing (not even merchant accounts)."""
    try:
        return await ledger.resolve(processor, draft, create_accounts=False)
    except ledger.LedgerError as exc:
        raise _ledger_failure(exc) from exc


def _draft(ref: str, kind: str, moment: datetime, description: str, amount, source: str, destination: str, **split) -> ledger.LedgerDraft:
    return ledger.LedgerDraft(
        client_ref=ref, type=kind, date=moment, description=description,
        splits=[ledger.LedgerSplit(amount=Decimal(amount), source_account=source, destination_account=destination, **split)],
    )


def _movement_draft(value: dict, ref: str) -> ledger.LedgerDraft:
    extra = {key: value[key] for key in ("category", "budget", "tags") if key in value}
    return _draft(
        ref, value["type"], datetime.fromisoformat(value["date"]), value["description"], value["amount"],
        value["source_account"], value["destination_account"], **extra,
    )


def _movement_key(value: dict) -> tuple:
    return (value["type"], value["amount"], value["date"], value["description"].casefold(), value["source_account"].casefold(), value["destination_account"].casefold())


async def _validate_movement(processor, proposals, new: dict) -> None:
    """Dry run of what ``apply`` will write; replaces the names in ``new`` by their Firefly spelling.

    What a creation earlier in the batch brings (a liability, category or budget) is not in Firefly yet: it is checked when applied."""
    if {new["source_account"].casefold(), new["destination_account"].casefold()} & _pending_accounts(proposals):
        return
    checked = dict(new)
    if "category" in checked:
        found = categories.find(await processor.categories(), checked["category"])
        if found is None and fold(checked["category"]) in {fold(name) for name in _pending_names(proposals, CREATE_CATEGORY, RENAME_CATEGORY)}:
            del checked["category"]
    if "budget" in checked and await budget_edit.find(processor.firefly, checked["budget"]) is None and checked["budget"].casefold() in _pending_names(proposals, CREATE_BUDGET, RENAME_BUDGET):
        del checked["budget"]
    if "category" in checked:
        checked["category"] = await canonical_category(processor, checked["category"])
    row = (await _dry_run(processor, _movement_draft(checked, "preview")))["splits"][0]
    for key in ("source_account", "destination_account", "category", "budget"):
        if key in checked:
            new[key] = row[key]


async def _prepare_movement(processor, batch, proposals, field: str, target, value) -> Prepared:
    if target is not None and str(target).strip():
        raise _invalid("ledger_create takes no target_ref: send null")
    parsed = _parse(_MovementValue, field, value)
    _within_window(parsed.date, batch)
    new = {
        "type": parsed.type, "amount": _money(parsed.amount), "date": parsed.date.astimezone(UTC).isoformat(),
        "description": parsed.description, "source_account": parsed.source_account, "destination_account": parsed.destination_account,
        **({"category": parsed.category} if parsed.category else {}), **({"budget": parsed.budget} if parsed.budget else {}),
        **({"tags": list(dict.fromkeys(parsed.tags))} if parsed.tags else {}),
    }
    _taken(proposals, field, _movement_key(new), lambda p: _movement_key(p.new_value))
    await _validate_movement(processor, proposals, new)
    day = parsed.date.astimezone(processor.zone).date().isoformat()
    return Prepared(None, f"{new['description']} · {new['amount']} · {day}"[:255], None, new, False)


async def _find_debt(session, ref: str):
    """The active debt whose id (or, failing that, name) is ``ref``."""
    rows = await debts.active(session)
    found = next((row for row in rows if ref.isdigit() and row.id == int(ref)), None)
    return found or next((row for row in rows if row.name.casefold() == ref.casefold()), None)


def _preview(capital: Decimal, expense: Decimal) -> dict:
    """How a payment divides: what reduces the debt and what the lender keeps (interest and insurance)."""
    return {"capital": _money(capital), "interest_and_insurance": _money(expense)}


def _pending_preview(pending: dict, amount: Decimal) -> dict | None:
    """The breakdown of a payment to a debt that the batch itself creates; None when its balance is not in the proposal."""
    if pending["kind"] == "credit_card":
        return _preview(amount, Decimal(0))
    if pending.get("balance") is None or pending.get("firefly_account_id"):
        return None
    split = math.payment_split(Decimal(pending["balance"]), Decimal(pending["rate_ea"]), Decimal(pending.get("insurance_monthly") or 0), amount)
    return _preview(split.capital, split.expense)


async def _payment_drafts(processor, row, capital: Decimal, expense: Decimal, value: dict, ref: str) -> list[ledger.LedgerDraft]:
    """The movements of a payment: ``capital`` as a transfer to the debt and ``expense`` (interest plus insurance) as a
    withdrawal to the lender's interest account, like the processor writes an automatic loan payment. A card is one transfer."""
    moment, source = datetime.fromisoformat(value["date"]), value["source_account"]
    drafts = []
    if capital > 0:
        label = f"Abono a capital de {row.name}" if expense > 0 else f"Pago de {row.name}"
        drafts.append(_draft(f"{ref}:capital", "transfer", moment, label, capital, source, row.firefly_account))
    if expense > 0:
        category = next((name for name in await processor.categories() if name.casefold() == "comisiones e impuestos"), None)
        interest_account = f"Intereses y seguro · {row.lender or row.name}"[:255]
        drafts.append(_draft(
            f"{ref}:interes", "withdrawal", moment, f"Intereses y seguro de {row.name}", expense, source, interest_account,
            tags=["intereses"], **({"category": category} if category else {}),
        ))
    return drafts


async def _prepare_debt_payment(session, processor, batch, proposals, field: str, target, value) -> Prepared:
    ref = _target(field, target)
    parsed = _parse(_PaymentValue, field, value)
    _within_window(parsed.date, batch)
    new = {"amount": _money(parsed.amount), "date": parsed.date.astimezone(UTC).isoformat(), "source_account": parsed.source_account}
    row = await _find_debt(session, ref)
    if row is None:
        pending = next((p.new_value for p in _live(proposals, CREATE_DEBT) if p.new_value["name"].casefold() == ref.casefold()), None)
        if pending is None:
            raise AssistantError(422, "unknown_debt", f"Unknown debt: {ref}")
        key = (pending["name"].casefold(), new["amount"], new["date"])
        _taken(proposals, field, key, lambda p: (p.target_ref.casefold(), p.new_value["amount"], p.new_value["date"]))
        return Prepared(pending["name"], pending["name"], _pending_preview(pending, parsed.amount), new, False)
    key = (str(row.id), new["amount"], new["date"])
    _taken(proposals, field, key, lambda p: (p.target_ref.casefold(), p.new_value["amount"], p.new_value["date"]))
    processor.invalidate_accounts()
    split = await debts.payment_split(processor.firefly, row, parsed.amount)
    for draft in await _payment_drafts(processor, row, split.capital, split.expense, new, "preview"):
        await _dry_run(processor, draft)
    return Prepared(str(row.id), row.name, _preview(split.capital, split.expense), new, False)


# --- debts and incomes -----------------------------------------------------------------------------------------------------


def _peso(amount, currency: str = "COP") -> str:
    return format_money(amount, currency).removesuffix(" COP")


def _debt_subject(data: dict) -> str:
    """«Crédito Lulo · saldo $1.500.000 · 24,95 % EA · cuota $120.000»."""
    card = data["kind"] == "credit_card"
    parts = [f"{'Tarjeta' if card else 'Crédito'} {data['name']}"]
    if data.get("balance") is not None:
        parts.append(f"saldo {_peso(data['balance'])}")
    parts.append(f"{Decimal(data['rate_ea']):.2f}".replace(".", ",") + " % EA")
    for key, label in (("installment", "cuota"), ("credit_limit", "cupo"), ("minimum_payment", "pago mínimo")):
        if data.get(key) is not None:
            parts.append(f"{label} {_peso(data[key])}")
    return " · ".join(parts)[:255]


def _income_subject(data: dict) -> str:
    """«Salario · $3.600.000 · fijo · día 30»."""
    parts = [data["name"], _peso(data["amount"], data["currency"]), "fijo" if data["kind"] == "fixed" else "variable"]
    if data.get("day_of_month") is not None:
        parts.append(f"día {data['day_of_month']}")
    return " · ".join(parts)[:255]


def _patch(model, field: str, value, not_null) -> dict:
    """The partial body of an update, validated like the ``PUT``; an empty one, a null in a required field or ``active`` is refused."""
    parsed = _parse(model, field, value)
    fields = parsed.model_dump(mode="json", exclude_unset=True)
    if not fields:
        raise _invalid(f"{field} needs at least one change")
    for name in not_null:
        if name in fields and fields[name] is None:
            raise _invalid(f"{name} cannot be null")
    if "active" in fields:
        raise _invalid(f"active cannot be changed here: use {field.replace('update', 'delete')}")
    return fields


def _record_id(field: str, target) -> int:
    ref = _target(field, target)
    if not ref.isdigit():
        raise _invalid(f"{field} needs the numeric id of the record (list_debts or list_incomes)")
    return int(ref)


async def _prepare_debt(session, processor, proposals, field: str, target, value) -> Prepared:
    try:
        if field == CREATE_DEBT:
            parsed = _parse(debts.DebtIn, field, value)
            _taken(proposals, field, parsed.name.casefold(), lambda p: p.new_value["name"].casefold())
            await debts.validate_new(session, processor.firefly, parsed.model_dump())
            new = parsed.model_dump(mode="json", exclude_none=True)
            return Prepared(None, _debt_subject(new), None, new, False)
        row = await debts.get(session, _record_id(field, target))
        if field == DELETE_DEBT:
            _no_value(field, value)
            _taken(proposals, field, str(row.id), lambda p: p.target_ref)
            return Prepared(str(row.id), row.name, {"name": row.name}, None, True)
        new = _patch(debts.DebtPatch, field, value, debts.NOT_NULL)
        if "balance" in new:
            raise _invalid("The balance is Firefly's: it cannot be changed with update_debt")
        await debts.validate_update(session, processor.firefly, row, new)
    except debts.DebtError as exc:
        raise _domain_failure(exc) from exc
    _taken(proposals, field, str(row.id), lambda p: p.target_ref)
    current = debts.view(row, Decimal(0))
    return Prepared(str(row.id), row.name, {key: current[key] for key in new}, new, False)


async def _prepare_income(session, proposals, field: str, target, value) -> Prepared:
    try:
        if field == CREATE_INCOME:
            parsed = _parse(incomes.IncomeIn, field, value)
            new = parsed.model_dump(mode="json", exclude_none=True)
            _taken(proposals, field, (parsed.name.casefold(), parsed.currency), lambda p: (p.new_value["name"].casefold(), p.new_value["currency"]))
            return Prepared(None, _income_subject(new), None, new, False)
        row = await incomes.get(session, _record_id(field, target))
    except incomes.IncomeError as exc:
        raise _domain_failure(exc) from exc
    if field == DELETE_INCOME:
        _no_value(field, value)
        _taken(proposals, field, str(row.id), lambda p: p.target_ref)
        return Prepared(str(row.id), row.name, {"name": row.name}, None, True)
    new = _patch(incomes.IncomePatch, field, value, incomes.NOT_NULL)
    _taken(proposals, field, str(row.id), lambda p: p.target_ref)
    current = incomes.view(row)
    return Prepared(str(row.id), row.name, {key: current[key] for key in new}, new, False)


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
    elif field == RENAME_BUDGET:
        return await _apply_rename_budget(session, processor, ref, value["name"], principal, resumed=resumed)
    elif field == DELETE_BUDGET:
        await budget_edit.delete(session, processor.firefly, ref, principal)
    elif field == CREATE_RULE:
        await rules.add_for_owner(
            session, processor, match=value["match"], pattern=value["pattern"], category=value["category"], merchant=value.get("merchant"),
            tags=value.get("tags"), principal=principal,
        )
    elif field == DELETE_RULE:
        if await rules.remove_for_owner(session, int(ref), principal) is None and not resumed:
            raise AssistantError(404, "not_found", "Rule not found")
    elif field == LEDGER_CREATE:
        return await _apply_movement(session, processor, proposal, principal)
    elif field == DEBT_PAYMENT:
        return await _apply_debt_payment(session, processor, proposal, principal)
    elif field in _DEBT_FIELDS:
        return await _apply_debt(session, processor, proposal, principal, resumed=resumed)
    elif field in _INCOME_FIELDS:
        return await _apply_income(session, proposal, principal, resumed=resumed)
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


async def _write_movement(session, processor, draft: ledger.LedgerDraft, principal) -> dict:
    """Create one movement through the owner's ledger; ``draft.client_ref`` makes a repeat return the first one."""
    try:
        public, change = await ledger.create(session, processor, draft, principal)
    except ledger.LedgerError as exc:
        raise _ledger_failure(exc) from exc
    except PermanentError as exc:
        raise _ledger_failure(ledger.firefly_rejection(exc)) from exc
    total = sum((Decimal(split["amount"]) for split in public["splits"]), Decimal(0))
    return {"change_id": change.id, "firefly_id": public["id"], "type": public["type"], "amount": _money(total)}


async def _apply_movement(session, processor, proposal, principal) -> dict:
    value = proposal.new_value
    if "category" in value:
        value = {**value, "category": await canonical_category(processor, value["category"])}
    return {"movements": [await _write_movement(session, processor, _movement_draft(value, f"assistant:{proposal.id}"), principal)]}


async def _apply_debt_payment(session, processor, proposal, principal) -> dict:
    """Pay a debt: the breakdown comes from ``debts.payment_split`` with the balance Firefly has now, is stored in the proposal's
    ``result`` before anything is written (a retry writes the same figures although the balance has dropped) and the movements
    are idempotent by ``client_ref``."""
    row = await _find_debt(session, proposal.target_ref)
    if row is None:
        raise AssistantError(404, "not_found", "Debt not found")
    value, ref = proposal.new_value, f"assistant:{proposal.id}"
    processor.invalidate_accounts()
    plan = proposal.result
    if not plan or "capital" not in plan:
        split = await debts.payment_split(processor.firefly, row, Decimal(value["amount"]))
        plan = {**_preview(split.capital, split.expense), "movements": []}
        for draft in await _payment_drafts(processor, row, split.capital, split.expense, value, ref):
            await _dry_run(processor, draft)
        proposal.result = plan
        await session.commit()
    drafts = await _payment_drafts(processor, row, Decimal(plan["capital"]), Decimal(plan["interest_and_insurance"]), value, ref)
    return {**plan, "movements": [await _write_movement(session, processor, draft, principal) for draft in drafts]}


async def _apply_debt(session, processor, proposal, principal, *, resumed: bool) -> dict | None:
    field, value = proposal.field, proposal.new_value
    try:
        if field == CREATE_DEBT:
            data = debts.DebtIn.model_validate(value).model_dump()
            twin = next((row for row in await debts.active(session) if row.name.casefold() == data["name"].casefold()), None)
            if resumed and twin is not None and twin.kind == data["kind"] and Decimal(twin.rate_ea) == data["rate_ea"]:
                return {"debt_id": twin.id}  # an earlier attempt created it and died before marking the proposal
            created = await debts.create(session, processor.firefly, processor.settings, data, principal)
            processor.invalidate_accounts()
            return {"debt_id": created["id"]}
        if field == UPDATE_DEBT:
            await debts.update(session, processor.firefly, processor.settings, int(proposal.target_ref), debts.DebtPatch.model_validate(value).model_dump(exclude_unset=True), principal)
        else:
            await debts.deactivate(session, int(proposal.target_ref), principal)
    except debts.DebtError as exc:
        if resumed and field == DELETE_DEBT and exc.status == 404:
            return None
        raise _domain_failure(exc) from exc
    return None


async def _apply_income(session, proposal, principal, *, resumed: bool) -> dict | None:
    field, value = proposal.field, proposal.new_value
    try:
        if field == CREATE_INCOME:
            data = incomes.IncomeIn.model_validate(value).model_dump()
            same = (data["name"], data["kind"], data["currency"], data["amount"])
            twin = next((row for row in await incomes.active(session) if (row.name, row.kind, row.currency, Decimal(row.amount)) == same), None)
            if resumed and twin is not None:
                return {"income_id": twin.id}  # an earlier attempt created it and died before marking the proposal
            return {"income_id": (await incomes.create(session, data, principal))["id"]}
        if field == UPDATE_INCOME:
            await incomes.update(session, int(proposal.target_ref), incomes.IncomePatch.model_validate(value).model_dump(exclude_unset=True), principal)
        else:
            await incomes.deactivate(session, int(proposal.target_ref), principal)
    except incomes.IncomeError as exc:
        if resumed and field == DELETE_INCOME and exc.status == 404:
            return None
        raise _domain_failure(exc) from exc
    return None


async def _apply_rename_budget(session, processor, ref: str, new: str, principal, *, resumed: bool) -> None:
    firefly = processor.firefly
    if resumed and await budget_edit.find(firefly, ref) is None and await budget_edit.find(firefly, new) is not None:
        return  # an earlier attempt renamed it
    await budget_edit.rename(session, firefly, ref, new, principal)


async def _undo(session, processor, change_id: int, principal) -> None:
    try:
        await ledger.undo(session, processor, change_id, principal)
    except ledger.LedgerError as exc:
        raise _ledger_failure(exc) from exc


async def revert(session, processor, proposal, principal) -> None:
    """Undo a ledger action through the ledger's own undo: the edit or deletion (``change_id``; nothing to undo when it changed
    nothing) or every movement a creation wrote, newest first. A movement already undone is skipped, so a partly failed revert
    can be repeated."""
    result = proposal.result or {}
    if "movements" not in result:
        if result.get("change_id") is not None:
            await _undo(session, processor, result["change_id"], principal)
        return
    for movement in reversed(result["movements"]):
        change = await session.get(LedgerChange, movement["change_id"], populate_existing=True)
        if change is not None and change.undone_at is None:
            await _undo(session, processor, change.id, principal)


def revertible(proposal) -> bool:
    return proposal.field not in FIELDS or proposal.field in REVERTIBLE
