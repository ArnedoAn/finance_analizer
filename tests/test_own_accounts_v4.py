"""Own accounts managed from the app (contract section 7): CRUD, scopes, validation, runtime source and one-time seed."""
import importlib.util
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, select

from app.core.config import OwnAccount, Settings
from app.core.exceptions import FireflyError
from app.db.models import InboxItem, OwnAccountRow
from app.db.models_assistant import Audit
from app.db.repositories import OwnAccountRepository
from app.services import audit, own_accounts
from tests.test_devices_v4 import API, auth, enroll_key

ADMIN_KEY = "test-admin-key-01234567890123456789"
PHONE_KEY = "test-phone-key-01234567890123456789"
ADMIN = auth(ADMIN_KEY)
PHONE = auth(PHONE_KEY)
URL = f"{API}/own-accounts"
MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "20261123_08_own_accounts.py"
BODY = {"firefly_account": "Bancolombia", "bank": "bancolombia", "kind": "savings", "last4": ["5678"], "aliases": ["bancolombia"], "sources": ["sms:87400"]}
NEQUI_SOURCES = ["app:com.nequi.MobileApp", "sms:85954", "email:nequi.com.co"]


async def create(client, **changes):
    return await client.post(URL, headers=ADMIN, json={**BODY, **changes})


async def listing(client):
    response = await client.get(URL, headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def account_id(client, name="Nequi"):
    return next(item["id"] for item in await listing(client) if item["firefly_account"] == name)


def sms(sender, text="Pagaste $12.000 en TIENDA"):
    return {"type": "sms", "sender": sender, "message": text}


async def webhook(client, payload):
    return await client.post(f"{API}/notifications/webhook", headers=PHONE, json=payload)


# --- CRUD -----------------------------------------------------------------------------------------------------------

async def test_crud_roundtrip(client):
    created = await create(client)
    assert created.status_code == 201, created.text
    body = created.json()
    assert set(body) == {"id", "firefly_account", "bank", "kind", "last4", "aliases", "sources", "statement_day", "payment_day", "firefly_exists", "updated_at"}
    assert isinstance(body["id"], int) and datetime.fromisoformat(body["updated_at"]).tzinfo is not None
    assert (body["firefly_account"], body["bank"], body["kind"], body["last4"], body["aliases"], body["sources"], body["firefly_exists"]) == (
        "Bancolombia", "bancolombia", "savings", ["5678"], ["bancolombia"], ["sms:87400"], True)

    items = await listing(client)
    assert [item["firefly_account"] for item in items] == ["Bancolombia", "Nequi"]
    assert [item["firefly_exists"] for item in items] == [True, True]
    assert items[1]["sources"] == NEQUI_SOURCES and items[1]["last4"] == ["1234"] and items[1]["kind"] == "wallet"

    url = f"{URL}/{body['id']}"
    patched = await client.patch(url, headers=ADMIN, json={"bank": "Bancolombia S.A.", "aliases": ["banco", "ahorros"]})
    assert patched.status_code == 200, patched.text
    after = patched.json()
    assert after["bank"] == "Bancolombia S.A." and after["aliases"] == ["banco", "ahorros"]
    assert (after["kind"], after["last4"], after["sources"], after["id"]) == ("savings", ["5678"], ["sms:87400"], body["id"])
    assert datetime.fromisoformat(after["updated_at"]) >= datetime.fromisoformat(body["updated_at"])

    unchanged = await client.patch(url, headers=ADMIN, json={})
    assert unchanged.status_code == 200 and unchanged.json()["bank"] == "Bancolombia S.A."
    cleared = await client.patch(url, headers=ADMIN, json={"sources": [], "last4": []})
    assert cleared.json()["sources"] == [] and cleared.json()["last4"] == []

    deleted = await client.delete(url, headers=ADMIN)
    assert deleted.status_code == 200 and deleted.json() == {"deleted": True}
    assert [item["firefly_account"] for item in await listing(client)] == ["Nequi"]
    gone = await client.delete(url, headers=ADMIN)
    assert gone.status_code == 404 and gone.json()["code"] == "own_account_not_found"


async def test_unknown_id_is_404(client):
    for response in (await client.patch(f"{URL}/9999", headers=ADMIN, json={"bank": "x"}), await client.delete(f"{URL}/9999", headers=ADMIN)):
        assert response.status_code == 404
        assert response.json()["code"] == "own_account_not_found" and response.json()["detail"]


async def test_empty_table_lists_nothing_and_never_asks_firefly(client, clients):
    await client.delete(f"{URL}/{await account_id(client)}", headers=ADMIN)
    clients[0].accounts.reset_mock()
    assert (await client.get(URL, headers=ADMIN)).json() == {"items": []}
    clients[0].accounts.assert_not_awaited()


# --- Scopes ---------------------------------------------------------------------------------------------------------

async def test_scopes_ingest_none_read_get_only_review_writes(client):
    nequi = await account_id(client)
    ingest = auth((await enroll_key(client, ["ingest"], "capture"))["key"])
    reader = auth((await enroll_key(client, ["read"], "reader"))["key"])
    reviewer = auth((await enroll_key(client, ["read", "review"], "owner"))["key"])

    assert (await client.get(URL)).status_code == 401
    for response in (
        await client.get(URL, headers=ingest), await client.post(URL, headers=ingest, json=BODY),
        await client.patch(f"{URL}/{nequi}", headers=ingest, json={"bank": "x"}), await client.delete(f"{URL}/{nequi}", headers=ingest),
        await client.post(URL, headers=reader, json=BODY), await client.patch(f"{URL}/{nequi}", headers=reader, json={"bank": "x"}),
        await client.delete(f"{URL}/{nequi}", headers=reader),
    ):
        assert response.status_code == 403 and response.json()["code"] == "insufficient_scope"
    assert (await client.get(URL, headers=reader)).status_code == 200
    assert [item["firefly_account"] for item in await listing(client)] == ["Nequi"]

    assert (await client.post(URL, headers=reviewer, json=BODY)).status_code == 201
    assert (await client.patch(f"{URL}/{nequi}", headers=reviewer, json={"bank": "nequi s.a."})).status_code == 200
    assert (await client.get(URL, headers=reviewer)).status_code == 200
    assert (await client.delete(f"{URL}/{await account_id(client, 'Bancolombia')}", headers=reviewer)).json() == {"deleted": True}


# --- Validation -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("source", [
    "app:nopackage", "app:com", "app:1com.bank", "app:com..bank", "app:com.bank app", "app:", "app:com.bank-app",
    "sms:", "sms:    ", "sms:" + "9" * 65, "sms:12\n34",
    "email:gmail.com", "email:someone@gmail.com", "email:no-domain", "email:@bank.test", "email:", "email:email:bank.test",
    "telegram:123", "85954", "nequi.com.co", "",
])
async def test_invalid_sources_are_rejected_on_create_and_update(client, source):
    response = await create(client, sources=[source])
    assert response.status_code == 422 and response.json()["code"] == "invalid_source" and response.json()["detail"]
    nequi = await account_id(client)
    response = await client.patch(f"{URL}/{nequi}", headers=ADMIN, json={"sources": ["sms:1", source]})
    assert response.status_code == 422 and response.json()["code"] == "invalid_source"
    items = await listing(client)
    assert [item["firefly_account"] for item in items] == ["Nequi"] and items[0]["sources"] == NEQUI_SOURCES


