"""Manual ledger editing: create, edit, delete, bulk edit and undo of ANY Firefly movement (contract section 15).

Every write to Firefly goes through here. A movement is handled as a *version*: a JSON-safe dict that is the
normalized form of a Firefly transaction group (``normalize``) as well as the resolved form of a ``LedgerDraft``
(``resolve``)::

    {type, date (UTC), description, notes, external_id,
     splits: [{amount, currency, foreign_amount, foreign_currency, source_account, destination_account,
               category, budget, bill, tags, notes, description, refs: {source, destination, budget, bill}}]}

``refs`` are the Firefly ids behind the names. They never reach the API (``_public`` drops them) nor the conflict
check (``_comparable``), but they let an undo restore a movement exactly, without re-validating legacy data.

Firefly keeps notes and descriptions per split while the contract has one of each per movement, so both are
*factored*: the movement value is shared by every split that has the same text, and a split carries its own only
when it differs (``_assemble`` / ``_effective`` are inverse). A single-split movement has no split description.

Persist order: Firefly first (idempotent by ``external_id`` for creations), then the ``ledger_changes`` row with the
version before and after, the inbox synchronization and the audit record, all in ONE commit.
"""
import asyncio
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from weakref import WeakValueDictionary

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import FinanceAnalyzerError, PermanentError
from app.core.logging import get_logger
from app.core.timezone import now_utc
from app.db.models import InboxEvent, InboxItem
from app.db.models_ledger import LedgerChange
from app.db.repositories import InboxRepository
from app.parsers import normalize_merchant
from app.services import audit
from app.services.alerts import money as format_money

logger = get_logger(__name__)

TYPES = ("withdrawal", "deposit", "transfer")
OWN_TYPES = frozenset({"asset", "liability", "liabilities"})
MAX_SPLITS = 20
MAX_BULK = 100
MAX_CLIENT_REF = 80
MAX_TEXT = 255
MAX_NOTES = 4000
MAX_TAGS = 10
MAX_TAG = 40
MAX_FOREIGN = Decimal("999999999999.99")
UNDO_WINDOW = timedelta(days=7)
_CENTS = Decimal("0.01")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_REJECTION = re.compile(r"^Firefly (\d{3}):")
_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

_ACCOUNT_KINDS = {"asset": "activo", "liability": "pasivo", "liabilities": "pasivo", "expense": "gasto", "revenue": "ingreso"}
_KIND_PHRASE = {"withdrawal": "un gasto", "deposit": "un ingreso", "transfer": "una transferencia"}
_VERBS = {"create": "Creaste", "update": "Editaste", "delete": "Eliminaste"}
_LABELS = {
    "type": "tipo", "date": "fecha", "description": "descripción", "notes": "notas", "splits": "divisiones",
    "amount": "monto", "currency": "moneda", "foreign": "moneda extranjera", "accounts": "cuentas",
    "category": "categoría", "budget": "presupuesto", "bill": "factura", "tags": "etiquetas",
}
_SPLIT_FIELDS = {
    "amount": "amount", "currency": "currency", "foreign_amount": "foreign", "foreign_currency": "foreign",
    "source_account": "accounts", "destination_account": "accounts", "category": "category", "budget": "budget",
    "bill": "bill", "tags": "tags", "notes": "notes", "description": "description",
}
_PUBLIC_SPLIT = (
    "amount", "currency", "foreign_amount", "foreign_currency", "source_account", "destination_account",
    "category", "budget", "bill", "tags", "notes", "description",
)
_INBOX_FIELDS = (
    "merchant", "category", "asset_account", "destination_account", "transaction_type", "description",
)


class LedgerError(Exception):
    """A client-visible failure; the router renders it as ``{"detail", "code"}`` with ``status``."""

    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def _invalid(detail: str) -> LedgerError:
    return LedgerError(422, "validation_error", detail)


def _not_found() -> LedgerError:
    return LedgerError(404, "not_found", "No existe ese movimiento.")


def _conflict(detail: str) -> LedgerError:
    return LedgerError(409, "conflict", detail)


# --- Request bodies --------------------------------------------------------------------------------------------------


class LedgerSplit(BaseModel):
    """One split without Firefly ids; accounts, category, budget and bill go by name. Business rules are checked in ``resolve``."""

    model_config = ConfigDict(extra="ignore")
    amount: Decimal
    currency: str = "COP"
    foreign_amount: Decimal | None = None
    foreign_currency: str | None = None
    source_account: str
    destination_account: str
    category: str | None = None
    budget: str | None = None
    bill: str | None = None
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None
    description: str | None = None


class LedgerDraft(BaseModel):
    """Body of create and update. ``client_ref`` is required to create and ignored by update."""

    model_config = ConfigDict(extra="ignore")
    client_ref: str | None = None
    type: str
    date: datetime
    description: str
    notes: str | None = None
    splits: list[LedgerSplit]


class LedgerBulkSet(BaseModel):
    model_config = ConfigDict(extra="ignore")
    category: str | None = None
    budget: str | None = None
    source_account: str | None = None
    tags_add: list[str] = Field(default_factory=list)
    tags_remove: list[str] = Field(default_factory=list)


class LedgerEdit(LedgerBulkSet):
    """Changes to ONE movement: everything a bulk edit sets plus description, date, amount (single split) and merchant."""

    description: str | None = None
    date: datetime | None = None
    amount: Decimal | None = None
    merchant: str | None = None


class LedgerBulk(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, coerce_numbers_to_str=True)
    ids: list[str]
    changes: LedgerBulkSet = Field(alias="set")


