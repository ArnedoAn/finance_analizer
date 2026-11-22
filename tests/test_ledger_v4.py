"""Manual ledger editing (contract section 15) over a stateful stand-in for Firefly III's transaction endpoints."""
import copy
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.api.dependencies import hash_secret
from app.core.exceptions import FireflyError, PermanentError
from app.core.timezone import now_utc
from app.db.models import InboxEvent
from app.db.models_devices import Device
from app.db.models_ledger import LedgerChange
from app.services import audit
from app.services import ledger as ledger_service

ADMIN = {"X-API-Key": "test-admin-key-01234567890123456789"}
API = "/api/v1/ledger"
OWN = {"asset", "liability", "liabilities"}


class FireflyLedger:
    """Stateful Firefly III stand-in installed on the ``AsyncMock`` of ``conftest.py``.

    It stores transaction groups the way Firefly answers them (12-decimal amounts, ``*_name`` next to ``*_id``,
    journals with ``transaction_journal_id``) and enforces the rules the mapping must honour, so a wrong payload
    fails the test instead of being stored: ``group_title`` with several splits, all keys present on updates
    (``null`` clears), account types per transaction type, one source/destination across splits, and that
    journals missing from an update are deleted.
    """

    def __init__(self, firefly, accounts, budgets, bills):
        self.accounts = {str(account["id"]): dict(account) for account in accounts}
        self.budgets, self.bills = dict(budgets), dict(bills)
        self.groups = {}
        self.requests = []  # (verb, id, payload) of every write the service sent
        self.created_accounts = []
        self._group, self._journal, self._account = 100, 1000, 50
        firefly.budgets.return_value = [{"id": ident, "name": name} for ident, name in budgets.items()]
        firefly.bills.return_value = [{"id": ident, "name": name} for ident, name in bills.items()]
        firefly.get_transaction.side_effect = self.get_transaction
        firefly.create_transaction.side_effect = self.create_transaction
        firefly.update_transaction.side_effect = self.update_transaction
        firefly.delete_transaction.side_effect = self.delete_transaction
        firefly.find_by_external_id.side_effect = self.find_by_external_id
        firefly.create_account.side_effect = self.create_account

    # -- state helpers used by the tests ---------------------------------------------------------------------------

    def seed(self, *, kind="withdrawal", amount="12000.00", source="1", destination="2", date="2026-11-06T12:00:00-05:00", description="Compra", external_id=None, **extra):
        """A movement that already lives in the ledger (not written through the service)."""
        split = {"type": kind, "date": date, "amount": amount, "description": description, "source_id": source, "destination_id": destination, **extra}
        if external_id:
            split["external_id"] = external_id
        return self.add({"transactions": [split]})

    def journals(self, group_id):
        return self.groups[str(group_id)]["attributes"]["transactions"]

    def writes(self, verb):
        return [entry for entry in self.requests if entry[0] == verb]

    # -- the Firefly client surface -----------------------------------------------------------------------------------

    async def get_transaction(self, transaction_id):
        group = self.groups.get(str(transaction_id))
        return copy.deepcopy(group) if group else None

    async def create_transaction(self, payload):
        self._check(payload, update=False)
        self.requests.append(("create", None, copy.deepcopy(payload)))
        return self.add(payload)

    async def update_transaction(self, transaction_id, payload):
        group = self.groups.get(str(transaction_id))
        if group is None:
            raise PermanentError("Firefly 404: Not Found")
        self._check(payload, update=True)
        self.requests.append(("update", str(transaction_id), copy.deepcopy(payload)))
        existing = {journal["transaction_journal_id"]: journal for journal in group["attributes"]["transactions"]}
        journals = []
        for split in payload["transactions"]:
            ident = split.get("transaction_journal_id")
            if ident is not None and ident not in existing:
                raise PermanentError("Firefly 422: unknown journal")
            journals.append(self._build(split, existing.get(ident)))
        # Journals the update does not mention are deleted, as Firefly does.
        group["attributes"]["transactions"] = journals
        if "group_title" in payload:
            group["attributes"]["group_title"] = payload["group_title"]
        return str(transaction_id)

    async def delete_transaction(self, transaction_id):
        if self.groups.pop(str(transaction_id), None) is None:
            raise PermanentError("Firefly 404: Not Found")
        self.requests.append(("delete", str(transaction_id), None))

    async def find_by_external_id(self, external_id):
        for group_id, group in self.groups.items():
            if any(journal.get("external_id") == external_id for journal in group["attributes"]["transactions"]):
                return group_id
        return None

    async def create_account(self, name, type):
        self._account += 1
        account = {"id": str(self._account), "name": name, "type": type}
        self.accounts[account["id"]] = account
        self.created_accounts.append((name, type))
        return dict(account)

    # -- internals ----------------------------------------------------------------------------------------------------

    def add(self, payload):
        self._group += 1
        group_id = str(self._group)
        self.groups[group_id] = {
            "type": "transactions", "id": group_id,
            "attributes": {"group_title": payload.get("group_title"), "transactions": [self._build(split) for split in payload["transactions"]]},
        }
        return group_id

    def _account_of(self, split, side):
        ident, name = split.get(f"{side}_id"), split.get(f"{side}_name")
        if ident is not None:
            account = self.accounts.get(str(ident))
        else:
            account = next((row for row in self.accounts.values() if row["name"] == name), None)
        if account is None:
            raise PermanentError(f"Firefly 422: unknown {side} account")
        return account

    def _check(self, payload, *, update):
        splits = payload["transactions"]
        assert 1 <= len(splits) <= 20
        if len(splits) > 1:
            assert payload.get("group_title"), "split transactions need a group_title"
        else:
            assert "group_title" not in payload
        kinds = {split["type"] for split in splits}
        assert len(kinds) == 1, kinds
        kind = kinds.pop()
        sources, destinations = set(), set()
        for split in splits:
            assert re.fullmatch(r"\d+\.\d{2}", split["amount"]), split["amount"]
            assert split["date"].endswith("-05:00"), split["date"]
            assert split["currency_code"] == "COP"
            assert (split.get("foreign_amount") is None) == (split.get("foreign_currency_code") is None)
            if update:
                needed = {"category_name", "notes", "tags", "budget_id", "bill_id", "foreign_amount", "foreign_currency_code"}
                assert needed <= split.keys(), needed - split.keys()
            else:
                assert all(value is not None for value in split.values()), split
            source, destination = self._account_of(split, "source"), self._account_of(split, "destination")
            allowed_source, allowed_destination = {"withdrawal": (OWN, {"expense"}), "deposit": ({"revenue"}, OWN), "transfer": (OWN, OWN)}[kind]
            if source["type"] not in allowed_source or destination["type"] not in allowed_destination:
                raise PermanentError(f"Firefly 422: invalid accounts for a {kind}")
            sources.add(source["id"])
            destinations.add(destination["id"])
            for key, known in (("budget_id", self.budgets), ("bill_id", self.bills)):
                if split.get(key) is not None:
                    assert str(split[key]) in known, (key, split[key])
        if kind in ("withdrawal", "transfer") and len(sources) > 1:
            raise PermanentError("Firefly 422: split withdrawals need one source")
        if kind in ("deposit", "transfer") and len(destinations) > 1:
            raise PermanentError("Firefly 422: split deposits need one destination")

    def _build(self, split, base=None):
        if base is None:
            self._journal += 1
            journal = {
                "transaction_journal_id": str(self._journal), "external_id": None, "currency_code": "COP", "foreign_amount": None,
                "foreign_currency_code": None, "category_name": None, "budget_id": None, "budget_name": None, "bill_id": None,
                "bill_name": None, "tags": [], "notes": None,
            }
        else:
            journal = copy.deepcopy(base)
        for key in ("type", "date", "description", "external_id", "category_name", "notes", "currency_code", "foreign_currency_code"):
            if key in split:
                journal[key] = split[key]
        if "tags" in split:
            journal["tags"] = list(split["tags"])
        if "amount" in split:
            journal["amount"] = f"{Decimal(split['amount']):.12f}"
        if "foreign_amount" in split:
            journal["foreign_amount"] = None if split["foreign_amount"] is None else f"{Decimal(split['foreign_amount']):.12f}"
        for side in ("source", "destination"):
            if f"{side}_id" in split or f"{side}_name" in split:
                account = self._account_of(split, side)
                journal[f"{side}_id"], journal[f"{side}_name"], journal[f"{side}_type"] = account["id"], account["name"], account["type"]
        for key, known in (("budget", self.budgets), ("bill", self.bills)):
            if f"{key}_id" in split:
                ident = split[f"{key}_id"]
                journal[f"{key}_id"] = None if ident is None else str(ident)
                journal[f"{key}_name"] = None if ident is None else known[str(ident)]
        return journal