async def test_sources_are_stored_normalized_without_duplicates(client):
    response = await create(client, sources=[
        "APP:Com.Bank.App", "sms: 85954 ", "email:Alertas@Banco.COM.co", "app:com.bank.app", "email:banco.com.co", "SMS:85954", "sms:" + "x" * 64,
    ])
    assert response.status_code == 201, response.text
    assert response.json()["sources"] == ["app:Com.Bank.App", "sms:85954", "email:alertas@banco.com.co", "email:banco.com.co", "sms:" + "x" * 64]
    patched = await client.patch(f"{URL}/{response.json()['id']}", headers=ADMIN, json={"sources": ["  EMAIL: Banco.com.co ", "sms:A1B2"]})
    assert patched.json()["sources"] == ["email:banco.com.co", "sms:A1B2"]


@pytest.mark.parametrize("changes", [
    {"last4": ["12a4"]}, {"last4": ["123"]}, {"last4": ["12345"]}, {"last4": ["1234"] * 11},
    {"aliases": [""]}, {"aliases": ["   "]}, {"aliases": ["x" * 61]}, {"aliases": ["a"] * 21},
    {"sources": ["sms:1"] * 31}, {"kind": "loan"}, {"bank": ""}, {"bank": "b" * 61},
    {"firefly_account": ""}, {"firefly_account": "f" * 256}, {"unknown": 1},
])
async def test_field_limits_are_422_validation_errors(client, changes):
    response = await create(client, **changes)
    assert response.status_code == 422 and response.json()["code"] == "validation_error"
    patched = await client.patch(f"{URL}/{await account_id(client)}", headers=ADMIN, json=changes)
    assert patched.status_code == 422 and patched.json()["code"] == "validation_error"
    assert [item["firefly_account"] for item in await listing(client)] == ["Nequi"]


