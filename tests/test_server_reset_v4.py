"""POST /maintenance/reset: full reset of movements (Firefly mocked): dry run, confirmation, what goes and what stays, idempotency, scope."""
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.api.dependencies import hash_secret
from app.api.routers import maintenance
from app.core.exceptions import FireflyError, PermanentError
from app.core.timezone import now_utc
from app.db.models import (
    AiSuggestion,
    AlertMute,
    BudgetMap,
    DashboardSnapshot,
    InboxEvent,
    InboxItem,
    KnownSender,
    MailAccountState,
    Memory,
    MerchantCorrection,
    Notification,
    OwnAccountRow,
)
from app.db.models_alerts import Alert
from app.db.models_assistant import Audit, Proposal, ProposalBatch
from app.db.models_devices import Device
from app.db.models_flags import RuntimeFlag
from app.db.models_ledger import LedgerChange
from app.db.models_rules import Merchant, MerchantItem, Rule
from app.services import audit, server_reset

ADMIN = {"X-API-Key": "test-admin-key-01234567890123456789"}
RESET = "/api/v1/maintenance/reset"
CONFIRM = "BORRAR MOVIMIENTOS"
ACCOUNTS = [
    {"id": "1", "name": "Nequi", "type": "asset"},
    {"id": "10", "name": "Éxito", "type": "expense"},  # keeps a movement Cofre did not create
    {"id": "11", "name": "RAPPI", "type": "expense"},  # only Cofre movements: ends empty
    {"id": "12", "name": "Salario", "type": "revenue"},  # only a Cofre movement: ends empty
    {"id": "13", "name": "Vacía", "type": "expense"},  # empty from the start
    {"id": "14", "name": "Tienda ajena", "type": "expense"},  # holds the owner's own movement
    {"id": "15", "name": "NEQUI", "type": "expense"},  # empty but named like an own account: never touched
]


def group(group_id, source, destination, *, external=None, tags=()):
    return {"id": group_id, "attributes": {"transactions": [{
        "transaction_journal_id": "j" + group_id, "type": "withdrawal", "date": "2026-11-01T10:00:00-05:00", "description": "x",
        "amount": "1.00", "source_id": source, "destination_id": destination, "external_id": external, "tags": list(tags),
    }]}}