@pytest.fixture
def fake(clients):
    firefly, _ = clients
    firefly.accounts.return_value = [*firefly.accounts.return_value, {"id": "6", "name": "Tarjeta Nu", "type": "liabilities"}]
    firefly.categories.return_value = ["Alimentación", "Transporte", "Ocio"]
    return FireflyLedger(firefly, firefly.accounts.return_value, budgets={"11": "Mercado", "12": "Ocio"}, bills={"21": "Internet", "22": "Arriendo"})


# --- helpers ---------------------------------------------------------------------------------------------------------


def line(**changes):
    return {"amount": "12000.00", "source_account": "Nequi", "destination_account": "SUPERMERCADO CENTRAL", **changes}


def body(**changes):
    return {
        "client_ref": uuid4().hex[:16], "type": "withdrawal", "date": "2026-11-06T12:00:00-05:00",
        "description": "Mercado de la semana", "splits": [line()], **changes,
    }


def put_body(**changes):
    return {key: value for key, value in body(**changes).items() if key != "client_ref"}


async def create(client, **changes):
    response = await client.post(f"{API}/transactions", headers=ADMIN, json=body(**changes))
    assert response.status_code == 201, response.text
    return response.json()


async def edit(client, firefly_id, **changes):
    response = await client.put(f"{API}/transactions/{firefly_id}", headers=ADMIN, json=put_body(**changes))
    assert response.status_code == 200, response.text
    return response.json()


async def undo(client, change_id, status=200):
    response = await client.post(f"{API}/changes/{change_id}/undo", headers=ADMIN)
    assert response.status_code == status, response.text
    return response.json()


async def device(db, name, scopes):
    key = f"{name}-key-" + "0123456789" * 3
    db.add(Device(name=name, key_hash=hash_secret(key), scopes=scopes))
    await db.commit()
    return {"X-API-Key": key}


async def change_rows(db):
    return list((await db.scalars(select(LedgerChange).order_by(LedgerChange.id))).all())


async def make_inbox(service, **changes):
    values = dict(
        source="sms", source_ref="sms-1", sender="85954", occurred_at=datetime(2026, 11, 6, 17, 0, tzinfo=UTC), status="created",
        method="parser", transaction_type="withdrawal", amount=Decimal("12000.00"), currency="COP", merchant="SUPERMERCADO CENTRAL",
        asset_account="Nequi", category="Alimentación", tags=[],
    )
    values.update(changes)
    row = await service.inbox.add(**values)
    await service.session.commit()
    return row


def error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["code"] == code, response.text
    return response.json()["detail"]


# --- create ----------------------------------------------------------------------------------------------------------


async def test_create_maps_the_draft_to_firefly_and_records_the_change(client, fake, db):
    draft = body(client_ref="abc-1", notes="Para la semana", splits=[line(source_account="nequi", category="alimentación", budget="mercado", bill="internet", tags=["casa", "semana"])])
    response = await client.post(f"{API}/transactions", headers=ADMIN, json=draft)
    assert response.status_code == 201, response.text
    transaction, change = response.json()["transaction"], response.json()["change"]

    assert [(verb, ident) for verb, ident, _ in fake.requests] == [("create", None)]
    assert fake.requests[0][2] == {"transactions": [{
        "type": "withdrawal", "date": "2026-11-06T12:00:00-05:00", "amount": "12000.00", "currency_code": "COP",
        "description": "Mercado de la semana", "category_name": "Alimentación", "tags": ["casa", "semana"], "notes": "Para la semana",
        "source_id": "1", "destination_id": "2", "budget_id": "11", "bill_id": "21", "external_id": "ledger:abc-1",
    }]}
    assert transaction == {
        "id": transaction["id"], "type": "withdrawal", "date": "2026-11-06T12:00:00-05:00", "description": "Mercado de la semana",
        "notes": "Para la semana", "external_id": "ledger:abc-1", "inbox_id": None,
        "splits": [{
            "amount": "12000.00", "currency": "COP", "foreign_amount": None, "foreign_currency": None, "source_account": "Nequi",
            "destination_account": "SUPERMERCADO CENTRAL", "category": "Alimentación", "budget": "Mercado", "bill": "Internet",
            "tags": ["casa", "semana"], "notes": None, "description": None,
        }],
    }
    assert transaction["id"] in fake.groups
    assert set(change) == {"id", "group_id", "action", "firefly_id", "at", "actor", "summary", "undone_at", "undo_deadline"}
    assert change["action"] == "create" and change["firefly_id"] == transaction["id"] and change["actor"] == "admin"
    assert change["summary"] == "Creaste un gasto de $12.000 COP" and change["undone_at"] is None and change["group_id"]
    assert datetime.fromisoformat(change["undo_deadline"]) - datetime.fromisoformat(change["at"]) == timedelta(days=7)

    read = await client.get(f"{API}/transactions/{transaction['id']}", headers=ADMIN)
    assert read.status_code == 200 and read.json() == transaction

    [row] = await change_rows(db)
    assert row.client_ref == "abc-1" and row.before_version is None and row.undone_at is None
    assert row.after_version["splits"][0]["refs"] == {"source": "1", "destination": "2", "budget": "11", "bill": "21"}
    entries, total = await audit.entries(db, action="ledger.create")
    assert total == 1 and entries[0].target_id == transaction["id"] and entries[0].actor == "admin"
    assert "Para la semana" not in json.dumps(entries[0].detail) and "Mercado de la semana" not in json.dumps(entries[0].detail)