async def test_required_fields_and_nulls(client):
    body = {key: value for key, value in BODY.items() if key != "kind"}
    assert (await client.post(URL, headers=ADMIN, json=body)).status_code == 422
    minimal = await client.post(URL, headers=ADMIN, json={"firefly_account": "Bancolombia", "bank": "b", "kind": "cash"})
    assert minimal.status_code == 201 and minimal.json()["sources"] == [] and minimal.json()["aliases"] == []
    nequi = await account_id(client)
    for field in ("bank", "kind", "last4", "aliases", "sources", "firefly_account"):
        response = await client.patch(f"{URL}/{nequi}", headers=ADMIN, json={field: None})
        assert response.status_code == 422 and response.json()["code"] == "validation_error"


# --- 409 and Firefly ------------------------------------------------------------------------------------------------

async def test_duplicate_name_is_409_ignoring_case(client):
    assert (await create(client)).status_code == 201
    for name in ("Bancolombia", "bancolombia", "NEQUI", "  nequi "):
        response = await create(client, firefly_account=name)
        assert response.status_code == 409 and response.json()["code"] == "own_account_exists", name
    own = await account_id(client, "Bancolombia")
    clash = await client.patch(f"{URL}/{own}", headers=ADMIN, json={"firefly_account": "NEQUI"})
    assert clash.status_code == 409 and clash.json()["code"] == "own_account_exists"
    recased = await client.patch(f"{URL}/{own}", headers=ADMIN, json={"firefly_account": "BANCOLOMBIA"})
    assert recased.status_code == 200 and recased.json()["firefly_account"] == "BANCOLOMBIA"
    assert [item["firefly_account"] for item in await listing(client)] == ["BANCOLOMBIA", "Nequi"]


async def test_firefly_account_must_exist_as_asset_or_liability(client, clients):
    for name in ("Fantasma", "SUPERMERCADO CENTRAL", "SALARIO"):
        response = await create(client, firefly_account=name)
        assert response.status_code == 422 and response.json()["code"] == "firefly_account_missing", name
    nequi = await account_id(client)
    renamed = await client.patch(f"{URL}/{nequi}", headers=ADMIN, json={"firefly_account": "Fantasma"})
    assert renamed.status_code == 422 and renamed.json()["code"] == "firefly_account_missing"
    assert [item["firefly_account"] for item in await listing(client)] == ["Nequi"]
    clients[0].create_account.assert_not_awaited()

    clients[0].accounts.return_value = [*clients[0].accounts.return_value, {"id": "9", "name": "Tarjeta Nu", "type": "liabilities"}]
    card = await create(client, firefly_account="tarjeta nu", kind="credit", sources=[])
    assert card.status_code == 201 and card.json()["firefly_exists"] is True


async def test_firefly_outage_does_not_block_and_reads_as_unknown(client, clients):
    clients[0].accounts.side_effect = FireflyError("down")
    created = await create(client, firefly_account="Fantasma")
    assert created.status_code == 201 and created.json()["firefly_exists"] is None
    assert [item["firefly_exists"] for item in await listing(client)] == [None, None]

    clients[0].accounts.side_effect = None
    fantasma = created.json()["id"]
    edited = await client.patch(f"{URL}/{fantasma}", headers=ADMIN, json={"bank": "otro"})
    assert edited.status_code == 200 and edited.json()["firefly_exists"] is False
    assert {item["firefly_account"]: item["firefly_exists"] for item in await listing(client)} == {"Fantasma": False, "Nequi": True}


# --- Audit ----------------------------------------------------------------------------------------------------------