# --- Field validation ------------------------------------------------------------------------------------------------


def _opt(value) -> str | None:
    value = value.strip() if isinstance(value, str) else None
    return value or None


def _required(value, label: str) -> str:
    text = _opt(value)
    if text is None or len(text) > MAX_TEXT:
        raise _invalid(f"{label} es obligatoria y admite hasta {MAX_TEXT} caracteres.")
    return text


def _notes(value) -> str | None:
    text = _opt(value)
    if text is not None and len(text) > MAX_NOTES:
        raise _invalid(f"Las notas admiten hasta {MAX_NOTES} caracteres.")
    return text


def _amount(value: Decimal | None, label: str, maximum: Decimal) -> Decimal:
    if value is None or not value.is_finite() or value <= 0:
        raise _invalid(f"{label} debe ser mayor que 0.")
    if value.normalize().as_tuple().exponent < -2:
        raise _invalid(f"{label} admite máximo 2 decimales.")
    if value > maximum:
        raise _invalid(f"{label} supera el máximo permitido ({format_money(maximum)}).")
    return value.quantize(_CENTS)


def _clean_tags(values) -> list[str]:
    tags = []
    for tag in values or []:
        tag = tag.strip() if isinstance(tag, str) else ""
        if not tag or len(tag) > MAX_TAG:
            raise _invalid(f"Cada etiqueta debe tener entre 1 y {MAX_TAG} caracteres.")
        if tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise _invalid(f"Cada división admite hasta {MAX_TAGS} etiquetas.")
    return tags


def _date(value: datetime, zone) -> str:
    try:
        moment = (value if value.tzinfo else value.replace(tzinfo=zone)).astimezone(UTC)
    except (OverflowError, ValueError):
        raise _invalid("La fecha no es válida.") from None
    if not datetime(2000, 1, 1, tzinfo=UTC) <= moment < datetime(2100, 1, 1, tzinfo=UTC):
        raise _invalid("La fecha está fuera del rango permitido.")
    return moment.replace(microsecond=0).isoformat()


def _foreign(split: LedgerSplit) -> tuple[Decimal | None, str | None]:
    code = _opt(split.foreign_currency)
    if split.foreign_amount is None and code is None:
        return None, None
    if code is None:
        raise _invalid("Indica la moneda extranjera (foreign_currency).")
    if split.foreign_amount is None:
        raise _invalid("Indica el monto en moneda extranjera (foreign_amount).")
    code = code.upper()
    if len(code) != 3 or not code.isascii() or not code.isalpha() or code == "COP":
        raise _invalid("La moneda extranjera debe ser un código de tres letras distinto de COP.")
    return _amount(split.foreign_amount, "El monto en moneda extranjera", MAX_FOREIGN), code


# --- Accounts ----------------------------------------------------------------------------------------------------------


def _kind_of(account: dict) -> str:
    return _ACCOUNT_KINDS.get(account["type"], account["type"])


def _own(accounts: list[dict], name: str) -> dict:
    """An own (asset or liability) account. Own accounts are never created."""
    wanted = name.casefold()
    same = [account for account in accounts if account["name"].casefold() == wanted]
    for account in same:
        if account["type"] in OWN_TYPES:
            return account
    if same:
        raise _invalid(f"«{name}» no es una cuenta propia (es una cuenta de {_kind_of(same[0])}).")
    raise _invalid(f"La cuenta «{name}» no existe en Firefly.")


def _counterparty(accounts: list[dict], name: str, account_type: str, role: str) -> dict | None:
    """The merchant (expense) or income (revenue) account called ``name``; None when it has to be created."""
    wanted = name.casefold()
    same = [account for account in accounts if account["name"].casefold() == wanted]
    for account in same:
        if account["type"] == account_type:
            return account
    if same:
        raise _invalid(f"«{name}» es una cuenta de {_kind_of(same[0])} y no puede ser {role}.")
    key = normalize_merchant(name)
    if key:
        for account in accounts:
            if account["type"] == account_type and normalize_merchant(account["name"]) == key:
                return account
    return None


def _ref(value) -> str | None:
    text = str(value) if value is not None else ""
    return None if text in ("", "0", "None") else text


# --- Versions --------------------------------------------------------------------------------------------------------


def _assemble(kind: str, date: str, description: str, splits: list[dict], external_id: str | None) -> dict:
    """Factor the per-split descriptions and notes (module docstring); ``splits`` carry their effective values."""
    single = len(splits) == 1
    group_description = splits[0]["description"] if single else description
    notes = [split["notes"] for split in splits]
    common = notes[0] if len(set(notes)) == 1 else None
    factored = [
        {**split, "description": None if split["description"] == group_description else split["description"],
         "notes": None if split["notes"] == common else split["notes"]}
        for split in splits
    ]
    return {"type": kind, "date": date, "description": group_description, "notes": common, "external_id": external_id, "splits": factored}


def _effective(version: dict) -> list[dict]:
    return [
        {**split,
         "description": split["description"] if split["description"] is not None else version["description"],
         "notes": split["notes"] if split["notes"] is not None else version["notes"]}
        for split in version["splits"]
    ]


def _fmt(value) -> str:
    try:
        return f"{abs(Decimal(str(value))).quantize(_CENTS):.2f}"
    except (ArithmeticError, ValueError):
        return "0.00"


