"""Consolidating counterparties: suggestions, merge in Firefly and the 7-day undo (Firefly and LLM mocked)."""
import json
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import call
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.api.dependencies import hash_secret
from app.core.exceptions import FireflyError, PermanentError
from app.core.timezone import now_utc
from app.db.models import AiUsage, CounterpartyMerge
from app.db.models_assistant import Audit
from app.db.models_devices import Device
from app.services import ai_budget, counterparties

ADMIN = {"X-API-Key": "test-admin-key-01234567890123456789"}
API = "/api/v1"

ACCOUNTS = [
    {"id": "1", "name": "Nequi", "type": "asset"},
    {"id": "10", "name": "Éxito", "type": "expense"},
    {"id": "11", "name": "EXITO", "type": "expense"},
    {"id": "12", "name": "Exito S.A.S", "type": "expense"},
    {"id": "20", "name": "Rappi", "type": "expense"},
    {"id": "21", "name": "Rappy", "type": "expense"},
    {"id": "30", "name": "Carlos Pérez", "type": "expense"},
    {"id": "31", "name": "Perez Carlos", "type": "expense"},
    {"id": "40", "name": "Farmatodo", "type": "expense"},
    {"id": "50", "name": "Droguería La 80", "type": "expense"},
    {"id": "51", "name": "Drogueria La 80 Centro", "type": "expense"},
    {"id": "60", "name": "EXITO", "type": "revenue"},
    {"id": "61", "name": "Salario", "type": "revenue"},
]
# (group, journal, destination expense account) of withdrawals paid from Nequi (id 1)
JOURNALS = [
    ("g1", "j1", "10"), ("g2", "j2", "10"), ("g3", "j3", "10"), ("g4", "j4", "11"), ("g5", "j5", "11"), ("g6", "j6", "12"),
    ("g7", "j7", "20"), ("g8", "j8", "20"), ("g9", "j9", "21"), ("g10", "j10", "30"), ("g11", "j11", "31"),
    ("g12", "j12", "40"), ("g13", "j13", "50"), ("g14", "j14", "51"),
]


class FakeLedger:
    """A tiny stateful Firefly: accounts and journals, with the calls the merge service makes."""

    def __init__(self, firefly):
        self.accounts = [dict(account) for account in ACCOUNTS]
        self.journals = [{"group": g, "journal": j, "source": "1", "destination": d} for g, j, d in JOURNALS]
        self.events = []
        self.fail = {}  # journal -> exception to raise when it is moved
        self.silent = set()  # journals whose move Firefly "accepts" without doing it
        self.next_id = 90
        firefly.accounts.side_effect = self._accounts
        firefly.account_transactions.side_effect = self._transactions
        firefly.update_split_account.side_effect = self._move
        firefly.delete_account.side_effect = self._delete
        firefly.create_account.side_effect = self._create

    async def _accounts(self):
        return [dict(account) for account in self.accounts]

    async def _transactions(self, account_id, start, end):
        if end >= date(2038, 1, 17):  # what the real Firefly answers
            raise PermanentError("Firefly 422: The end must be a date before 2038-01-17.")
        return [
            {"firefly_id": row["group"], "transaction_journal_id": row["journal"], "type": "withdrawal", "date": "2026-11-01T10:00:00-05:00",
             "description": "x", "amount": "1.00", "source_id": row["source"], "destination_id": row["destination"]}
            for row in self.journals if account_id in (row["source"], row["destination"])
        ]

    async def _move(self, group_id, journal_id, field, account_id):
        if journal_id in self.fail:
            raise self.fail[journal_id]
        self.events.append(("move", journal_id, field, account_id))
        if journal_id in self.silent:
            return
        for row in self.journals:
            if row["journal"] == journal_id:
                row["destination" if field == "destination_id" else "source"] = account_id

    async def _delete(self, account_id):
        self.events.append(("delete", account_id))
        self.accounts = [account for account in self.accounts if account["id"] != account_id]

    async def _create(self, name, type):
        self.next_id += 1
        account = {"id": str(self.next_id), "name": name, "type": type}
        self.accounts.append(account)
        self.events.append(("create", name, type))
        return account

    def destination(self, journal):
        return next(row["destination"] for row in self.journals if row["journal"] == journal)


@pytest.fixture
def ledger(clients):
    firefly, _ = clients
    return FakeLedger(firefly)


@pytest.fixture(autouse=True)
def isolated_budget_settings(settings, monkeypatch):
    monkeypatch.setattr(ai_budget, "get_settings", lambda: settings)