async def test_every_write_is_audited_without_sensitive_data(client):
    created = (await create(client)).json()
    url = f"{URL}/{created['id']}"
    await client.patch(url, headers=ADMIN, json={"bank": "BCOL", "sources": ["sms:87400", "app:com.bancolombia.app"]})
    await client.delete(url, headers=ADMIN)
    entries = (await client.get(f"{API}/audit", headers=ADMIN, params={"limit": 500})).json()["items"]
    by_action = {entry["action"]: entry for entry in entries if entry["action"].startswith("own_account.")}
    assert set(by_action) == {"own_account.add", "own_account.update", "own_account.delete"}
    assert all(entry["target_type"] == "own_account" and entry["target_id"] == str(created["id"]) and entry["actor"] == "admin" for entry in by_action.values())
    assert by_action["own_account.add"]["detail"] == {"firefly_account": "Bancolombia", "sources": 1}
    assert by_action["own_account.update"]["detail"] == {"firefly_account": "Bancolombia", "changed": ["bank", "sources"], "sources": 2}
    assert by_action["own_account.delete"]["detail"] == {"firefly_account": "Bancolombia", "sources": 2}
    dumped = json.dumps([entry["detail"] for entry in by_action.values()])
    assert "87400" not in dumped and "com.bancolombia" not in dumped and "5678" not in dumped


# --- The table is the runtime source --------------------------------------------------------------------------------

async def test_source_added_through_the_api_applies_to_the_next_ingest(client, db):
    first = await webhook(client, sms("99999"))
    assert first.status_code == 200 and first.json()["accepted"] is False and first.json()["status"] == "ignored"
    ignored = await db.get(InboxItem, first.json()["inbox_id"], populate_existing=True)
    assert ignored.status == "ignored" and ignored.raw_text is None

    nequi = await account_id(client)
    patched = await client.patch(f"{URL}/{nequi}", headers=ADMIN, json={"sources": [*NEQUI_SOURCES, "sms:99999"]})
    assert patched.status_code == 200

    second = await webhook(client, sms("99999"))
    assert second.status_code == 202 and second.json()["accepted"] is True and second.json()["status"] == "queued"
    accepted = await db.get(InboxItem, second.json()["inbox_id"], populate_existing=True)
    assert accepted.status == "created" and accepted.asset_account == "Nequi"


async def test_removed_source_and_deleted_account_stop_applying_at_once(client):
    assert (await webhook(client, sms("85954"))).status_code == 202
    nequi = await account_id(client)
    await client.patch(f"{URL}/{nequi}", headers=ADMIN, json={"sources": ["app:com.nequi.MobileApp"]})
    assert (await webhook(client, sms("85954"))).json()["status"] == "ignored"
    assert (await webhook(client, {"type": "notification", "app": "com.nequi.MobileApp", "message": "Pagaste $12.000"})).status_code == 202
    await client.delete(f"{URL}/{nequi}", headers=ADMIN)
    assert (await webhook(client, {"type": "notification", "app": "com.nequi.MobileApp", "message": "Pagaste $12.000"})).json()["status"] == "ignored"


async def test_new_account_resolves_its_own_source(client, db):
    assert (await create(client, last4=["5678"], sources=["sms:87400"])).status_code == 201
    response = await webhook(client, sms("87400"))
    assert response.status_code == 202
    # Only Bancolombia lists this sender: the parser's hint (Nequi's last4) cannot move the item to another account.
    item = await db.get(InboxItem, response.json()["inbox_id"], populate_existing=True)
    assert item.status == "created" and item.asset_account == "Bancolombia"


async def test_email_sources_become_trusted_sender_rules(client, service):
    assert (await create(client, sources=["email:alertas@bancolombia.com.co", "email:nequi.com.co"])).status_code == 201
    keywords = sorted(rule.keyword for rule in own_accounts.email_rules(await service._own()))
    assert keywords == ["alertas@bancolombia.com.co", "nequi.com.co", "nequi.com.co"]
    await client.delete(f"{URL}/{await account_id(client, 'Bancolombia')}", headers=ADMIN)
    assert [rule.keyword for rule in own_accounts.email_rules(await service._own())] == ["nequi.com.co"]


async def test_quick_entry_uses_accounts_added_through_the_api(client):
    assert (await create(client, aliases=["banco"], sources=[])).status_code == 201
    response = await client.post(f"{API}/transactions/quick", headers=ADMIN, json={"text": "25k taxi banco", "client_ref": "own-accounts-1"})
    assert response.status_code == 201, response.text
    assert response.json()["asset_account"] == "Bancolombia"


async def test_health_missing_accounts_come_from_the_table(client, clients, add_own_account, monkeypatch):
    from app.api.routers import system

    monkeypatch.setattr(system, "get_firefly", lambda: clients[0])
    monkeypatch.setattr(system, "get_llm", lambda: clients[1])
    await add_own_account("Fantasma")
    health = await client.get(f"{API}/health", headers=ADMIN)
    assert health.json()["services"]["missing_own_accounts"] == ["Fantasma"]
    await client.delete(f"{URL}/{await account_id(client, 'Fantasma')}", headers=ADMIN)
    assert (await client.get(f"{API}/health", headers=ADMIN)).json()["services"]["missing_own_accounts"] == []