def normalize(raw: dict, zone) -> dict:
    """The version of a Firefly transaction group (the ``data`` object of ``GET /transactions/{id}``)."""
    rows = (raw.get("attributes") or {}).get("transactions") or []
    if not rows:
        raise _not_found()
    splits = []
    for row in rows:
        foreign = _fmt(row.get("foreign_amount"))
        has_foreign = Decimal(foreign) > 0 and bool(row.get("foreign_currency_code"))
        splits.append({
            "amount": _fmt(row.get("amount")), "currency": row.get("currency_code") or "COP",
            "foreign_amount": foreign if has_foreign else None,
            "foreign_currency": row["foreign_currency_code"] if has_foreign else None,
            "source_account": row.get("source_name") or "", "destination_account": row.get("destination_name") or "",
            "category": row.get("category_name") or None, "budget": row.get("budget_name") or None,
            "bill": row.get("bill_name") or None, "tags": list(row.get("tags") or []),
            "notes": row.get("notes") or None, "description": row.get("description") or "",
            "refs": {
                "source": _ref(row.get("source_id")), "destination": _ref(row.get("destination_id")),
                "budget": _ref(row.get("budget_id")), "bill": _ref(row.get("bill_id")),
            },
        })
    moment = datetime.fromisoformat(str(rows[0]["date"]).replace("Z", "+00:00"))
    moment = (moment if moment.tzinfo else moment.replace(tzinfo=zone)).astimezone(UTC).replace(microsecond=0)
    external_id = next((row["external_id"] for row in rows if row.get("external_id")), None)
    title = (raw["attributes"].get("group_title") or "").strip() or splits[0]["description"]
    return _assemble(rows[0].get("type") or "", moment.isoformat(), title, splits, external_id)


def _comparable(version: dict) -> dict:
    """What the conflict check compares: the user-visible state, without ids and with tags in a fixed order."""
    return {
        "type": version["type"], "date": version["date"], "description": version["description"], "notes": version["notes"],
        "splits": [
            {key: sorted(value) if key == "tags" else value for key, value in split.items() if key != "refs"}
            for split in version["splits"]
        ],
    }


def _changed(before: dict, after: dict) -> list[str]:
    old, new = _comparable(before), _comparable(after)
    fields = [key for key in ("type", "date", "description", "notes") if old[key] != new[key]]
    if len(old["splits"]) != len(new["splits"]):
        return [*fields, "splits"]
    for left, right in zip(old["splits"], new["splits"], strict=True):
        for key, label in _SPLIT_FIELDS.items():
            if left[key] != right[key] and label not in fields:
                fields.append(label)
    return fields


def _total(version: dict) -> Decimal:
    return sum((Decimal(split["amount"]) for split in version["splits"]), Decimal(0))


def _public(version: dict, firefly_id, inbox_id: int | None, zone) -> dict:
    """The ``LedgerTransaction`` of the contract: dates in America/Bogota and no Firefly ids inside the splits."""
    return {
        "id": str(firefly_id), "type": version["type"],
        "date": datetime.fromisoformat(version["date"]).astimezone(zone).isoformat(),
        "description": version["description"], "notes": version["notes"], "external_id": version["external_id"],
        "inbox_id": inbox_id,
        "splits": [{key: split[key] for key in _PUBLIC_SPLIT} for split in version["splits"]],
    }


def _payload(version: dict, zone, journals: tuple | list = (), *, update: bool) -> dict:
    """The Firefly III 6.x body for ``version``. Updates send every key (``null`` clears) and the ids of the journals they replace."""
    when = datetime.fromisoformat(version["date"]).astimezone(zone).isoformat()
    splits = _effective(version)
    external = [journals[number].get("external_id") if number < len(journals) else None for number in range(len(splits))]
    if version.get("external_id") and version["external_id"] not in external:
        external[0] = version["external_id"]
    bodies = []
    for number, split in enumerate(splits):
        refs = split["refs"]
        body = {
            "type": version["type"], "date": when, "amount": split["amount"], "currency_code": split["currency"],
            "foreign_amount": split["foreign_amount"], "foreign_currency_code": split["foreign_currency"],
            "description": split["description"], "category_name": split["category"], "tags": list(split["tags"]),
            "notes": split["notes"],
        }
        for side in ("source", "destination"):
            if refs[side]:
                body[f"{side}_id"] = refs[side]
            else:
                body[f"{side}_name"] = split[f"{side}_account"]
        for key in ("budget", "bill"):
            if refs[key]:
                body[f"{key}_id"] = refs[key]
            elif split[key]:
                body[f"{key}_name"] = split[key]
            else:
                body[f"{key}_id"] = None
        if external[number]:
            body["external_id"] = external[number]
        if update:
            if number < len(journals) and journals[number].get("id"):
                body["transaction_journal_id"] = journals[number]["id"]
        else:
            body = {key: value for key, value in body.items() if value is not None}
        bodies.append(body)
    payload = {"transactions": bodies}
    if len(bodies) > 1:
        payload["group_title"] = version["description"]
    return payload


def _journals(raw: dict) -> list[dict]:
    return [
        {"id": _ref(row.get("transaction_journal_id")), "external_id": row.get("external_id") or None}
        for row in raw["attributes"]["transactions"]
    ]


def _require_editable(version: dict) -> None:
    if version["type"] not in TYPES:
        raise _invalid("Este tipo de movimiento no se puede editar desde aquí.")


# --- Draft resolution ------------------------------------------------------------------------------------------------