@pytest.mark.parametrize(("kind", "source", "destination", "ids"), [
    ("withdrawal", "Nequi", "SUPERMERCADO CENTRAL", ("1", "2")),
    ("withdrawal", "Tarjeta Nu", "SUPERMERCADO CENTRAL", ("6", "2")),
    ("deposit", "SALARIO", "Bancolombia", ("3", "4")),
    ("transfer", "Nequi", "Tarjeta Nu", ("1", "6")),
])
async def test_each_type_maps_its_accounts(client, fake, kind, source, destination, ids):
    await create(client, type=kind, splits=[line(source_account=source, destination_account=destination)])
    [(_, _, payload)] = fake.writes("create")
    assert payload["transactions"][0]["type"] == kind
    assert (payload["transactions"][0]["source_id"], payload["transactions"][0]["destination_id"]) == ids
    assert not fake.created_accounts


async def test_create_is_idempotent_by_client_ref(client, fake, db):
    first = await create(client, client_ref="same")
    again = await client.post(f"{API}/transactions", headers=ADMIN, json=body(client_ref="same", description="Otro texto", splits=[line(amount="99.00")]))
    assert again.status_code == 201
    assert again.json() == first
    assert len(fake.writes("create")) == 1 and len(fake.groups) == 1
    assert len(await change_rows(db)) == 1
    other = await create(client, client_ref="other")
    assert other["transaction"]["id"] != first["transaction"]["id"] and len(fake.groups) == 2


async def test_create_adopts_a_movement_an_earlier_attempt_already_wrote(client, fake, db):
    existing = fake.seed(external_id="ledger:crashed")
    result = await create(client, client_ref="crashed")
    assert result["transaction"]["id"] == existing and result["change"]["firefly_id"] == existing
    assert not fake.writes("create") and len(fake.groups) == 1
    assert len(await change_rows(db)) == 1


async def test_create_race_on_client_ref_returns_the_recorded_result(client, fake, monkeypatch):
    first = await create(client, client_ref="race")
    real = ledger_service._by_client_ref
    calls = []

    async def blind_once(db, ref):
        calls.append(ref)
        return None if len(calls) == 1 else await real(db, ref)

    monkeypatch.setattr(ledger_service, "_by_client_ref", blind_once)
    again = await client.post(f"{API}/transactions", headers=ADMIN, json=body(client_ref="race"))
    assert again.status_code == 201 and again.json() == first
    assert len(calls) == 2 and len(fake.writes("create")) == 1


async def test_new_merchant_and_income_accounts_are_created_but_never_own_accounts(client, fake):
    result = await create(client, splits=[line(destination_account="Tienda Nueva")])
    assert fake.created_accounts == [("Tienda Nueva", "expense")]
    assert result["transaction"]["splits"][0]["destination_account"] == "Tienda Nueva"
    await create(client, type="deposit", splits=[{"amount": "100000.00", "source_account": "Prima", "destination_account": "Bancolombia"}])
    assert fake.created_accounts == [("Tienda Nueva", "expense"), ("Prima", "revenue")]
    await create(client, splits=[line(destination_account="tienda nueva")])
    assert len(fake.created_accounts) == 2

    rejected = await client.post(f"{API}/transactions", headers=ADMIN, json=body(splits=[line(source_account="Cuenta Fantasma")]))
    assert "Cuenta Fantasma" in error(rejected, 422, "validation_error")
    assert len(fake.created_accounts) == 2 and all(account["name"] != "Cuenta Fantasma" for account in fake.accounts.values())


async def test_nothing_is_created_when_a_later_split_is_invalid(client, fake, db):
    response = await client.post(f"{API}/transactions", headers=ADMIN, json=body(splits=[line(destination_account="Comercio Nuevo"), line(category="Inexistente")]))
    assert error(response, 422, "validation_error").startswith("División 2: ")
    assert fake.created_accounts == [] and fake.requests == [] and await change_rows(db) == []


async def test_firefly_failures_leave_no_record_and_the_retry_works(client, clients, fake, db):
    firefly, _ = clients
    firefly.create_transaction.side_effect = FireflyError("down")
    draft = body(client_ref="flaky")
    error(await client.post(f"{API}/transactions", headers=ADMIN, json=draft), 502, "upstream_unavailable")
    assert await change_rows(db) == []

    firefly.create_transaction.side_effect = PermanentError("Firefly 422: secret upstream body {'x': 1}")
    response = await client.post(f"{API}/transactions", headers=ADMIN, json=draft)
    assert error(response, 422, "validation_error") == "Firefly rechazó el movimiento (422)."
    assert "secret" not in response.text and await change_rows(db) == []

    firefly.create_transaction.side_effect = fake.create_transaction
    retry = await client.post(f"{API}/transactions", headers=ADMIN, json=draft)
    assert retry.status_code == 201 and len(fake.groups) == 1


# --- read --------------------------------------------------------------------------------------------------------------