class FakeFirefly:
    """A tiny stateful Firefly (two groups per page) with the calls the reset makes."""

    def __init__(self, firefly):
        self.accounts = [dict(account) for account in ACCOUNTS]
        self.groups = {row["id"]: row for row in (
            group("g1", "1", "10", external="fa:ref-1", tags=("auto", "email")),
            group("g2", "1", "11", tags=("manual",)),
            group("g3", "1", "11", external="ledger:abc"),
            group("g4", "12", "1", external="fa:ref-4", tags=("auto",)),
            group("g5", "1", "14", tags=("viaje",)),
            group("g6", "1", "10", external="otro:1"),
        )}
        self.events = []
        self.fail = {}  # group id -> exception raised when it is deleted
        firefly.transaction_groups_page.side_effect = self._page
        firefly.accounts.side_effect = self._accounts
        firefly.account_transactions.side_effect = self._transactions
        firefly.delete_transaction.side_effect = self._delete_transaction
        firefly.delete_account.side_effect = self._delete_account

    async def _page(self, page, start, end):
        rows = list(self.groups.values())
        return rows[(page - 1) * 2:page * 2], max(1, -(-len(rows) // 2))

    async def _accounts(self):
        return [dict(account) for account in self.accounts]

    async def _transactions(self, account_id, start, end):
        return [
            {"firefly_id": row["id"], **{key: split.get(key) for key in ("transaction_journal_id", "type", "date", "description", "amount", "source_id", "destination_id")}}
            for row in self.groups.values() for split in row["attributes"]["transactions"] if account_id in (split["source_id"], split["destination_id"])
        ]

    async def _delete_transaction(self, group_id):
        if group_id in self.fail:
            raise self.fail.pop(group_id)
        self.events.append(("delete_transaction", group_id))
        if group_id not in self.groups:
            raise PermanentError("Firefly 404: not found")
        del self.groups[group_id]

    async def _delete_account(self, account_id):
        self.events.append(("delete_account", account_id))
        self.accounts = [account for account in self.accounts if account["id"] != account_id]

    def names(self):
        return {account["name"] for account in self.accounts}


@pytest.fixture
def fake(clients):
    return FakeFirefly(clients[0])


@pytest.fixture(autouse=True)
def inline(monkeypatch, db):
    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(maintenance, "get_db_session", session)
    monkeypatch.setattr(server_reset, "_task", None)
    monkeypatch.setattr(server_reset, "_runs", OrderedDict())


async def count(db, model, *where):
    return await db.scalar(select(func.count()).select_from(model).where(*where))


async def seed(db):
    now = now_utc()
    items = [InboxItem(source="email", source_ref=f"ref-{number}", status="created", amount=Decimal("1000.00"), merchant="exito") for number in (1, 2)]
    db.add_all(items)
    await db.flush()
    merchant = Merchant(name="exito", default_category="Alimentación", count=5, last_seen_at=now)
    batch = ProposalBatch(window_from=now, window_to=now)
    alert = Alert(type="pending_review", title="t", item_id=items[0].id)
    db.add_all([merchant, batch, alert])
    await db.flush()
    db.add_all([
        InboxEvent(inbox_id=items[0].id, action="created"), AiSuggestion(item_id=items[1].id),
        Proposal(batch_id=batch.id, target_id=items[0].id, field="category"), MerchantItem(item_id=items[0].id, merchant_id=merchant.id),
        LedgerChange(group_id="g", action="create", firefly_id="g1", summary="s"), DashboardSnapshot(month="2026-11", data={}),
        Rule(match="merchant_exact", pattern="exito", category="Mercado", source="learned"),
        Rule(match="merchant_exact", pattern="rappi", category="Domicilios", source="manual"),
        BudgetMap(kind="merchant", key="exito", budget="Mercado", learned=True), MerchantCorrection(merchant="exito", category="Mercado", streak=2),
        KnownSender(keyword="bancolombia"), Memory(text="Prefiere ahorrar", kind="preference"), AlertMute(below_amount=Decimal("1000.00")),
        MailAccountState(name="gmail", last_uid=500, uidvalidity="7", last_poll_at=now),
        Notification(key="alert:new_charge:1"), Notification(key="alert:digest:day:2026-11-23"), Notification(key="mail-test:gmail"),
        RuntimeFlag(key="digest_grouped", value={"items": [{"id": 1}]}), RuntimeFlag(key="digest_policy", value={"daily": False}),
        Device(name="phone", key_hash=hash_secret("phone-key-" + "0123456789" * 3), scopes=["ingest"]),
    ])
    await audit.record(db, action="seed.before", target_type="x", target_id=1)
    await db.commit()


async def device(db, name, scopes):
    key = f"{name}-key-" + "0123456789" * 3
    db.add(Device(name=name, key_hash=hash_secret(key), scopes=scopes))
    await db.commit()
    return {"X-API-Key": key}


async def run(client, **body):
    response = await client.post(RESET, headers=ADMIN, json={"dry_run": False, "confirm": CONFIRM, **body})
    assert response.status_code == 202, response.text
    await server_reset._task
    status = await client.get(f"{RESET}/{response.json()['id']}", headers=ADMIN)
    assert status.status_code == 200
    return status.json()


# --- the table inventory ------------------------------------------------------------------------------------------------


def test_every_table_is_deleted_reset_or_deliberately_kept():
    from app.db.models import Base

    options = server_reset.ResetOptions()
    handled = {name for name, *_ in server_reset._targets(options)} | {name for name, *_ in server_reset._updates(options)}
    kept = {
        "known_senders", "cycle_runs", "own_accounts", "ai_usage", "audit", "devices", "enroll_codes", "memories", "alert_mutes",
        "mail_account_state", "rules", "merchants", "notifications", "runtime_flags",
    }
    # rules, merchants, notifications and runtime_flags are touched in part (learned rules, counters, alert keys, the digest queue).
    assert set(Base.metadata.tables) == handled | kept, set(Base.metadata.tables) ^ (handled | kept)
    assert {"rules", "merchants", "notifications", "runtime_flags"} <= handled
    assert not {"known_senders", "own_accounts", "audit", "devices", "memories", "alert_mutes", "ai_usage", "enroll_codes", "cycle_runs"} & handled


def test_cofre_movements_are_recognized_by_external_id_or_tag():
    assert server_reset.is_cofre_group(group("a", "1", "2", external="fa:x"))
    assert server_reset.is_cofre_group(group("a", "1", "2", external="ledger:x"))
    assert server_reset.is_cofre_group(group("a", "1", "2", tags=("auto",)))
    assert server_reset.is_cofre_group(group("a", "1", "2", tags=("viaje", "manual")))
    assert not server_reset.is_cofre_group(group("a", "1", "2", external="otro:fa:x", tags=("viaje",)))
    assert not server_reset.is_cofre_group({"id": "z", "attributes": {"transactions": []}})


# --- dry run and confirmation -------------------------------------------------------------------------------------------


async def test_dry_run_is_the_default_and_changes_nothing(client, db, fake, clients):
    await seed(db)
    for body in (None, {}, {"dry_run": True}):
        response = await client.post(RESET, headers=ADMIN, json=body)
        assert response.status_code == 200, response.text
    data = response.json()
    assert data["dry_run"] is True and data["options"] == {"keep_rules": False, "reset_mail_cursor": False}
    assert data["tables"]["inbox_items"] == 2 and data["tables"]["inbox_events"] == 1 and data["tables"]["alerts"] == 1
    assert data["tables"]["rules"] == 1 and data["tables"]["budget_map"] == 1 and data["tables"]["merchant_corrections"] == 1  # learned rules only
    assert data["tables"]["notifications"] == 1 and data["tables"]["runtime_flags"] == 1 and data["tables"]["ledger_changes"] == 1
    assert data["updated"] == {"merchants": 1}
    assert data["firefly"]["transactions"] == 4 and data["firefly"]["accounts"] == 3
    assert {a["name"] for a in data["firefly"]["account_examples"]} == {"RAPPI", "Salario", "Vacía"}
    assert await count(db, InboxItem) == 2 and len(fake.groups) == 6 and len(fake.accounts) == 7 and fake.events == []
    clients[0].delete_transaction.assert_not_awaited()
    assert await count(db, Audit, Audit.action == "maintenance.reset") == 0


async def test_dry_run_reports_options_effects(client, db, fake):
    await seed(db)
    data = (await client.post(RESET, headers=ADMIN, json={"keep_rules": True, "reset_mail_cursor": True})).json()
    assert "rules" not in data["tables"] and "budget_map" not in data["tables"] and "merchant_corrections" not in data["tables"]
    assert data["updated"] == {"merchants": 1, "mail_account_state": 1}


@pytest.mark.parametrize("body", [{"dry_run": False}, {"dry_run": False, "confirm": "borrar movimientos"}, {"dry_run": False, "confirm": ""}, {"dry_run": False, "confirm": "BORRAR"}])
async def test_confirmation_is_mandatory_for_a_real_run(client, db, fake, body):
    await seed(db)
    response = await client.post(RESET, headers=ADMIN, json=body)
    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert server_reset._task is None and not server_reset._runs
    assert await count(db, InboxItem) == 2 and len(fake.groups) == 6 and fake.events == []


async def test_unknown_fields_are_rejected(client, fake):
    response = await client.post(RESET, headers=ADMIN, json={"dry_run": True, "everything": True})
    assert response.status_code == 422 and response.json()["code"] == "validation_error"


# --- the real run -------------------------------------------------------------------------------------------------------


async def test_real_run_deletes_movements_and_keeps_the_rest(client, db, fake):
    await seed(db)
    started = await client.post(RESET, headers=ADMIN, json={"dry_run": False, "confirm": CONFIRM})
    assert started.status_code == 202 and set(started.json()) == {"id"}
    run_id = started.json()["id"]
    await server_reset._task
    state = (await client.get(f"{RESET}/{run_id}", headers=ADMIN)).json()
    assert state["status"] == "done" and state["phase"] == "finished" and state["error"] is None and state["finished_at"]
    assert state["progress"]["firefly_transactions"] == {"found": 4, "deleted": 4}
    assert state["progress"]["firefly_accounts"] == {"found": 3, "deleted": 3, "skipped": 0}
    assert state["result"]["firefly"] == {"transactions": 4, "accounts": 3, "skipped_accounts": []}
    assert state["result"]["tables"]["inbox_items"] == 2 and state["result"]["updated"] == {"merchants": 1}

    # Firefly: Cofre's movements and the accounts left empty are gone; everything else stays.
    assert set(fake.groups) == {"g5", "g6"}
    assert fake.names() == {"Nequi", "Éxito", "Tienda ajena", "NEQUI"}
    deletions = [event for event in fake.events if event[0] == "delete_transaction"]
    assert deletions and fake.events.index(("delete_account", "11")) > fake.events.index(deletions[-1])  # movements first, then accounts

    # Database: movements and dependents gone, learned things gone.
    for model in (InboxItem, InboxEvent, AiSuggestion, Proposal, ProposalBatch, MerchantItem, Alert, LedgerChange, DashboardSnapshot, BudgetMap, MerchantCorrection):
        assert await count(db, model) == 0, model
    assert await count(db, Rule, Rule.source == "learned") == 0
    # Kept: manual rules, senders, memories and mutes, devices, settings, cursors, own accounts and the audit.
    assert [rule.pattern for rule in (await db.scalars(select(Rule))).all()] == ["rappi"]
    for model in (KnownSender, Memory, AlertMute, Device):
        assert await count(db, model) == 1, model
    flags = {flag.key for flag in (await db.scalars(select(RuntimeFlag))).all()}
    assert "digest_policy" in flags and "ai_second_opinion" in flags and "digest_grouped" not in flags
    assert {row.key for row in (await db.scalars(select(Notification))).all()} == {"alert:digest:day:2026-11-23", "mail-test:gmail"}
    merchant = (await db.scalars(select(Merchant))).one()
    assert (merchant.default_category, merchant.count, merchant.last_seen_at) == ("Alimentación", 0, None)
    cursor = await db.get(MailAccountState, "gmail", populate_existing=True)
    assert (cursor.last_uid, cursor.uidvalidity) == (500, "7")
    assert await count(db, OwnAccountRow) == 1
    entries = (await db.scalars(select(Audit).order_by(Audit.id))).all()
    assert [entry.action for entry in entries] == ["seed.before", "maintenance.reset"]
    detail = entries[1].detail
    assert entries[1].actor == "admin" and detail["status"] == "done" and detail["run_id"] == run_id
    assert detail["tables"]["inbox_items"] == 2 and detail["firefly"]["transactions"] == 4 and detail["keep_rules"] is False


async def test_keep_rules_and_mail_cursor_options(client, db, fake):
    await seed(db)
    state = await run(client, keep_rules=True, reset_mail_cursor=True)
    assert state["status"] == "done" and state["options"] == {"keep_rules": True, "reset_mail_cursor": True}
    assert await count(db, Rule) == 2 and await count(db, BudgetMap) == 1 and await count(db, MerchantCorrection) == 1
    assert await count(db, InboxItem) == 0
    cursor = await db.get(MailAccountState, "gmail", populate_existing=True)
    assert (cursor.last_uid, cursor.uidvalidity) == (0, None)
    assert state["result"]["updated"] == {"merchants": 1, "mail_account_state": 1}


async def test_the_run_is_idempotent(client, db, fake):
    await seed(db)
    first = await run(client)
    second = await run(client)
    assert first["status"] == second["status"] == "done"
    assert second["result"]["firefly"] == {"transactions": 0, "accounts": 0, "skipped_accounts": []}
    assert sum(second["result"]["tables"].values()) == 0 and second["result"]["updated"] == {"merchants": 0}
    assert set(fake.groups) == {"g5", "g6"} and await count(db, Audit, Audit.action == "maintenance.reset") == 2


async def test_a_failure_in_firefly_leaves_the_database_alone_and_can_be_repeated(client, db, fake):
    await seed(db)
    fake.fail["g3"] = FireflyError("Firefly temporarily unavailable (503)")
    failed = await run(client)
    assert failed["status"] == "failed" and failed["phase"] == "firefly_transactions"
    assert failed["error"]["code"] == "firefly_unavailable" and failed["error"]["phase"] == "firefly_transactions"
    assert failed["progress"]["firefly_transactions"] == {"found": 4, "deleted": 2} and failed["result"] is None
    assert await count(db, InboxItem) == 2 and await count(db, Rule) == 2 and set(fake.groups) == {"g3", "g4", "g5", "g6"}
    audits = (await db.scalars(select(Audit).where(Audit.action == "maintenance.reset"))).all()
    assert len(audits) == 1 and audits[0].detail["status"] == "failed" and audits[0].detail["phase"] == "firefly_transactions"
    repeated = await run(client)  # the same call again finishes the job
    assert repeated["status"] == "done" and repeated["result"]["firefly"]["transactions"] == 2
    assert set(fake.groups) == {"g5", "g6"} and await count(db, InboxItem) == 0
    assert fake.names() == {"Nequi", "Éxito", "Tienda ajena", "NEQUI"}


async def test_a_movement_already_gone_counts_as_deleted(client, db, fake):
    await seed(db)
    fake.fail["g1"] = PermanentError("Firefly 404: not found")
    state = await run(client)
    assert state["status"] == "done" and state["progress"]["firefly_transactions"]["deleted"] == 4
    assert await count(db, InboxItem) == 0


async def test_an_account_firefly_refuses_to_delete_is_reported_and_does_not_stop_the_reset(client, db, fake, clients):
    await seed(db)

    async def refuse(account_id):
        if account_id == "13":
            raise PermanentError("Firefly 422: cannot delete")
        await fake._delete_account(account_id)

    clients[0].delete_account.side_effect = refuse
    state = await run(client)
    assert state["status"] == "done" and state["result"]["firefly"]["accounts"] == 2
    assert state["result"]["firefly"]["skipped_accounts"] == [{"id": "13", "name": "Vacía"}]
    assert await count(db, InboxItem) == 0 and "Vacía" in fake.names()


async def test_a_database_failure_is_reported_and_rolled_back_by_the_session(client, db, fake, monkeypatch):
    await seed(db)

    async def broken(session, options):
        raise RuntimeError("boom")

    monkeypatch.setattr(server_reset, "_purge_database", broken)
    state = await run(client)
    assert state["status"] == "failed" and state["error"]["code"] == "reset_failed" and state["error"]["phase"] == "database"
    assert "boom" not in str(state)


async def test_only_one_reset_runs_at_a_time(client, db, fake, monkeypatch):
    await seed(db)
    monkeypatch.setattr(server_reset, "_task", asyncio.get_running_loop().create_future())
    response = await client.post(RESET, headers=ADMIN, json={"dry_run": False, "confirm": CONFIRM})
    assert response.status_code == 409 and response.json()["code"] == "reset_running"
    assert await count(db, InboxItem) == 2 and fake.events == []
    assert (await client.post(RESET, headers=ADMIN, json={"dry_run": True})).status_code == 200  # looking is always allowed


async def test_caches_are_invalidated(client, db, fake, monkeypatch):
    await seed(db)
    calls = []
    monkeypatch.setattr(server_reset.snapshots, "drop_pending", lambda: _record(calls, "snapshots"))
    monkeypatch.setattr(server_reset.own_accounts, "invalidate", lambda: calls.append("own_accounts"))
    monkeypatch.setattr(server_reset.descriptions, "invalidate", lambda: calls.append("descriptions"))
    monkeypatch.setattr(server_reset.ai_search, "clear_cache", lambda: calls.append("search"))
    await run(client)
    assert {"snapshots", "own_accounts", "descriptions", "search"} <= set(calls)


async def _record(calls, name):
    calls.append(name)


# --- scope and status ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("scopes", [["review"], ["read"], ["ingest"], ["review", "read"]])
async def test_only_admin_may_reset_never_a_device_without_admin(client, db, fake, scopes):
    await seed(db)
    headers = await device(db, "phone2", scopes)
    for body in ({"dry_run": True}, {"dry_run": False, "confirm": CONFIRM}, None):
        response = await client.post(RESET, headers=headers, json=body)
        assert response.status_code == 403, (body, response.text)
    assert (await client.get(f"{RESET}/abc", headers=headers)).status_code == 403
    assert await count(db, InboxItem) == 2 and fake.events == [] and not server_reset._runs


async def test_an_admin_device_may_reset_and_a_missing_key_is_401(client, db, fake):
    headers = await device(db, "admin-phone", ["admin"])
    assert (await client.post(RESET, headers=headers, json={"dry_run": True})).status_code == 200
    assert (await client.post(RESET, json={"dry_run": True})).status_code == 401
    assert (await client.get(f"{RESET}/abc")).status_code == 401


async def test_unknown_run_is_404(client, db, fake):
    response = await client.get(f"{RESET}/nope", headers=ADMIN)
    assert response.status_code == 404 and response.json()["code"] == "reset_not_found"


async def test_status_is_readable_while_the_run_goes_on(client, db, fake):
    await seed(db)
    response = await client.post(RESET, headers=ADMIN, json={"dry_run": False, "confirm": CONFIRM})
    assert response.status_code == 202
    run_id = response.json()["id"]
    state = (await client.get(f"{RESET}/{run_id}", headers=ADMIN)).json()
    assert state["id"] == run_id and state["status"] in {"running", "done"} and set(state) >= {"phase", "progress", "result", "error", "started_at", "options"}
    await server_reset._task
    assert (await client.get(f"{RESET}/{run_id}", headers=ADMIN)).json()["status"] == "done"