def _split_row(kind, split, *, single, accounts, categories, budgets, bills, maximum):
    """Validate one split against the ledger; returns ``(row, pending)`` where ``pending`` is a merchant/income account to create."""
    amount = _amount(split.amount, "El monto", maximum)
    if (split.currency or "COP").strip().upper() != "COP":
        raise _invalid("El monto debe estar en COP; si el original fue en otra moneda, indica foreign_amount y foreign_currency.")
    foreign_amount, foreign_currency = _foreign(split)
    source_name = _required(split.source_account, "La cuenta de origen")
    destination_name = _required(split.destination_account, "La cuenta de destino")
    pending = None
    if kind == "withdrawal":
        source = _own(accounts, source_name)
        destination = _counterparty(accounts, destination_name, "expense", "el comercio de un gasto")
        pending = ("destination", "expense") if destination is None else None
    elif kind == "deposit":
        source = _counterparty(accounts, source_name, "revenue", "el origen de un ingreso")
        destination = _own(accounts, destination_name)
        pending = ("source", "revenue") if source is None else None
    else:
        source, destination = _own(accounts, source_name), _own(accounts, destination_name)
        if source["id"] == destination["id"]:
            raise _invalid("El origen y el destino de una transferencia deben ser cuentas distintas.")
    category = _opt(split.category)
    if category is not None:
        if category.casefold() not in categories:
            raise _invalid(f"La categoría «{category}» no existe en Firefly.")
        category = categories[category.casefold()]
    named = {}
    for key, label, known in (("budget", "El presupuesto", budgets), ("bill", "La factura", bills)):
        name = _opt(getattr(split, key))
        if name is not None and name.casefold() not in known:
            raise _invalid(f"{label} «{name}» no existe en Firefly.")
        named[key] = known[name.casefold()] if name is not None else None
    own_description = None if single else _opt(split.description)
    if own_description is not None and len(own_description) > MAX_TEXT:
        raise _invalid(f"La descripción admite hasta {MAX_TEXT} caracteres.")
    row = {
        "amount": f"{amount:.2f}", "currency": "COP",
        "foreign_amount": f"{foreign_amount:.2f}" if foreign_amount is not None else None, "foreign_currency": foreign_currency,
        "source_account": source["name"] if source else source_name,
        "destination_account": destination["name"] if destination else destination_name,
        "category": category, "budget": named["budget"]["name"] if named["budget"] else None,
        "bill": named["bill"]["name"] if named["bill"] else None, "tags": _clean_tags(split.tags),
        "notes": _notes(split.notes), "description": own_description,
        "refs": {
            "source": str(source["id"]) if source else None, "destination": str(destination["id"]) if destination else None,
            "budget": named["budget"]["id"] if named["budget"] else None, "bill": named["bill"]["id"] if named["bill"] else None,
        },
    }
    return row, pending


def _same_account(kind: str, rows: list[dict]) -> None:
    """Firefly needs one source for split withdrawals and transfers and one destination for split deposits and transfers."""
    if kind in ("withdrawal", "transfer") and len({row["source_account"].casefold() for row in rows}) > 1:
        raise _invalid("Todas las divisiones deben salir de la misma cuenta.")
    if kind in ("deposit", "transfer") and len({row["destination_account"].casefold() for row in rows}) > 1:
        raise _invalid("Todas las divisiones deben llegar a la misma cuenta.")


async def _create_missing(processor, rows: list[dict], pendings: list) -> None:
    """Create the merchant (expense) and income (revenue) accounts the draft needs, after every check passed."""
    accounts = await processor.accounts()
    created = {}
    for row, pending in zip(rows, pendings, strict=True):
        if pending is None:
            continue
        side, account_type = pending
        name = " ".join(row[f"{side}_account"].split())
        key = (account_type, name.casefold())
        if key not in created:
            created[key] = await processor.firefly.create_account(name, account_type)
            accounts.append(created[key])
        row[f"{side}_account"] = created[key]["name"]
        row["refs"][side] = str(created[key]["id"])


async def _named(lookup) -> dict:
    return {row["name"].casefold(): row for row in await lookup()}


async def resolve(processor, draft: LedgerDraft, *, create_accounts: bool = True) -> dict:
    """Validate ``draft`` against the ledger (contract section 15) and return its version. Own accounts are never created;
    with ``create_accounts=False`` neither are the merchant and income accounts (a dry run that writes nothing)."""
    kind = (draft.type or "").strip()
    if kind not in TYPES:
        raise _invalid("El tipo debe ser withdrawal, deposit o transfer.")
    description = (draft.description or "").strip()
    if not 1 <= len(description) <= MAX_TEXT:
        raise _invalid(f"La descripción es obligatoria y admite hasta {MAX_TEXT} caracteres.")
    notes = _notes(draft.notes)
    count = len(draft.splits)
    if not 1 <= count <= MAX_SPLITS:
        raise _invalid(f"Un movimiento debe tener entre 1 y {MAX_SPLITS} divisiones.")
    date = _date(draft.date, processor.zone)
    maximum = processor.settings.max_amount_cop
    accounts = await processor.accounts()
    categories = {name.casefold(): name for name in await processor.categories()}
    budgets = await _named(processor.firefly.budgets) if any(_opt(split.budget) for split in draft.splits) else {}
    bills = await _named(processor.firefly.bills) if any(_opt(split.bill) for split in draft.splits) else {}
    rows, pendings = [], []
    for number, split in enumerate(draft.splits, 1):
        try:
            row, pending = _split_row(kind, split, single=count == 1, accounts=accounts, categories=categories, budgets=budgets, bills=bills, maximum=maximum)
        except LedgerError as exc:
            if count == 1:
                raise
            raise LedgerError(exc.status, exc.code, f"División {number}: {exc.detail}") from exc
        row["description"] = row["description"] or description
        row["notes"] = row["notes"] if row["notes"] is not None else notes
        rows.append(row)
        pendings.append(pending)
    if sum((Decimal(row["amount"]) for row in rows), Decimal(0)) > maximum:
        raise _invalid(f"El total del movimiento supera el máximo permitido ({format_money(maximum)}).")
    _same_account(kind, rows)
    if create_accounts:
        await _create_missing(processor, rows, pendings)
    return _assemble(kind, date, description, rows, None)