# --- Snapshot -------------------------------------------------------------------------------------------------------

async def test_processor_loads_lazily_so_no_path_sees_an_empty_list(service):
    own_accounts.invalidate()
    assert [a.firefly_account for a in await service._source_accounts("sms", "85954")] == ["Nequi"]
    own_accounts.invalidate()
    item = SimpleNamespace(source="notification", app="com.nequi.MobileApp", sender="")
    assert (await service._resolve(item, "1234")).firefly_account == "Nequi"
    own_accounts.invalidate()
    assert (await service._counterparty("nequi", "Otra")).firefly_account == "Nequi"


async def test_snapshot_is_shared_until_a_write_invalidates_it(db, add_own_account):
    first = await own_accounts.load(db)
    assert await own_accounts.load(db) is first
    await add_own_account("Bancolombia")
    second = await own_accounts.load(db)
    assert second is not first and [a.firefly_account for a in second] == ["Bancolombia", "Nequi"]
    assert second[1].sources == tuple(NEQUI_SOURCES) and isinstance(second[1].last4, tuple)


async def test_a_load_overtaken_by_a_write_is_not_cached(db, monkeypatch):
    real = OwnAccountRepository.all

    async def overtaken(self):
        rows = await real(self)
        own_accounts.invalidate()  # a write commits while this query is in flight
        return rows

    monkeypatch.setattr(OwnAccountRepository, "all", overtaken)
    await own_accounts.load(db)
    assert own_accounts._snapshot is None
    monkeypatch.setattr(OwnAccountRepository, "all", real)
    await own_accounts.load(db)
    assert own_accounts._snapshot is not None


@pytest.mark.parametrize(("source", "identity", "expected"), [
    ("sms", "85954", ["Nequi"]), ("sms", "85955", []), ("notification", "COM.NEQUI.MOBILEAPP", ["Nequi"]),
    ("email", "alertas@nequi.com.co", ["Nequi"]), ("email", "alertas@mail.nequi.com.co", ["Nequi"]),
    ("email", "alertas@evilnequi.com.co", []), ("email", "x@bank.test", []),
])
async def test_for_source_matches_like_the_processor_always_did(db, source, identity, expected):
    accounts = await own_accounts.load(db)
    assert [a.firefly_account for a in own_accounts.for_source(accounts, source, identity)] == expected


# --- Seed from OWN_ACCOUNTS -----------------------------------------------------------------------------------------

def seed_settings(*accounts):
    return Settings(finance_api_key=ADMIN_KEY, phone_api_key=PHONE_KEY, mail_accounts=[], own_accounts=list(accounts), _env_file=None)


async def seed_entries(db):
    return (await db.scalars(select(Audit).where(Audit.action == "own_account.seed"))).all()


async def test_seed_imports_the_settings_once_and_is_audited(db):
    await db.execute(delete(OwnAccountRow))
    await db.commit()
    own_accounts.invalidate()
    assert await own_accounts.load(db) == ()  # an empty snapshot is cached; the seed must drop it
    configured = seed_settings(
        OwnAccount(firefly_account="Nequi", bank="nequi", kind="wallet", last4=["1234"], aliases=["nequi"],
                   sources=["app:com.nequi.MobileApp", "SMS:85954", "email:Nequi.com.co", "email:gmail.com"]),
        OwnAccount(firefly_account="Bancolombia", bank="bancolombia", kind="savings", sources=["sms:87400"]),
        OwnAccount(firefly_account="NEQUI", bank="duplicate", kind="cash"),
    )
    assert await own_accounts.seed(db, configured) == 2
    loaded = await own_accounts.load(db)
    assert [a.firefly_account for a in loaded] == ["Bancolombia", "Nequi"]
    nequi = loaded[1]
    assert (nequi.bank, nequi.kind, nequi.last4, nequi.aliases) == ("nequi", "wallet", ("1234",), ("nequi",))
    assert nequi.sources == ("app:com.nequi.MobileApp", "sms:85954", "email:nequi.com.co")
    entry, = await seed_entries(db)
    assert entry.actor == "system" and entry.detail == {"count": 2, "accounts": ["Nequi", "Bancolombia"], "skipped_sources": 1}

    # Once rows exist the settings are never read again, whatever they say.
    other = seed_settings(OwnAccount(firefly_account="Otra", bank="x", kind="cash"))
    assert await own_accounts.seed(db, other) == 0
    assert [a.firefly_account for a in await own_accounts.load(db)] == ["Bancolombia", "Nequi"]
    assert len(await seed_entries(db)) == 1