def same_if_80(sent=None, confidence=0.8):
    async def fake(prompt, content, cycle_id, usage=None):
        pairs = content["UNTRUSTED_DATA"]["pairs"]
        if sent is not None:
            sent.append(content)
        usage.update(calls=1, total_tokens=10)
        return {"answers": [{"i": pair["i"], "same": "80" in pair["a"], "confidence": confidence, "reason": "Misma droguería"} for pair in pairs]}

    return fake


async def device(db, name, scopes):
    key = f"{name}-key-" + "0123456789" * 3
    db.add(Device(name=name, key_hash=hash_secret(key), scopes=scopes))
    await db.commit()
    return {"X-API-Key": key}


async def audits(db, action):
    return list((await db.scalars(select(Audit).where(Audit.action == action).order_by(Audit.id))).all())


async def suggestions(client):
    response = await client.get(f"{API}/counterparties/merge-suggestions", headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()


async def merge(client, target, sources, headers=ADMIN):
    return await client.post(f"{API}/counterparties/merge", headers=headers, json={"target_id": target, "source_ids": sources})


# --- suggestions ------------------------------------------------------------------------------------------------------


def test_normalization_ignores_case_accents_punctuation_and_legal_suffixes():
    assert counterparties.normalize("Éxito") == counterparties.normalize("EXITO") == counterparties.normalize("Exito S.A.S") == ["exito"]
    assert counterparties.normalize("Droguería  La 80 Ltda.") == ["drogueria", "la", "80"]
    assert counterparties.normalize("Perez, Carlos") == ["perez", "carlos"]
    assert counterparties.normalize("S.A.S") == ["s", "a", "s"]  # a name made only of a suffix is kept as it is
    assert counterparties._within("rappi", "rappy", 1) and not counterparties._within("rappi", "rapid", 0)
    assert not counterparties._within("abcdefgh", "abxyefgh", 1)


async def test_suggestions_group_near_equal_names_by_type_with_counts(client, clients, db, ledger):
    _, llm = clients
    sent = []
    llm.complete_json.side_effect = same_if_80(sent)
    body = await suggestions(client)
    assert all(set(group) == {"id", "target", "sources", "confidence", "reason"} for group in body)
    assert [group["confidence"] for group in body] == [0.98, 0.9, 0.85, 0.8]
    by_target = {group["target"]["id"]: group for group in body}
    assert set(by_target) == {"10", "30", "20", "50"}
    exito = by_target["10"]
    assert exito["target"] == {"id": "10", "name": "Éxito"}  # the account with most transactions stays
    assert exito["sources"] == [{"id": "11", "name": "EXITO", "transactions": 2}, {"id": "12", "name": "Exito S.A.S", "transactions": 1}]
    assert exito["reason"] == "Mismo nombre salvo mayúsculas, tildes o signos"
    assert by_target["30"]["sources"] == [{"id": "31", "name": "Perez Carlos", "transactions": 1}] and by_target["30"]["reason"] == "Mismas palabras en otro orden"
    assert by_target["20"]["sources"] == [{"id": "21", "name": "Rappy", "transactions": 1}] and by_target["20"]["reason"] == "Nombres casi iguales (posible errata)"
    assert by_target["50"]["sources"] == [{"id": "51", "name": "Drogueria La 80 Centro", "transactions": 1}]
    assert by_target["50"]["reason"] == "Misma droguería"
    # The revenue account called EXITO is another type: never merged with the expense accounts; asset accounts never listed.
    listed = {group["target"]["id"] for group in body} | {source["id"] for group in body for source in group["sources"]}
    assert not listed & {"1", "60", "61", "40"}
    assert len(body[0]["id"]) == 12

    assert len(sent) == 1 and [(p["a"], p["b"]) for p in sent[0]["UNTRUSTED_DATA"]["pairs"]] == [("Droguería La 80", "Drogueria La 80 Centro")]
    usage = (await db.scalars(select(AiUsage).where(AiUsage.kind == "counterparty_merge"))).one()
    assert (usage.calls, usage.auto_calls, usage.tokens) == (1, 0, 10)  # an owner request: counted, not braked

    assert await suggestions(client) == body  # the verdict is cached: no second call
    assert llm.complete_json.call_count == 1


async def test_llm_verdicts_below_confidence_or_negative_are_ignored_and_failures_keep_the_deterministic_groups(client, clients, ledger):
    _, llm = clients
    llm.complete_json.side_effect = same_if_80(confidence=0.5)
    assert {group["target"]["id"] for group in await suggestions(client)} == {"10", "30", "20"}
    llm.complete_json.side_effect = RuntimeError("LLM chain exhausted")
    ledger.accounts.append({"id": "52", "name": "Droguería La 80 Sur", "type": "expense"})  # a new doubtful pair: not cached
    assert {group["target"]["id"] for group in await suggestions(client)} == {"10", "30", "20"}


async def test_names_of_people_reach_the_model_but_identifiers_do_not(client, clients, ledger):
    _, llm = clients
    ledger.accounts += [{"id": "70", "name": "Juan Perez", "type": "expense"}, {"id": "71", "name": "Juan Perez 3001234567", "type": "expense"}]
    sent = []

    async def fake(prompt, content, cycle_id, usage=None):
        sent.append(content)
        return {"answers": [{"i": pair["i"], "same": "Juan" in pair["a"], "confidence": 0.9, "reason": "Misma persona"} for pair in content["UNTRUSTED_DATA"]["pairs"]]}

    llm.complete_json.side_effect = fake
    body = await suggestions(client)
    wire = json.dumps(sent[0], ensure_ascii=False)
    assert "Juan Perez" in wire and "3001234567" not in wire and "[TEL]" in wire
    assert "UNTRUSTED_DATA" in llm.complete_json.call_args.args[0]
    juan = next(group for group in body if group["target"]["id"] in {"70", "71"})
    assert {juan["target"]["id"], juan["sources"][0]["id"]} == {"70", "71"} and juan["confidence"] == 0.9


async def test_suggestions_are_not_braked_by_the_automatic_budget(client, clients, db, ledger):
    _, llm = clients
    llm.complete_json.side_effect = same_if_80()
    await ai_budget.set_policy(db, 0)
    await db.commit()
    assert {group["target"]["id"] for group in await suggestions(client)} == {"10", "30", "20", "50"}


# --- merge ------------------------------------------------------------------------------------------------------------


async def inbox_item(service, group, merchant):
    row = await service.inbox.add(
        source="sms", source_ref=str(uuid4()), sender="85954", subject_or_title="Compra", raw_text=None, status="created", method="parser",
        transaction_type="withdrawal", amount=Decimal("1.00"), currency="COP", merchant=merchant, asset_account="Nequi", firefly_transaction_id=group,
    )
    await service.session.commit()
    return row


async def test_merge_moves_every_split_deletes_the_empty_sources_and_audits(client, service, clients, db, ledger):
    firefly, _ = clients
    moved = await inbox_item(service, "g4", "EXITO")
    untouched = await inbox_item(service, "g1", "ÉXITO")
    other = await inbox_item(service, "g5", "OTRO COMERCIO")
    moved_id, untouched_id, other_id = moved.id, untouched.id, other.id

    response = await merge(client, "10", ["11", "12"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "applied" and body["account_type"] == "expense" and body["target"] == {"id": "10", "name": "Éxito"}
    assert body["sources"] == [
        {"id": "11", "name": "EXITO", "moved": 2, "planned": 2, "deleted": True, "error": None},
        {"id": "12", "name": "Exito S.A.S", "moved": 1, "planned": 1, "deleted": True, "error": None},
    ]
    assert all(ledger.destination(journal) == "10" for journal in ("j1", "j2", "j3", "j4", "j5", "j6"))
    assert ledger.destination("j7") == "20"  # other counterparties are untouched
    assert ledger.events == [
        ("move", "j4", "destination_id", "10"), ("move", "j5", "destination_id", "10"), ("delete", "11"),
        ("move", "j6", "destination_id", "10"), ("delete", "12"),
    ]
    assert firefly.update_split_account.call_args_list[0] == call("g4", "j4", "destination_id", "10")
    assert [account["id"] for account in ledger.accounts if account["type"] == "expense" and account["id"] in {"10", "11", "12"}] == ["10"]

    record = await db.get(CounterpartyMerge, body["id"])
    assert record.status == "applied" and record.target_id == "10" and record.actor == "admin"
    assert record.expires_at - record.created_at == timedelta(days=7)
    assert [(entry["id"], entry["deleted"], [move["done"] for move in entry["moves"]]) for entry in record.changes["sources"]] == [("11", True, [True, True]), ("12", True, [True])]
    assert record.changes["sources"][0]["moves"][0] == {"group_id": "g4", "journal_id": "j4", "field": "destination_id", "done": True}
    entry = (await audits(db, "counterparty.merge"))[0]
    assert entry.target_id == str(record.id) and entry.detail["status"] == "applied" and entry.detail["target"] == {"id": "10", "name": "Éxito"}

    # Inbox items of the merged account follow it (so history and rules agree); others stay.
    for item_id, merchant in ((moved_id, "Éxito"), (untouched_id, "ÉXITO"), (other_id, "OTRO COMERCIO")):
        assert (await service.inbox.get(item_id)).merchant == merchant


async def test_merge_accepts_numeric_ids_and_revenue_accounts(client, ledger):
    ledger.journals.append({"group": "g20", "journal": "j20", "source": "61", "destination": "1"})
    ledger.accounts.append({"id": "62", "name": "SALARIO", "type": "revenue"})
    ledger.journals.append({"group": "g21", "journal": "j21", "source": "62", "destination": "1"})
    response = await client.post(f"{API}/counterparties/merge", headers=ADMIN, json={"target_id": 61, "source_ids": [62]})
    assert response.status_code == 200 and response.json()["account_type"] == "revenue"
    assert ledger.events == [("move", "j21", "source_id", "61"), ("delete", "62")]


@pytest.mark.parametrize(("target", "sources", "status", "code"), [
    ("10", ["10"], 422, "target_in_sources"),
    ("10", ["999"], 404, "account_not_found"),
    ("999", ["11"], 404, "account_not_found"),
    ("10", ["1"], 422, "invalid_account_type"),   # an own (asset) account is never merged
    ("1", ["11"], 422, "invalid_account_type"),
    ("10", ["60"], 422, "mixed_account_types"),   # expense with revenue
])
async def test_merge_validates_the_accounts_before_touching_anything(client, ledger, target, sources, status, code):
    response = await merge(client, target, sources)
    assert response.status_code == status and response.json()["code"] == code
    assert ledger.events == []


async def test_merge_body_validation(client, ledger):
    assert (await merge(client, "10", [])).status_code == 422
    assert (await merge(client, "10", [str(n) for n in range(100, 125)])).status_code == 422
    assert (await client.post(f"{API}/counterparties/merge", headers=ADMIN, json={"target_id": "10", "source_ids": ["11"], "x": 1})).status_code == 422
    assert ledger.events == []


async def test_a_failed_move_keeps_its_source_and_the_merge_can_be_repeated(client, ledger, db):
    ledger.fail["j5"] = PermanentError("Firefly 422: nope")
    body = (await merge(client, "10", ["11", "12"])).json()
    assert body["status"] == "partial"
    first, second = body["sources"]
    assert (first["moved"], first["planned"], first["deleted"]) == (1, 2, False) and first["error"]
    assert second["deleted"] is True and second["error"] is None
    assert ("delete", "11") not in ledger.events and ("delete", "12") in ledger.events
    assert ledger.destination("j4") == "10" and ledger.destination("j5") == "11"

    del ledger.fail["j5"]
    again = (await merge(client, "10", ["11"])).json()
    assert again["status"] == "applied" and again["id"] != body["id"]
    assert again["sources"] == [{"id": "11", "name": "EXITO", "moved": 1, "planned": 1, "deleted": True, "error": None}]
    assert ("delete", "11") in ledger.events and ledger.destination("j5") == "10"
    assert (await db.get(CounterpartyMerge, body["id"])).status == "partial"


async def test_an_unavailable_firefly_stops_that_source_without_losing_the_map(client, ledger, db):
    ledger.fail["j4"] = FireflyError("Firefly temporarily unavailable (503)")
    body = (await merge(client, "10", ["11"])).json()
    assert body["status"] == "partial" and body["sources"][0]["moved"] == 0 and body["sources"][0]["error"]
    record = await db.get(CounterpartyMerge, body["id"])
    assert [move["done"] for move in record.changes["sources"][0]["moves"]] == [False, False]
    assert ("delete", "11") not in ledger.events


async def test_a_source_that_still_has_movements_is_never_deleted(client, ledger):
    ledger.silent.add("j5")  # Firefly says OK but the journal stays on the source account
    body = (await merge(client, "10", ["11", "12"])).json()
    assert body["status"] == "partial"
    assert body["sources"][0]["deleted"] is False and "Quedan movimientos" in body["sources"][0]["error"]
    assert [event for event in ledger.events if event[0] == "delete"] == [("delete", "12")]


# --- undo -------------------------------------------------------------------------------------------------------------


async def test_undo_recreates_the_accounts_moves_the_splits_back_and_restores_the_inbox(client, service, clients, db, ledger):
    firefly, _ = clients
    item = await inbox_item(service, "g4", "EXITO")
    item_id = item.id
    merged = (await merge(client, "10", ["11", "12"])).json()
    ledger.events.clear()

    response = await client.post(f"{API}/counterparties/merge/{merged['id']}/undo", headers=ADMIN)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "undone" and body["failed"] == [] and body["undone_at"] is not None
    assert firefly.create_account.call_args_list == [call("EXITO", "expense"), call("Exito S.A.S", "expense")]
    restored = {account["name"]: account["id"] for account in ledger.accounts if account["id"] not in {a["id"] for a in ACCOUNTS}}
    assert set(restored) == {"EXITO", "Exito S.A.S"}
    assert [ledger.destination(journal) for journal in ("j4", "j5")] == [restored["EXITO"]] * 2
    assert ledger.destination("j6") == restored["Exito S.A.S"]
    assert [ledger.destination(journal) for journal in ("j1", "j2", "j3")] == ["10"] * 3
    assert (await service.inbox.get(item_id)).merchant == "EXITO"
    assert (await db.get(CounterpartyMerge, merged["id"], populate_existing=True)).status == "undone"
    entry = (await audits(db, "counterparty.merge_undo"))[0]
    assert entry.target_id == str(merged["id"]) and entry.detail == {"undone": True, "failed": []}

    again = await client.post(f"{API}/counterparties/merge/{merged['id']}/undo", headers=ADMIN)
    assert again.status_code == 409 and again.json()["code"] == "merge_already_undone"


async def test_undo_reuses_an_account_the_owner_already_recreated(client, clients, ledger):
    firefly, _ = clients
    merged = (await merge(client, "10", ["11"])).json()
    ledger.accounts.append({"id": "77", "name": "exito", "type": "expense"})
    body = (await client.post(f"{API}/counterparties/merge/{merged['id']}/undo", headers=ADMIN)).json()
    assert body["status"] == "undone"
    firefly.create_account.assert_not_called()
    assert ledger.destination("j4") == "77" and ledger.destination("j5") == "77"


async def test_a_partial_undo_can_be_finished_without_repeating_what_is_done(client, clients, db, ledger):
    firefly, _ = clients
    merged = (await merge(client, "10", ["11", "12"])).json()
    ledger.fail["j5"] = FireflyError("Firefly temporarily unavailable (503)")
    ledger.events.clear()
    body = (await client.post(f"{API}/counterparties/merge/{merged['id']}/undo", headers=ADMIN)).json()
    assert body["status"] == "applied" and [failure["id"] for failure in body["failed"]] == ["11"]
    assert ledger.destination("j6") not in {"10", "12"}  # the other source went back
    assert firefly.create_account.call_count == 2
    del ledger.fail["j5"]
    ledger.events.clear()
    retry = (await client.post(f"{API}/counterparties/merge/{merged['id']}/undo", headers=ADMIN)).json()
    assert retry["status"] == "undone" and retry["failed"] == []
    assert [event[:2] for event in ledger.events] == [("move", "j5")]  # only the pending move; no account created twice
    assert firefly.create_account.call_count == 2


async def test_undo_is_refused_after_seven_days_and_for_unknown_merges(client, db, ledger):
    merged = (await merge(client, "10", ["11"])).json()
    record = await db.get(CounterpartyMerge, merged["id"])
    record.expires_at = now_utc() - timedelta(minutes=1)
    await db.commit()
    expired = await client.post(f"{API}/counterparties/merge/{merged['id']}/undo", headers=ADMIN)
    assert expired.status_code == 409 and expired.json()["code"] == "undo_expired"
    assert ledger.destination("j4") == "10"
    missing = await client.post(f"{API}/counterparties/merge/999/undo", headers=ADMIN)
    assert missing.status_code == 404 and missing.json()["code"] == "merge_not_found"


# --- scopes -----------------------------------------------------------------------------------------------------------


async def test_counterparty_routes_scopes(client, clients, db, ledger):
    _, llm = clients
    llm.complete_json.side_effect = same_if_80()
    reader = await device(db, "reader", ["read"])
    reviewer = await device(db, "reviewer", ["review"])
    assert (await client.get(f"{API}/counterparties/merge-suggestions", headers=reader)).status_code == 200
    assert (await merge(client, "10", ["11"], headers=reader)).status_code == 403
    assert (await client.post(f"{API}/counterparties/merge/1/undo", headers=reader)).status_code == 403
    assert (await client.get(f"{API}/counterparties/merge-suggestions")).status_code == 401
    assert ledger.events == []
    assert (await merge(client, "10", ["11"], headers=reviewer)).status_code == 200