def _draft_from(version: dict, zone) -> LedgerDraft:
    """The draft that would produce ``version`` again (bulk edits modify it and resolve it anew)."""
    public = _public(version, "", None, zone)
    return LedgerDraft(
        type=version["type"], date=datetime.fromisoformat(version["date"]), description=version["description"],
        notes=version["notes"], splits=[LedgerSplit(**split) for split in public["splits"]],
    )


# --- Inbox coherence ---------------------------------------------------------------------------------------------------


async def _inbox_item(db, external_id: str | None) -> InboxItem | None:
    """The inbox item behind a movement the analyzer wrote: its Firefly ``external_id`` is ``fa:<source_ref>``."""
    if external_id and external_id.startswith("fa:"):
        return await InboxRepository(db).by_ref(external_id[3:])
    return None


def _inbox_snapshot(item: InboxItem) -> dict:
    return {
        "status": item.status, "firefly_transaction_id": item.firefly_transaction_id,
        "amount": str(item.amount) if item.amount is not None else None,
        "occurred_at": item.occurred_at.astimezone(UTC).isoformat(), "tags": list(item.tags or []),
        **{name: getattr(item, name) for name in _INBOX_FIELDS},
    }


def _inbox_restore(item: InboxItem, snapshot: dict) -> None:
    for name in _INBOX_FIELDS:
        setattr(item, name, snapshot[name])
    item.amount = Decimal(snapshot["amount"]) if snapshot["amount"] is not None else None
    item.occurred_at = datetime.fromisoformat(snapshot["occurred_at"])
    item.tags = list(snapshot["tags"])


def _inbox_sync(item: InboxItem, version: dict) -> None:
    """Copy the edited movement into its inbox item (status untouched) so reviews and reconciliation agree with Firefly."""
    first = version["splits"][0]
    kind = version["type"]
    automatic = ["manual"] if item.source == "manual" else ["auto", item.source]
    item.amount = _total(version)
    item.category = first["category"]
    item.occurred_at = datetime.fromisoformat(version["date"])
    item.description = version["description"]
    item.tags = [tag for tag in dict.fromkeys(tag for split in version["splits"] for tag in split["tags"]) if tag not in automatic][:MAX_TAGS]
    if not (item.transaction_type == "refund" and kind == "deposit"):
        item.transaction_type = kind
    if kind == "withdrawal":
        item.merchant, item.asset_account, item.destination_account = first["destination_account"], first["source_account"], None
    elif kind == "deposit":
        item.merchant, item.asset_account, item.destination_account = first["source_account"], first["destination_account"], None
    else:
        item.asset_account, item.destination_account = first["source_account"], first["destination_account"]


# --- History -----------------------------------------------------------------------------------------------------------


def _summary(action: str, version: dict, fields: list[str] | None = None) -> str:
    text = f"{_VERBS[action]} {_KIND_PHRASE.get(version['type'], 'un movimiento')} de {format_money(_total(version))}"
    if action == "update":
        text += f" ({', '.join(_LABELS[field] for field in fields)})" if fields else " (sin cambios)"
    return text


def _undo_deadline(change: LedgerChange) -> datetime:
    return change.at + UNDO_WINDOW


def change_data(change: LedgerChange) -> dict:
    """The ``LedgerChange`` of the contract."""
    return {
        "id": change.id, "group_id": change.group_id, "action": change.action, "firefly_id": change.firefly_id,
        "at": change.at, "actor": change.actor, "summary": change.summary, "undone_at": change.undone_at,
        "undo_deadline": _undo_deadline(change),
    }


async def _record(db, principal, *, action, firefly_id, before, after, group_id, client_ref=None, item=None, inbox_before=None) -> LedgerChange:
    """Stage the change row and its audit record; the caller commits."""
    version = after or before
    fields = _changed(before, after) if action == "update" else []
    change = LedgerChange(
        group_id=group_id or uuid4().hex, action=action, firefly_id=str(firefly_id), client_ref=client_ref, at=now_utc(),
        actor=getattr(principal, "name", None) or "system", device_id=getattr(principal, "device_id", None),
        summary=_summary(action, version, fields), before_version=before, after_version=after,
        inbox_id=item.id if item is not None else None, inbox_before=inbox_before,
    )
    db.add(change)
    await db.flush()
    await audit.record(
        db, action=f"ledger.{action}", target_type="ledger_transaction", target_id=firefly_id, principal=principal,
        detail={
            "change_id": change.id, "group_id": change.group_id, "type": version["type"], "amount": _total(version),
            "fields": fields if action == "update" else [], "inbox_id": change.inbox_id,
        },
    )
    return change


@asynccontextmanager
async def _lock(key: str):
    """In-process serialization per key (the service runs a single uvicorn worker)."""
    lock = _LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        yield


async def _fetch(processor, firefly_id: str) -> dict:
    if not _ID.match(firefly_id or ""):
        raise _not_found()
    raw = await processor.firefly.get_transaction(firefly_id)
    if raw is None:
        raise _not_found()
    return raw