async def test_seed_does_nothing_without_settings_or_over_existing_rows(db):
    assert await own_accounts.seed(db, seed_settings(OwnAccount(firefly_account="Otra", bank="x", kind="cash"))) == 0  # Nequi is there
    await db.execute(delete(OwnAccountRow))
    await db.commit()
    own_accounts.invalidate()
    assert await own_accounts.seed(db, seed_settings()) == 0
    assert await OwnAccountRepository(db).count() == 0 and await seed_entries(db) == []


async def test_seed_never_resurrects_accounts_the_owner_deleted(client, db):
    configured = seed_settings(OwnAccount(firefly_account="Nequi", bank="nequi", kind="wallet", sources=["sms:85954"]))
    for item in await listing(client):  # the owner empties the list from the app; the file is still mounted at the next start
        assert (await client.delete(f"{URL}/{item['id']}", headers=ADMIN)).status_code == 200
    assert await listing(client) == []
    assert await own_accounts.seed(db, configured) == 0
    assert await listing(client) == [] and await seed_entries(db) == []


async def test_seed_is_blocked_by_any_own_account_audit_entry_and_only_by_those(db):
    configured = seed_settings(OwnAccount(firefly_account="Otra", bank="x", kind="cash"))
    await db.execute(delete(OwnAccountRow))
    await audit.record(db, action="sender.add", target_type="sender", target_id=1)  # unrelated writes do not count
    await db.commit()
    own_accounts.invalidate()
    assert await own_accounts.seed(db, configured) == 1
    for action in ("own_account.add", "own_account.update", "own_account.delete"):
        await db.execute(delete(OwnAccountRow))
        await db.execute(delete(Audit).where(Audit.action.startswith("own_account.")))
        await audit.record(db, action=action, target_type="own_account", target_id=1)
        await db.commit()
        own_accounts.invalidate()
        assert await own_accounts.seed(db, configured) == 0, action
        assert await OwnAccountRepository(db).count() == 0


async def test_seeded_accounts_work_without_a_restart(client, db):
    await db.execute(delete(OwnAccountRow))
    await db.commit()
    own_accounts.invalidate()
    assert (await webhook(client, sms("85954"))).json()["status"] == "ignored"
    await own_accounts.seed(db, seed_settings(OwnAccount(firefly_account="Nequi", bank="nequi", kind="wallet", sources=["sms:85954"])))
    assert (await webhook(client, sms("85954"))).status_code == 202


# --- Migration ------------------------------------------------------------------------------------------------------

def test_migration_matches_the_model_and_enforces_unique_names_ignoring_case():
    spec = importlib.util.spec_from_file_location("own_accounts_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (module.revision, module.down_revision) == ("20261123_08", "20261122_07")
    assert [p for p in MIGRATION.parent.glob("*.py") if 'down_revision = "20261122_07"' in p.read_text()] == [MIGRATION]

    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        module.upgrade()
    # statement_day and payment_day arrive with the quality migration (20261125_15), tested in tests/test_quality_v4.py
    assert {c["name"] for c in sa.inspect(engine).get_columns("own_accounts")} == {c.name for c in OwnAccountRow.__table__.columns} - {"statement_day", "payment_day"}
    insert = sa.text("INSERT INTO own_accounts (firefly_account, bank, kind, last4, aliases, sources, created_at, updated_at) "
                     "VALUES (:name, 'b', 'wallet', '[]', '[]', '[]', '2026-11-23', '2026-11-23')")
    with engine.begin() as connection:
        connection.execute(insert, {"name": "Nequi"})
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(insert, {"name": "NEQUI"})
    engine.dispose()


def test_routes_declare_read_for_the_listing_and_review_for_writes():
    from app.main import app
    from tests.test_route_scopes import api_routes, route_scopes

    scopes = {
        (route.path, tuple(sorted(route.methods))): route_scopes(route)
        for route in api_routes(app) if route.path.startswith("/api/v1/own-accounts")
    }
    assert scopes == {
        ("/api/v1/own-accounts", ("GET",)): ["read"],
        ("/api/v1/own-accounts", ("POST",)): ["review"],
        ("/api/v1/own-accounts/{account_id}", ("PATCH",)): ["review"],
        ("/api/v1/own-accounts/{account_id}", ("DELETE",)): ["review"],
    }