async def test_get_normalizes_what_firefly_returns(client, fake):
    fake.groups["900"] = {"type": "transactions", "id": "900", "attributes": {"group_title": None, "transactions": [{
        "transaction_journal_id": "9001", "type": "withdrawal", "date": "2026-11-06T17:00:00Z", "amount": "12000.000000000000",
        "currency_code": "COP", "foreign_amount": "0", "foreign_currency_code": None, "description": "Compra", "source_id": "1",
        "source_name": "Nequi", "destination_id": "2", "destination_name": "SUPERMERCADO CENTRAL", "category_name": None,
        "budget_id": "0", "budget_name": None, "bill_id": None, "bill_name": None, "tags": [], "notes": None, "external_id": None,
    }]}}
    fake.groups["901"] = {"type": "transactions", "id": "901", "attributes": {"group_title": "Viaje", "transactions": [
        {"transaction_journal_id": "9011", "type": "withdrawal", "date": "2026-11-06T12:00:00-05:00", "amount": "30000.000000000000", "currency_code": "COP",
         "foreign_amount": "5.000000000000", "foreign_currency_code": "USD", "description": "Hotel", "source_id": "1", "source_name": "Nequi",
         "destination_id": "2", "destination_name": "SUPERMERCADO CENTRAL", "category_name": "Ocio", "tags": ["viaje"], "notes": "Reserva", "external_id": "x-1"},
        {"transaction_journal_id": "9012", "type": "withdrawal", "date": "2026-11-06T12:00:00-05:00", "amount": "10000.000000000000", "currency_code": "COP",
         "description": "Viaje", "source_id": "1", "source_name": "Nequi", "destination_id": "2", "destination_name": "SUPERMERCADO CENTRAL", "tags": [], "notes": "Reserva"},
    ]}}
    simple = (await client.get(f"{API}/transactions/900", headers=ADMIN)).json()
    assert simple["date"] == "2026-11-06T12:00:00-05:00" and simple["description"] == "Compra" and simple["external_id"] is None
    assert simple["splits"][0] == {
        "amount": "12000.00", "currency": "COP", "foreign_amount": None, "foreign_currency": None, "source_account": "Nequi",
        "destination_account": "SUPERMERCADO CENTRAL", "category": None, "budget": None, "bill": None, "tags": [], "notes": None, "description": None,
    }
    split = (await client.get(f"{API}/transactions/901", headers=ADMIN)).json()
    assert split["description"] == "Viaje" and split["notes"] == "Reserva" and split["external_id"] == "x-1"
    assert [part["description"] for part in split["splits"]] == ["Hotel", None]
    assert [part["notes"] for part in split["splits"]] == [None, None]
    assert split["splits"][0]["foreign_amount"] == "5.00" and split["splits"][0]["foreign_currency"] == "USD"


async def test_get_unknown_or_malformed_ids_are_404(client, fake):
    assert error(await client.get(f"{API}/transactions/777", headers=ADMIN), 404, "not_found")
    assert error(await client.get(f"{API}/transactions/a.b", headers=ADMIN), 404, "not_found")
    assert error(await client.put(f"{API}/transactions/777", headers=ADMIN, json=put_body()), 404, "not_found")
    assert error(await client.delete(f"{API}/transactions/777", headers=ADMIN), 404, "not_found")
    assert error(await client.post(f"{API}/changes/999/undo", headers=ADMIN), 404, "not_found")
    assert error(await client.post(f"{API}/groups/nope/undo", headers=ADMIN), 404, "not_found")


async def test_movements_of_other_types_are_readable_but_not_editable(client, fake):
    fid = fake.seed(kind="opening balance")
    assert (await client.get(f"{API}/transactions/{fid}", headers=ADMIN)).json()["type"] == "opening balance"
    assert "no se puede editar" in error(await client.put(f"{API}/transactions/{fid}", headers=ADMIN, json=put_body()), 422, "validation_error")
    assert "no se puede editar" in error(await client.delete(f"{API}/transactions/{fid}", headers=ADMIN), 422, "validation_error")
    assert fid in fake.groups and fake.requests == []


# --- update ----------------------------------------------------------------------------------------------------------


async def test_update_with_splits_and_foreign_currency_then_back_to_one_split(client, fake, db):
    created = await create(client, client_ref="edit-1", notes="Nota común", splits=[line(category="Alimentación", tags=["casa"])])
    fid = created["transaction"]["id"]
    journal = fake.journals(fid)[0]["transaction_journal_id"]

    splits = [
        line(amount="30000.00", category="Alimentación", description="Mercado", tags=["casa"]),
        line(amount="20000.00", destination_account="TAXI LIBRE", category="Transporte", foreign_amount="5.00", foreign_currency="usd", notes="Corrida", budget="Ocio"),
    ]
    result = await edit(client, fid, description="Salida del sábado", notes=None, splits=splits)
    [(_, sent_id, payload)] = fake.writes("update")
    first, second = payload["transactions"]
    assert sent_id == fid and payload["group_title"] == "Salida del sábado"
    assert first["transaction_journal_id"] == journal and "transaction_journal_id" not in second
    assert first["external_id"] == "ledger:edit-1" and "external_id" not in second
    assert (first["foreign_amount"], first["foreign_currency_code"], first["budget_id"], first["bill_id"], first["notes"]) == (None, None, None, None, None)
    assert (second["foreign_amount"], second["foreign_currency_code"], second["budget_id"], second["notes"]) == ("5.00", "USD", "12", "Corrida")
    assert (first["description"], second["description"]) == ("Mercado", "Salida del sábado")
    assert (second["destination_id"], second["category_name"]) == ("51", "Transporte") and fake.created_accounts == [("TAXI LIBRE", "expense")]

    transaction = result["transaction"]
    assert transaction["id"] == fid and transaction["description"] == "Salida del sábado" and transaction["notes"] is None
    assert transaction["external_id"] == "ledger:edit-1"
    assert [part["amount"] for part in transaction["splits"]] == ["30000.00", "20000.00"]
    assert [part["description"] for part in transaction["splits"]] == ["Mercado", None]
    assert [part["notes"] for part in transaction["splits"]] == [None, "Corrida"]
    assert transaction["splits"][1]["foreign_amount"] == "5.00" and transaction["splits"][1]["foreign_currency"] == "USD"
    assert transaction["splits"][1]["budget"] == "Ocio" and transaction["splits"][1]["destination_account"] == "TAXI LIBRE"
    assert (await client.get(f"{API}/transactions/{fid}", headers=ADMIN)).json() == transaction
    assert len(fake.journals(fid)) == 2 and fake.groups[fid]["attributes"]["group_title"] == "Salida del sábado"
    assert result["change"]["action"] == "update" and result["change"]["firefly_id"] == fid
    assert result["change"]["summary"] == "Editaste un gasto de $50.000 COP (descripción, notas, divisiones)"

    rows = await change_rows(db)
    assert len(rows[1].before_version["splits"]) == 1 and len(rows[1].after_version["splits"]) == 2
    assert rows[1].before_version["notes"] == "Nota común"

    # Back to a single plain split: the second journal disappears, every optional field is cleared explicitly.
    await edit(client, fid)
    last = fake.writes("update")[-1][2]
    assert "group_title" not in last and len(last["transactions"]) == 1
    assert last["transactions"][0]["transaction_journal_id"] == journal and last["transactions"][0]["external_id"] == "ledger:edit-1"
    [remaining] = fake.journals(fid)
    assert remaining["transaction_journal_id"] == journal and remaining["external_id"] == "ledger:edit-1"
    assert (remaining["category_name"], remaining["budget_id"], remaining["bill_id"], remaining["notes"], remaining["tags"], remaining["foreign_amount"]) == (None, None, None, None, [], None)
    assert remaining["amount"] == "12000.000000000000" and remaining["description"] == "Mercado de la semana"