def firefly_rejection(exc: PermanentError) -> LedgerError:
    """A Firefly 4xx as a client error; the Firefly response body never leaves the server."""
    match = _REJECTION.match(str(exc))
    if match and match[1] == "404":
        return _not_found()
    return _invalid(f"Firefly rechazó el movimiento{f' ({match[1]})' if match else ''}.")


def public_error(exc: Exception) -> str:
    """Short Spanish text for a per-item failure (bulk and group undo); never an upstream body."""
    if isinstance(exc, LedgerError):
        return exc.detail
    if isinstance(exc, PermanentError):
        return firefly_rejection(exc).detail
    if isinstance(exc, FinanceAnalyzerError):
        return "Firefly no respondió; inténtalo de nuevo."
    return "Error inesperado; revisa el registro del servidor."


# --- Reads ---------------------------------------------------------------------------------------------------------------


async def get(db, processor, firefly_id: str) -> dict:
    raw = await _fetch(processor, firefly_id)
    version = normalize(raw, processor.zone)
    item = await _inbox_item(db, version["external_id"])
    return _public(version, raw["id"], item.id if item else None, processor.zone)


async def list_changes(db, *, limit: int, offset: int) -> tuple[list[LedgerChange], int]:
    total = await db.scalar(select(func.count()).select_from(LedgerChange))
    query = select(LedgerChange).order_by(LedgerChange.id.desc()).limit(limit).offset(offset).execution_options(populate_existing=True)
    return list((await db.scalars(query)).all()), total


# --- Writes --------------------------------------------------------------------------------------------------------------


async def _by_client_ref(db, ref: str) -> LedgerChange | None:
    return await db.scalar(select(LedgerChange).where(LedgerChange.client_ref == ref).execution_options(populate_existing=True))


def _replay(change: LedgerChange, zone) -> tuple[dict, LedgerChange]:
    return _public(change.after_version, change.firefly_id, change.inbox_id, zone), change


async def create(db, processor, draft: LedgerDraft, principal) -> tuple[dict, LedgerChange]:
    """Create a movement; repeating ``client_ref`` returns the first result and writes nothing."""
    ref = _opt(draft.client_ref)
    if ref is None or len(ref) > MAX_CLIENT_REF:
        raise _invalid(f"client_ref es obligatorio y admite hasta {MAX_CLIENT_REF} caracteres.")
    firefly = processor.firefly
    async with _lock(f"create:{ref}"):
        existing = await _by_client_ref(db, ref)
        if existing is not None:
            return _replay(existing, processor.zone)
        version = await resolve(processor, draft)
        version["external_id"] = f"ledger:{ref}"
        # Like the processor: a result committed by an earlier attempt that died before our commit is adopted, not duplicated.
        firefly_id = await firefly.find_by_external_id(version["external_id"]) or await firefly.create_transaction(_payload(version, processor.zone, update=False))
        raw = await firefly.get_transaction(str(firefly_id))
        after = normalize(raw, processor.zone) if raw else version
        try:
            change = await _record(db, principal, action="create", firefly_id=firefly_id, before=None, after=after, group_id=None, client_ref=ref)
        except IntegrityError:
            # Another worker recorded the same client_ref first; its result is the result.
            await db.rollback()
            existing = await _by_client_ref(db, ref)
            if existing is None:
                raise
            return _replay(existing, processor.zone)
        await db.commit()
        return _public(after, firefly_id, None, processor.zone), change


async def _write_update(db, processor, firefly_id: str, raw: dict, before: dict, target: dict, principal, group_id: str | None):
    """Replace the movement with ``target``; records the change and syncs the inbox item. The caller commits."""
    firefly = processor.firefly
    await firefly.update_transaction(firefly_id, _payload(target, processor.zone, _journals(raw), update=True))
    refreshed = await firefly.get_transaction(firefly_id)
    after = normalize(refreshed, processor.zone) if refreshed else target
    item = await _inbox_item(db, before["external_id"])
    inbox_before = None
    if item is not None:
        inbox_before = _inbox_snapshot(item)
        _inbox_sync(item, after)
        db.add(InboxEvent(inbox_id=item.id, action="ledger_update"))
    change = await _record(db, principal, action="update", firefly_id=firefly_id, before=before, after=after, group_id=group_id, item=item, inbox_before=inbox_before)
    if principal is not None:  # an owner's edit (or an applied suggestion), never the server's own automatic writes
        from app.services import budget_assign  # lazy: budget_assign writes through this module

        await budget_assign.learn_from_change(db, before, after, principal)
    return after, item, change


async def update(db, processor, firefly_id: str, draft: LedgerDraft, principal) -> tuple[dict, LedgerChange]:
    """Replace a whole movement with the splits of ``draft``."""
    raw = await _fetch(processor, firefly_id)
    before = normalize(raw, processor.zone)
    _require_editable(before)
    target = await resolve(processor, draft)
    target["external_id"] = before["external_id"]
    after, item, change = await _write_update(db, processor, firefly_id, raw, before, target, principal, None)
    await db.commit()
    return _public(after, raw["id"], item.id if item else None, processor.zone), change


async def retitle(db, processor, firefly_id: str, expected: str, description: str, principal, group_id: str | None = None) -> bool:
    """Replace the description of a single-split movement that still reads ``expected`` (its e-mail subject).

    False, with nothing written, when the movement has several splits, its description is no longer ``expected`` (the
    owner edited it) or nothing changes. Otherwise one audited, undoable change (``group_id`` groups a batch) and the inbox
    item is synchronized like any edit. Commits."""
    raw = await _fetch(processor, firefly_id)
    before = normalize(raw, processor.zone)
    _require_editable(before)
    if len(before["splits"]) != 1 or (before["description"] or "").strip() != expected.strip():
        return False
    draft = _draft_from(before, processor.zone)
    draft.description = description
    target = await resolve(processor, draft)
    target["external_id"] = before["external_id"]
    if _comparable(target) == _comparable(before):
        return False
    await _write_update(db, processor, firefly_id, raw, before, target, principal, group_id)
    await db.commit()
    return True


async def delete(db, processor, firefly_id: str, principal) -> LedgerChange:
    raw = await _fetch(processor, firefly_id)
    before = normalize(raw, processor.zone)
    _require_editable(before)
    await processor.firefly.delete_transaction(firefly_id)
    item = await _inbox_item(db, before["external_id"])
    inbox_before = None
    if item is not None:
        inbox_before = _inbox_snapshot(item)
        item.status = "voided"
        await processor.compact(item)
        db.add(InboxEvent(inbox_id=item.id, action="ledger_delete"))
    change = await _record(db, principal, action="delete", firefly_id=firefly_id, before=before, after=None, group_id=None, item=item, inbox_before=inbox_before)
    await db.commit()
    return change


async def _bulk_operations(processor, changes: LedgerBulkSet) -> dict:
    operations = {}
    category = changes.category.strip() if changes.category is not None else None
    if category is not None:
        if not category:
            raise _invalid("La categoría no puede estar vacía.")
        known = {name.casefold(): name for name in await processor.categories()}
        if category.casefold() not in known:
            raise _invalid(f"La categoría «{category}» no existe en Firefly.")
        operations["category"] = known[category.casefold()]
    budget = changes.budget.strip() if changes.budget is not None else None
    if budget is not None:
        if not budget:
            raise _invalid("El presupuesto no puede estar vacío.")
        known = await _named(processor.firefly.budgets)
        if budget.casefold() not in known:
            raise _invalid(f"El presupuesto «{budget}» no existe en Firefly.")
        operations["budget"] = known[budget.casefold()]["name"]
    source = changes.source_account.strip() if changes.source_account is not None else None
    if source is not None:
        operations["source_account"] = _required(source, "La cuenta de origen")
    if changes.tags_add:
        operations["tags_add"] = _clean_tags(changes.tags_add)
    if changes.tags_remove:
        operations["tags_remove"] = _clean_tags(changes.tags_remove)
    operations.update(await _edit_operations(processor, changes))
    if not operations:
        raise _invalid("Indica al menos un cambio.")
    return operations


async def _edit_operations(processor, changes: LedgerBulkSet) -> dict:
    """The extra single-movement changes of a ``LedgerEdit``: description, date, amount and merchant."""
    operations = {}
    if not isinstance(changes, LedgerEdit):
        return operations
    if changes.description is not None:
        operations["description"] = _required(changes.description, "La descripción")
    if changes.date is not None:
        operations["date"] = _date(changes.date, processor.zone)
    if changes.amount is not None:
        operations["amount"] = _amount(changes.amount, "El monto", processor.settings.max_amount_cop)
    if changes.merchant is not None:
        operations["merchant"] = _required(changes.merchant, "El comercio")
    return operations


def _apply_operations(draft: LedgerDraft, operations: dict) -> None:
    """Write ``operations`` into the draft rebuilt from the movement as it is now."""
    if "description" in operations:
        draft.description = operations["description"]
    if "date" in operations:
        draft.date = datetime.fromisoformat(operations["date"])
    if "amount" in operations:
        if len(draft.splits) != 1:
            raise _invalid("El monto solo se cambia en movimientos de una sola división.")
        draft.splits[0].amount = operations["amount"]
    counterparty_side = "source_account" if draft.type == "deposit" else "destination_account"
    if "merchant" in operations and draft.type == "transfer":
        raise _invalid("Una transferencia no tiene comercio.")
    for split in draft.splits:
        for key in ("category", "budget", "source_account"):
            if key in operations:
                setattr(split, key, operations[key])
        if "merchant" in operations:
            setattr(split, counterparty_side, operations["merchant"])
        removed = set(operations.get("tags_remove", ()))
        split.tags = [tag for tag in split.tags if tag not in removed]
        split.tags += [tag for tag in operations.get("tags_add", ()) if tag not in split.tags]


async def _bulk_one(db, processor, firefly_id: str, operations: dict, principal, group_id: str | None) -> LedgerChange | None:
    """Apply ``operations`` to one movement; the recorded change, or None when nothing would change."""
    raw = await _fetch(processor, firefly_id)
    before = normalize(raw, processor.zone)
    _require_editable(before)
    draft = _draft_from(before, processor.zone)
    _apply_operations(draft, operations)
    target = await resolve(processor, draft)
    target["external_id"] = before["external_id"]
    if _comparable(target) == _comparable(before):
        return None
    _, _, change = await _write_update(db, processor, firefly_id, raw, before, target, principal, group_id)
    await db.commit()
    return change


async def edit(db, processor, firefly_id: str, changes: LedgerEdit, principal) -> LedgerChange | None:
    """Change some fields of one movement (the same path as a bulk edit); None when it already has those values."""
    return await _bulk_one(db, processor, firefly_id, await _bulk_operations(processor, changes), principal, None)