async def test_invalid_update_writes_nothing(client, fake, db):
    fid = (await create(client))["transaction"]["id"]
    response = await client.put(f"{API}/transactions/{fid}", headers=ADMIN, json=put_body(splits=[line(category="Inexistente")]))
    assert "categoría" in error(response, 422, "validation_error")
    assert fake.writes("update") == [] and len(await change_rows(db)) == 1


# --- delete ----------------------------------------------------------------------------------------------------------


async def test_delete_removes_the_movement_and_records_it(client, fake, db):
    fid = (await create(client, notes="n"))["transaction"]["id"]
    response = await client.delete(f"{API}/transactions/{fid}", headers=ADMIN)
    assert response.status_code == 200 and set(response.json()) == {"change"}
    change = response.json()["change"]
    assert change["action"] == "delete" and change["firefly_id"] == fid and change["summary"] == "Eliminaste un gasto de $12.000 COP"
    assert fid not in fake.groups and fake.writes("delete") == [("delete", fid, None)]
    error(await client.get(f"{API}/transactions/{fid}", headers=ADMIN), 404, "not_found")
    error(await client.delete(f"{API}/transactions/{fid}", headers=ADMIN), 404, "not_found")
    row = (await change_rows(db))[1]
    assert row.after_version is None and row.before_version["splits"][0]["amount"] == "12000.00"


# --- undo ----------------------------------------------------------------------------------------------------------------


async def test_undo_create_deletes_the_movement(client, fake, db):
    created = await create(client)
    fid, cid = created["transaction"]["id"], created["change"]["id"]
    result = await undo(client, cid)
    assert result["transaction"] is None and result["change"]["id"] == cid and result["change"]["undone_at"] is not None
    assert fid not in fake.groups
    assert (await change_rows(db))[0].undone_at is not None


async def test_undo_update_restores_the_previous_version_even_across_splits(client, fake):
    created = await create(client, client_ref="u-1", notes="Nota", splits=[line(category="Alimentación", tags=["a", "b"])])
    fid = created["transaction"]["id"]
    updated = await edit(client, fid, description="Otra cosa", splits=[line(amount="1000.00"), line(amount="2000.00", destination_account="Tienda Nueva")])
    assert len(fake.journals(fid)) == 2
    restored = await undo(client, updated["change"]["id"])
    assert restored["transaction"] == created["transaction"] and restored["change"]["undone_at"] is not None
    assert (await client.get(f"{API}/transactions/{fid}", headers=ADMIN)).json() == created["transaction"]
    [journal] = fake.journals(fid)
    assert journal["external_id"] == "ledger:u-1" and journal["tags"] == ["a", "b"] and journal["notes"] == "Nota"


async def test_undo_delete_recreates_the_movement_with_a_new_id(client, fake, db):
    created = await create(client, client_ref="gone", notes="n", splits=[line(category="Transporte", tags=["a"], budget="Mercado")])
    old = created["transaction"]["id"]
    deleted = (await client.delete(f"{API}/transactions/{old}", headers=ADMIN)).json()["change"]
    result = await undo(client, deleted["id"])
    new = result["transaction"]["id"]
    assert new != old and old not in fake.groups and new in fake.groups
    assert result["transaction"] == {**created["transaction"], "id": new}
    assert fake.journals(new)[0]["external_id"] == "ledger:gone" and result["change"]["undone_at"] is not None
    assert (await client.get(f"{API}/transactions/{new}", headers=ADMIN)).json() == result["transaction"]
    # The restored movement is a new one: the original creation can no longer be undone.
    assert error(await client.post(f"{API}/changes/{created['change']['id']}/undo", headers=ADMIN), 409, "conflict")
    assert len(await change_rows(db)) == 2


async def test_a_change_can_only_be_undone_once(client, fake):
    created = await create(client)
    await undo(client, created["change"]["id"])
    detail = error(await client.post(f"{API}/changes/{created['change']['id']}/undo", headers=ADMIN), 409, "conflict")
    assert "ya se deshizo" in detail


async def test_undo_expires_after_seven_days(client, fake, db):
    old = await create(client)
    recent = await create(client)
    (first, second) = await change_rows(db)
    first.at = now_utc() - timedelta(days=8)
    second.at = now_utc() - timedelta(days=6, hours=23)
    await db.commit()
    detail = error(await client.post(f"{API}/changes/{old['change']['id']}/undo", headers=ADMIN), 409, "revert_expired")
    assert "7 días" in detail and old["transaction"]["id"] in fake.groups
    assert (await undo(client, recent["change"]["id"]))["transaction"] is None


async def test_undo_is_a_conflict_when_the_movement_changed_afterwards(client, fake, db):
    created = await create(client)
    fid = created["transaction"]["id"]
    fake.journals(fid)[0]["category_name"] = "Transporte"  # edited directly in Firefly
    detail = error(await client.post(f"{API}/changes/{created['change']['id']}/undo", headers=ADMIN), 409, "conflict")
    assert "cambió" in detail and fid in fake.groups and (await change_rows(db))[0].undone_at is None

    updated = await edit(client, fid, description="Nueva")
    fake.journals(fid)[0]["notes"] = "alguien más"
    error(await client.post(f"{API}/changes/{updated['change']['id']}/undo", headers=ADMIN), 409, "conflict")
    assert fake.journals(fid)[0]["notes"] == "alguien más" and fake.journals(fid)[0]["description"] == "Nueva"

    del fake.groups[fid]  # gone from Firefly altogether
    error(await client.post(f"{API}/changes/{updated['change']['id']}/undo", headers=ADMIN), 409, "conflict")