async def _isolate(db, exc: Exception, label: str, **context) -> str:
    """Per-item failure of a batch operation: log, discard unsent work and return the public text."""
    if not isinstance(exc, LedgerError | FinanceAnalyzerError):
        await db.rollback()
    logger.warning(label, error_type=type(exc).__name__, **context)
    return public_error(exc)


async def bulk(db, processor, body: LedgerBulk, principal) -> tuple[str, list[str], list[dict]]:
    """Apply ``body.changes`` to every id; each movement is an independent change and all share ``group_id``."""
    ids = list(dict.fromkeys(value.strip() for value in body.ids))
    if not 1 <= len(ids) <= MAX_BULK:
        raise _invalid(f"Indica entre 1 y {MAX_BULK} movimientos.")
    operations = await _bulk_operations(processor, body.changes)
    group_id = uuid4().hex
    changed, failed = [], []
    for firefly_id in ids:
        try:
            if await _bulk_one(db, processor, firefly_id, operations, principal, group_id):
                changed.append(firefly_id)
        except Exception as exc:
            failed.append({"id": firefly_id, "detail": await _isolate(db, exc, "ledger_bulk_item_failed")})
    return group_id, changed, failed


# --- Undo --------------------------------------------------------------------------------------------------------------


async def _restore(processor, change: LedgerChange, current_id: str | None, raw: dict | None) -> tuple[str, dict]:
    """Put ``before_version`` back: update in place, or recreate a deleted movement (new id). Returns ``(firefly_id, after)``."""
    firefly, zone = processor.firefly, processor.zone
    before = change.before_version
    if change.action == "update":
        await firefly.update_transaction(current_id, _payload(before, zone, _journals(raw), update=True))
        firefly_id = current_id
    else:
        external_id = before.get("external_id")
        found = await firefly.find_by_external_id(external_id) if external_id else None
        firefly_id = str(found or await firefly.create_transaction(_payload(before, zone, update=False)))
    refreshed = await firefly.get_transaction(firefly_id)
    return firefly_id, normalize(refreshed, zone) if refreshed else before


async def _undo(db, processor, change: LedgerChange, principal) -> dict | None:
    """Revert one change; stages everything and returns the resulting transaction (None after undoing a create)."""
    if change.undone_at is not None:
        raise _conflict("Este cambio ya se deshizo.")
    if now_utc() > _undo_deadline(change):
        raise LedgerError(409, "revert_expired", "Pasaron más de 7 días: ya no se puede deshacer este cambio.")
    firefly, zone = processor.firefly, processor.zone
    raw = await firefly.get_transaction(change.firefly_id)
    transaction, firefly_id, inbox_id = None, change.firefly_id, change.inbox_id
    try:
        if change.action == "delete":
            if raw is not None:
                raise _conflict("El movimiento ya existe en Firefly; no hay nada que restaurar.")
            firefly_id, after = await _restore(processor, change, None, None)
        else:
            if raw is None or _comparable(normalize(raw, zone)) != _comparable(change.after_version):
                raise _conflict("El movimiento cambió o ya no existe; no se puede deshacer sin perder esos cambios.")
            if change.action == "create":
                await firefly.delete_transaction(change.firefly_id)
                after = None
            else:
                _, after = await _restore(processor, change, change.firefly_id, raw)
    except PermanentError as exc:
        raise _conflict("Firefly rechazó la restauración del movimiento.") from exc
    item = await db.get(InboxItem, inbox_id, populate_existing=True) if inbox_id is not None else None
    if item is not None and change.inbox_before is not None:
        _inbox_restore(item, change.inbox_before)
        if change.action == "delete":
            item.status, item.firefly_transaction_id = change.inbox_before["status"], firefly_id
        db.add(InboxEvent(inbox_id=item.id, action="ledger_undo"))
    if after is not None:
        transaction = _public(after, firefly_id, item.id if item else None, zone)
    change.undone_at = now_utc()
    await audit.record(
        db, action="ledger.undo", target_type="ledger_transaction", target_id=change.firefly_id, principal=principal,
        detail={"change_id": change.id, "group_id": change.group_id, "undone_action": change.action, "firefly_id": firefly_id},
    )
    return transaction


async def undo(db, processor, change_id: int, principal) -> tuple[LedgerChange, dict | None]:
    if await db.get(LedgerChange, change_id) is None:
        raise LedgerError(404, "not_found", "No existe ese cambio.")
    async with _lock(f"undo:{change_id}"):
        change = await db.get(LedgerChange, change_id, populate_existing=True)
        transaction = await _undo(db, processor, change, principal)
        await db.commit()
    return change, transaction


async def undo_group(db, processor, group_id: str, principal) -> tuple[list[int], list[dict]]:
    """Undo every pending change of the group, newest first. Failures are reported per change; the rest still go through."""
    query = select(LedgerChange).where(LedgerChange.group_id == group_id).order_by(LedgerChange.id.desc()).execution_options(populate_existing=True)
    ids = [change.id for change in (await db.scalars(query)).all()]
    if not ids:
        raise LedgerError(404, "not_found", "No existe ese grupo de cambios.")
    undone, failed = [], []
    for change_id in ids:
        try:
            async with _lock(f"undo:{change_id}"):
                change = await db.get(LedgerChange, change_id, populate_existing=True)
                if change.undone_at is not None:
                    continue
                await _undo(db, processor, change, principal)
                await db.commit()
        except Exception as exc:
            failed.append({"id": change_id, "detail": await _isolate(db, exc, "ledger_undo_failed", change_id=change_id)})
        else:
            undone.append(change_id)
    return undone, failed