async def test_undo_ignores_the_order_of_tags(client, fake):
    created = await create(client, splits=[line(tags=["a", "b"])])
    fake.journals(created["transaction"]["id"])[0]["tags"] = ["b", "a"]
    assert (await undo(client, created["change"]["id"]))["transaction"] is None


async def test_undo_delete_is_a_conflict_when_firefly_rejects_the_restoration(client, fake, db):
    created = await create(client)
    deleted = (await client.delete(f"{API}/transactions/{created['transaction']['id']}", headers=ADMIN)).json()["change"]
    del fake.accounts["2"]  # the merchant account disappeared from Firefly
    detail = error(await client.post(f"{API}/changes/{deleted['id']}/undo", headers=ADMIN), 409, "conflict")
    assert "rechazó" in detail and (await change_rows(db))[1].undone_at is None and fake.groups == {}


# --- bulk ---------------------------------------------------------------------------------------------------------------


async def test_bulk_edit_shares_one_group_and_the_group_undo_restores_everything(client, fake, db):
    b1 = (await create(client, splits=[line(tags=["viejo"])]))["transaction"]["id"]
    b2 = (await create(client, splits=[line(category="Transporte", tags=["viejo", "otro"])]))["transaction"]["id"]
    b3 = (await create(client, splits=[line(amount="30000.00", destination_account="Tienda Nueva")]))["transaction"]["id"]
    b4 = (await create(client, splits=[line(category="Transporte", budget="Mercado", tags=["lote"])]))["transaction"]["id"]

    response = await client.post(f"{API}/bulk", headers=ADMIN, json={
        "ids": [b1, b2, b3, b4, b1, "999999"],
        "set": {"category": "transporte", "budget": "mercado", "tags_add": ["lote"], "tags_remove": ["viejo"]},
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert set(result) == {"group_id", "changed", "failed"}
    assert result["changed"] == [b1, b2, b3]  # b4 already had that state: nothing written, nothing reported
    assert result["failed"] == [{"id": "999999", "detail": "No existe ese movimiento."}]
    for fid in (b1, b2, b3):
        [journal] = fake.journals(fid)
        assert (journal["category_name"], journal["budget_name"]) == ("Transporte", "Mercado")
    assert fake.journals(b1)[0]["tags"] == ["lote"] and fake.journals(b2)[0]["tags"] == ["otro", "lote"]
    assert len(fake.writes("update")) == 3
    assert fake.journals(b3)[0]["destination_name"] == "Tienda Nueva" and fake.journals(b3)[0]["amount"] == "30000.000000000000"

    listing = (await client.get(f"{API}/changes", headers=ADMIN)).json()
    assert listing["total"] == 7
    updates = [item for item in listing["items"] if item["action"] == "update"]
    assert [item["firefly_id"] for item in updates] == [b3, b2, b1]
    assert {item["group_id"] for item in updates} == {result["group_id"]}
    assert updates[-1]["summary"] == "Editaste un gasto de $12.000 COP (categoría, presupuesto, etiquetas)"
    entries, total = await audit.entries(db, action="ledger.update")
    assert total == 3

    undone = (await client.post(f"{API}/groups/{result['group_id']}/undo", headers=ADMIN)).json()
    assert undone == {"undone": [item["id"] for item in updates], "failed": []}
    assert fake.journals(b1)[0]["category_name"] is None and fake.journals(b1)[0]["tags"] == ["viejo"] and fake.journals(b1)[0]["budget_id"] is None
    assert fake.journals(b2)[0]["category_name"] == "Transporte" and fake.journals(b2)[0]["tags"] == ["viejo", "otro"]
    assert fake.journals(b4)[0]["tags"] == ["lote"]
    again = (await client.post(f"{API}/groups/{result['group_id']}/undo", headers=ADMIN)).json()
    assert again == {"undone": [], "failed": []}


async def test_group_undo_reports_what_it_cannot_undo_and_still_does_the_rest(client, fake):
    a = (await create(client))["transaction"]["id"]
    b = (await create(client))["transaction"]["id"]
    c = (await create(client))["transaction"]["id"]
    group = (await client.post(f"{API}/bulk", headers=ADMIN, json={"ids": [a, b, c], "set": {"category": "Transporte"}})).json()
    assert group["changed"] == [a, b, c]
    by_id = {item["firefly_id"]: item["id"] for item in (await client.get(f"{API}/changes", headers=ADMIN)).json()["items"] if item["action"] == "update"}
    fake.journals(b)[0]["notes"] = "alguien lo editó"
    del fake.groups[c]
    result = (await client.post(f"{API}/groups/{group['group_id']}/undo", headers=ADMIN)).json()
    assert result["undone"] == [by_id[a]]
    assert [item["id"] for item in result["failed"]] == [by_id[c], by_id[b]]
    assert all("cambió" in item["detail"] for item in result["failed"])
    assert fake.journals(a)[0]["category_name"] is None and fake.journals(b)[0]["category_name"] == "Transporte"


async def test_bulk_source_account_follows_the_type_rules_per_movement(client, fake):
    withdrawal = (await create(client))["transaction"]["id"]
    deposit = (await create(client, type="deposit", splits=[{"amount": "5000.00", "source_account": "SALARIO", "destination_account": "Nequi"}]))["transaction"]["id"]
    result = (await client.post(f"{API}/bulk", headers=ADMIN, json={"ids": [withdrawal, deposit], "set": {"source_account": "Bancolombia"}})).json()
    assert result["changed"] == [withdrawal]
    assert fake.journals(withdrawal)[0]["source_name"] == "Bancolombia"
    assert result["failed"][0]["id"] == deposit and "no puede ser el origen de un ingreso" in result["failed"][0]["detail"]
    assert fake.journals(deposit)[0]["source_name"] == "SALARIO"


@pytest.mark.parametrize(("payload", "fragment"), [
    ({"ids": [], "set": {"category": "Transporte"}}, "entre 1 y 100"),
    ({"ids": [str(number) for number in range(101)], "set": {"category": "Transporte"}}, "entre 1 y 100"),
    ({"ids": ["1"], "set": {}}, "al menos un cambio"),
    ({"ids": ["1"], "set": {"tags_add": [], "tags_remove": []}}, "al menos un cambio"),
    ({"ids": ["1"], "set": {"category": "Inexistente"}}, "categoría"),
    ({"ids": ["1"], "set": {"category": "  "}}, "categoría"),
    ({"ids": ["1"], "set": {"budget": "Inexistente"}}, "presupuesto"),
    ({"ids": ["1"], "set": {"source_account": " "}}, "cuenta de origen"),
    ({"ids": ["1"], "set": {"tags_add": [" "]}}, "etiqueta"),
])
async def test_bulk_validation_rejects_the_whole_request(client, fake, payload, fragment):
    fake.seed()
    response = await client.post(f"{API}/bulk", headers=ADMIN, json=payload)
    assert fragment in error(response, 422, "validation_error")
    assert fake.requests == []


# --- inbox coherence ---------------------------------------------------------------------------------------------------


def analyzer_movement(fake):
    return fake.seed(external_id="fa:sms-1", tags=["auto", "sms"], category_name="Alimentación", description="Compra en SUPERMERCADO CENTRAL")


async def test_editing_an_analyzer_movement_syncs_its_inbox_item_without_changing_its_status(client, fake, service, db):
    fid = analyzer_movement(fake)
    item = await make_inbox(service, firefly_transaction_id=fid)
    read = (await client.get(f"{API}/transactions/{fid}", headers=ADMIN)).json()
    assert read["inbox_id"] == item.id and read["external_id"] == "fa:sms-1"

    result = await edit(client, fid, date="2026-11-07T09:30:00-05:00", description="Compra grande", splits=[
        line(amount="45000.00", source_account="Bancolombia", destination_account="TIENDA D1", category="Transporte", tags=["auto", "sms", "viaje"]),
    ])
    assert result["transaction"]["inbox_id"] == item.id
    await db.refresh(item)
    assert item.status == "created" and item.firefly_transaction_id == fid
    assert (item.amount, item.merchant, item.category, item.asset_account) == (Decimal("45000.00"), "TIENDA D1", "Transporte", "Bancolombia")
    assert (item.transaction_type, item.destination_account, item.description, item.tags) == ("withdrawal", None, "Compra grande", ["viaje"])
    assert item.occurred_at == datetime(2026, 11, 7, 14, 30, tzinfo=UTC)
    assert fake.journals(fid)[0]["external_id"] == "fa:sms-1"
    assert "ledger_update" in {event.action for event in (await db.scalars(select(InboxEvent))).all()}
    # The nightly reconciliation still finds the movement by its external_id: nothing is voided or orphaned.
    assert await service.reconcile() == {"voided": [], "orphaned": []}
    await db.refresh(item)
    assert item.status == "created"

    # Undoing the edit puts the item back as it was.
    await undo(client, result["change"]["id"])
    await db.refresh(item)
    assert (item.amount, item.merchant, item.category, item.asset_account) == (Decimal("12000.00"), "SUPERMERCADO CENTRAL", "Alimentación", "Nequi")
    assert (item.description, item.tags, item.status) == (None, [], "created")
    assert item.occurred_at == datetime(2026, 11, 6, 17, 0, tzinfo=UTC)


async def test_editing_into_a_transfer_and_a_deposit_keeps_the_inbox_accounts_coherent(client, fake, service, db):
    fid = analyzer_movement(fake)
    item = await make_inbox(service, firefly_transaction_id=fid)
    await edit(client, fid, type="transfer", splits=[line(source_account="Nequi", destination_account="Bancolombia")])
    await db.refresh(item)
    assert (item.transaction_type, item.asset_account, item.destination_account, item.merchant) == ("transfer", "Nequi", "Bancolombia", "SUPERMERCADO CENTRAL")
    await edit(client, fid, type="deposit", splits=[{"amount": "12000.00", "source_account": "SALARIO", "destination_account": "Nequi"}])
    await db.refresh(item)
    assert (item.transaction_type, item.asset_account, item.destination_account, item.merchant) == ("deposit", "Nequi", None, "SALARIO")


async def test_deleting_an_analyzer_movement_voids_its_inbox_item_and_undo_restores_it(client, fake, service, db):
    fid = analyzer_movement(fake)
    item = await make_inbox(service, firefly_transaction_id=fid)
    change = (await client.delete(f"{API}/transactions/{fid}", headers=ADMIN)).json()["change"]
    await db.refresh(item)
    assert item.status == "voided"
    assert "ledger_delete" in {event.action for event in (await db.scalars(select(InboxEvent))).all()}
    assert await service.reconcile() == {"voided": [], "orphaned": []}

    restored = await undo(client, change["id"])
    new = restored["transaction"]["id"]
    await db.refresh(item)
    assert item.status == "created" and item.firefly_transaction_id == new and restored["transaction"]["inbox_id"] == item.id
    assert fake.journals(new)[0]["external_id"] == "fa:sms-1"
    assert await service.reconcile() == {"voided": [], "orphaned": []}


async def test_a_movement_whose_inbox_item_is_missing_has_no_inbox_id(client, fake):
    fid = fake.seed(external_id="fa:missing")
    assert (await client.get(f"{API}/transactions/{fid}", headers=ADMIN)).json()["inbox_id"] is None
    assert (await edit(client, fid))["transaction"]["inbox_id"] is None


# --- changes, audit, scopes ----------------------------------------------------------------------------------------------


async def test_changes_are_listed_newest_first_with_pagination(client, fake):
    ids = [(await create(client))["change"]["id"] for _ in range(3)]
    page = (await client.get(f"{API}/changes", headers=ADMIN, params={"limit": 2})).json()
    assert page["total"] == 3 and [item["id"] for item in page["items"]] == [ids[2], ids[1]]
    rest = (await client.get(f"{API}/changes", headers=ADMIN, params={"limit": 2, "offset": 2})).json()
    assert [item["id"] for item in rest["items"]] == [ids[0]]
    assert (await client.get(f"{API}/changes", headers=ADMIN, params={"limit": 0})).status_code == 422


async def test_every_write_is_audited_without_free_text(client, fake, db):
    secret = {"description": "Descripción privada 123", "notes": "Nota privada 456"}
    created = await create(client, **secret)
    fid = created["transaction"]["id"]
    updated = await edit(client, fid, **secret, splits=[line(amount="13000.00")])
    await undo(client, updated["change"]["id"])
    await client.delete(f"{API}/transactions/{fid}", headers=ADMIN)
    entries, total = await audit.entries(db, limit=50)
    assert [entry.action for entry in entries] == ["ledger.delete", "ledger.undo", "ledger.update", "ledger.create"] and total == 4
    assert {entry.target_id for entry in entries} == {fid} and {entry.target_type for entry in entries} == {"ledger_transaction"}
    dump = json.dumps([entry.detail for entry in entries]) + json.dumps([row.summary for row in await change_rows(db)])
    assert "privada" not in dump
    update_entry = entries[2]
    assert update_entry.detail["fields"] == ["amount"] and update_entry.detail["amount"] == "13000.00" and update_entry.detail["change_id"] == updated["change"]["id"]


async def test_read_scope_can_read_but_never_write(client, fake, db):
    reader = await device(db, "reader", ["read"])
    created = await create(client)
    fid, cid, gid = created["transaction"]["id"], created["change"]["id"], created["change"]["group_id"]
    assert (await client.get(f"{API}/transactions/{fid}", headers=reader)).status_code == 200
    assert (await client.get(f"{API}/changes", headers=reader)).status_code == 200
    attempts = [
        ("POST", "/transactions", body()), ("PUT", f"/transactions/{fid}", put_body()), ("DELETE", f"/transactions/{fid}", None),
        ("POST", "/bulk", {"ids": [fid], "set": {"category": "Transporte"}}), ("POST", f"/changes/{cid}/undo", None),
        ("POST", f"/groups/{gid}/undo", None),
    ]
    for method, path, payload in attempts:
        response = await client.request(method, API + path, headers=reader, json=payload)
        error(response, 403, "insufficient_scope")
    assert fid in fake.groups and len(fake.requests) == 1 and len(await change_rows(db)) == 1


async def test_review_scope_writes_and_other_keys_are_refused(client, fake, db):
    reviewer = await device(db, "reviewer", ["read", "review"])
    response = await client.post(f"{API}/transactions", headers=reviewer, json=body())
    assert response.status_code == 201 and response.json()["change"]["actor"] == "reviewer"
    entries, _ = await audit.entries(db, action="ledger.create")
    assert entries[0].device_id is not None and entries[0].actor == "reviewer"
    ingest = await device(db, "phone", ["ingest"])
    error(await client.get(f"{API}/changes", headers=ingest), 403, "insufficient_scope")
    error(await client.get(f"{API}/changes"), 401, "invalid_key")


# --- validation ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("changes", "fragment"), [
    ({"splits": [line(amount="0")]}, "mayor que 0"),
    ({"splits": [line(amount="-5.00")]}, "mayor que 0"),
    ({"splits": [line(amount="50000000.01")]}, "máximo permitido"),
    ({"splits": [line(amount="30000000.00"), line(amount="30000000.00")]}, "total del movimiento"),
    ({"splits": [line(amount="10.505")]}, "2 decimales"),
    ({"splits": []}, "entre 1 y 20"),
    ({"splits": [line()] * 21}, "entre 1 y 20"),
    ({"splits": [line(), line(amount="0")]}, "División 2: El monto"),
    ({"description": "  "}, "descripción"),
    ({"description": "x" * 256}, "255"),
    ({"type": "refund"}, "tipo"),
    ({"date": "1999-01-01T00:00:00-05:00"}, "fecha"),
    ({"client_ref": None}, "client_ref"),
    ({"client_ref": "x" * 81}, "client_ref"),
    ({"splits": [line(category="Inexistente")]}, "categoría"),
    ({"splits": [line(budget="Inexistente")]}, "presupuesto"),
    ({"splits": [line(bill="Inexistente")]}, "factura"),
    ({"splits": [line(source_account="Cuenta Fantasma")]}, "no existe"),
    ({"splits": [line(source_account="SUPERMERCADO CENTRAL")]}, "no es una cuenta propia"),
    ({"splits": [line(destination_account="Bancolombia")]}, "no puede ser el comercio"),
    ({"type": "deposit", "splits": [line(source_account="Bancolombia", destination_account="Nequi")]}, "no puede ser el origen"),
    ({"type": "transfer", "splits": [line(source_account="Nequi", destination_account="Nequi")]}, "cuentas distintas"),
    ({"type": "transfer", "splits": [line(source_account="Nequi", destination_account="SUPERMERCADO CENTRAL")]}, "no es una cuenta propia"),
    ({"splits": [line(), line(source_account="Bancolombia")]}, "misma cuenta"),
    ({"splits": [line(foreign_amount="5.00")]}, "foreign_currency"),
    ({"splits": [line(foreign_currency="USD")]}, "foreign_amount"),
    ({"splits": [line(foreign_amount="5.00", foreign_currency="COP")]}, "distinto de COP"),
    ({"splits": [line(foreign_amount="0", foreign_currency="USD")]}, "mayor que 0"),
    ({"splits": [line(foreign_amount="5.00", foreign_currency="USD", currency="USD")]}, "COP"),
    ({"splits": [line(currency="USD")]}, "COP"),
    ({"splits": [line(tags=[f"t{number}" for number in range(11)])]}, "hasta 10"),
    ({"splits": [line(tags=[" "])]}, "etiqueta"),
    ({"splits": [line(tags=["x" * 41])]}, "etiqueta"),
    ({"notes": "x" * 4001}, "notas"),
])
async def test_invalid_drafts_are_rejected_in_spanish_and_write_nothing(client, fake, db, changes, fragment):
    response = await client.post(f"{API}/transactions", headers=ADMIN, json=body(**changes))
    detail = error(response, 422, "validation_error")
    assert fragment in detail, detail
    assert fake.requests == [] and fake.created_accounts == [] and await change_rows(db) == []


async def test_malformed_bodies_are_validation_errors(client, fake):
    for payload in ({"client_ref": "x", "type": "withdrawal"}, {"client_ref": "x", "type": "withdrawal", "date": "ayer", "description": "d", "splits": [line()]}, {"ids": "no"}):
        path = "/bulk" if "ids" in payload else "/transactions"
        error(await client.post(API + path, headers=ADMIN, json=payload), 422, "validation_error")
    assert fake.requests == []


async def test_a_draft_may_use_the_edge_values_of_the_contract(client, fake):
    splits = [line(amount="1.00", destination_account=f"Comercio {number}") for number in range(20)]
    result = await create(client, description="d" * 255, client_ref="r" * 80, splits=splits, date="2026-11-06T12:00:00")  # no zone: Bogota time
    assert len(result["transaction"]["splits"]) == 20 and result["transaction"]["date"] == "2026-11-06T12:00:00-05:00"
    assert fake.writes("create")[0][2]["group_title"] == "d" * 255
    top = await create(client, splits=[line(amount="50000000.00")])
    assert top["transaction"]["splits"][0]["amount"] == "50000000.00"
    assert top["change"]["summary"] == "Creaste un gasto de $50.000.000 COP"
