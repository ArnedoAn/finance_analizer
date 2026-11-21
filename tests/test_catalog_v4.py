"""Merchants, rules, transfer pairing and anomalies (Phase 5 A6, contract sections 7, 8 and 14)."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.exceptions import PermanentError
from app.core.timezone import now_utc
from app.db.models import InboxItem
from app.db.models_assistant import Audit
from app.db.models_rules import Merchant, Rule
from app.services import anomalies, rules, transfers

ADMIN = {"X-API-Key": "test-admin-key-01234567890123456789"}
PHONE = {"X-API-Key": "test-phone-key-01234567890123456789"}


async def make(service, **changes):
    values = dict(
        source="sms", source_ref=str(uuid4()), sender="85954", subject_or_title="Compra", raw_text=None,
        occurred_at=now_utc(), status="created", method="parser", transaction_type="withdrawal",
        amount=Decimal("12000.00"), currency="COP", merchant="SUPERMERCADO CENTRAL",
        asset_account="Nequi", firefly_transaction_id=str(uuid4()),
    )
    values.update(changes)
    row = await service.inbox.add(**values)
    await service.session.commit()
    return row


async def email_item(service, ref):
    row = await service.inbox.add(
        source="email", source_ref=ref, sender="alertas@nequi.com.co", raw_text="Pagaste $12.000 en SUPERMERCADO CENTRAL",
        subject_or_title="Compra", occurred_at=now_utc(), status="pending",
    )
    await service.session.commit()
    return row


# --- rules and merchants -------------------------------------------------------------------------


async def test_rule_assigns_category_during_processing(service, clients, db):
    await rules.create_rule(db, match="merchant_exact", pattern="supermercado central", category="Alimentación")
    await db.commit()
    row = await service.process(await email_item(service, "mail:rule"))
    assert row.status == "created" and row.category == "Alimentación"
    assert clients[0].create_transaction.call_args.args[0]["transactions"][0]["category_name"] == "Alimentación"
    assert (await db.scalar(select(Rule))).hits == 1


async def test_merchant_default_category_applies_after_rules(service, db):
    db.add(Merchant(name="SUPERMERCADO CENTRAL", default_category="Transporte", count=0))
    await db.commit()
    row = await service.process(await email_item(service, "mail:default"))
    assert row.category == "Transporte"


async def test_existing_category_is_never_overwritten(service, db):
    await rules.create_rule(db, match="merchant_exact", pattern="SUPERMERCADO CENTRAL", category="Alimentación")
    item = await make(service, status="pending", method=None, category="Transporte")
    await rules.apply(db, item)
    assert item.category == "Transporte" and item.status == "pending"


async def test_rule_priority_exact_then_contains_then_text_then_manual(service, db):
    await rules.create_rule(db, match="text_contains", pattern="compra", category="Transporte")
    await rules.create_rule(db, match="merchant_contains", pattern="SUPER", category="Transporte", source="learned")
    await rules.create_rule(db, match="merchant_contains", pattern="CENTRAL", category="Alimentación", source="manual")
    first = await make(service, status="pending", method=None, category=None)
    await rules.apply(db, first)
    assert first.category == "Alimentación"  # contains beats text; manual beats learned
    await rules.create_rule(db, match="merchant_exact", pattern="SUPERMERCADO CENTRAL", category="Transporte")
    second = await make(service, status="pending", method=None, category=None)
    await rules.apply(db, second)
    assert second.category == "Transporte"  # exact beats contains


async def test_merchant_count_is_idempotent_per_item(service, db):
    row = await service.process(await email_item(service, "mail:count"))
    merchant = await db.scalar(select(Merchant))
    assert (merchant.name, merchant.count) == ("SUPERMERCADO CENTRAL", 1)
    row.method, row.status = None, "pending"
    await db.commit()
    await service.process(row)
    await rules.apply(db, row)
    await db.refresh(merchant)
    assert merchant.count == 1
    await service.process(await email_item(service, "mail:count-2"))
    await db.refresh(merchant)
    assert merchant.count == 2 and merchant.last_seen_at is not None


async def test_merchants_and_rules_endpoints(client, service, db, clients):
    await service.process(await email_item(service, "mail:api"))
    listing = (await client.get("/api/v1/merchants?q=central", headers=ADMIN)).json()
    assert listing["total"] == 1
    merchant = listing["items"][0]
    assert set(merchant) == {"id", "name", "default_category", "count", "last_seen_at"} and merchant["count"] == 1

    bad = await client.put(f"/api/v1/merchants/{merchant['id']}", headers=ADMIN, json={"default_category": "Inexistente"})
    assert bad.status_code == 422
    assert (await client.put("/api/v1/merchants/999", headers=ADMIN, json={"default_category": "Transporte"})).status_code == 404
    done = await client.put(f"/api/v1/merchants/{merchant['id']}", headers=ADMIN, json={"default_category": "transporte"})
    assert done.status_code == 200
    body = done.json()
    assert body["merchant"]["default_category"] == "Transporte"
    assert body["rule"]["match"] == "merchant_exact" and body["rule"]["pattern"] == "SUPERMERCADO CENTRAL"
    assert set(body["rule"]) == {"id", "match", "pattern", "category", "merchant", "source", "hits", "created_at"}

    only_default = await client.put(f"/api/v1/merchants/{merchant['id']}", headers=ADMIN, json={"default_category": "Alimentación", "create_rule": False})
    assert only_default.json()["rule"] is None

    created = await client.post("/api/v1/rules", headers=ADMIN, json={"match": "text_contains", "pattern": "Uber", "category": "Transporte"})
    assert created.status_code == 201 and created.json()["source"] == "manual" and created.json()["hits"] == 0
    unknown = await client.post("/api/v1/rules", headers=ADMIN, json={"match": "text_contains", "pattern": "x", "category": "Nada"})
    assert unknown.status_code == 422
    invalid = await client.post("/api/v1/rules", headers=ADMIN, json={"match": "regex", "pattern": "x", "category": "Transporte"})
    assert invalid.status_code == 422
    assert (await client.get("/api/v1/rules", headers=ADMIN)).json()["total"] == 2

    deleted = await client.delete(f"/api/v1/rules/{created.json()['id']}", headers=ADMIN)
    assert deleted.json() == {"deleted": True}
    assert (await client.delete(f"/api/v1/rules/{created.json()['id']}", headers=ADMIN)).status_code == 404
    assert (await client.get("/api/v1/rules", headers=ADMIN)).json()["total"] == 1


async def test_scopes(client, monkeypatch):
    from app.api import dependencies

    rule = {"match": "text_contains", "pattern": "uber", "category": "Transporte"}
    assert (await client.get("/api/v1/rules", headers=PHONE)).status_code == 403
    assert (await client.get("/api/v1/anomalies", headers=PHONE)).status_code == 403

    async def reader(*_args, **_kwargs):
        return dependencies.Principal(None, "reader", frozenset({"read"}))

    monkeypatch.setattr(dependencies, "authenticate", reader)
    assert (await client.post("/api/v1/rules", headers=ADMIN, json=rule)).status_code == 403
    assert (await client.delete("/api/v1/rules/1", headers=ADMIN)).status_code == 403
    assert (await client.put("/api/v1/merchants/1", headers=ADMIN, json={"default_category": "Transporte"})).status_code == 403
    assert (await client.post("/api/v1/transfers/pair", headers=ADMIN, json={"out_id": 1, "in_id": 2})).status_code == 403
    for path in ("/merchants", "/rules", "/transfers/pending", "/anomalies"):
        assert (await client.get("/api/v1" + path, headers=ADMIN)).status_code == 200

    async def reviewer(*_args, **_kwargs):
        return dependencies.Principal(None, "reviewer", frozenset({"review"}))

    monkeypatch.setattr(dependencies, "authenticate", reviewer)
    assert (await client.post("/api/v1/rules", headers=ADMIN, json=rule)).status_code == 201


# --- transfers -----------------------------------------------------------------------------------


async def test_pairing_creates_a_single_transfer(client, service, db, clients):
    firefly = clients[0]
    now = now_utc()
    out = await make(service, amount=Decimal("50000.00"), merchant="TRANSFERENCIA", asset_account="Nequi", occurred_at=now, firefly_transaction_id="tx-out")
    incoming = await make(service, source="email", transaction_type="deposit", amount=Decimal("50000.00"), merchant="TRANSFERENCIA", asset_account="Bancolombia", occurred_at=now + timedelta(minutes=5), firefly_transaction_id="tx-in")
    # Not pairs: same account, other amount, outside the window, already a transfer.
    await make(service, amount=Decimal("7000.00"), asset_account="Nequi")
    await make(service, transaction_type="deposit", amount=Decimal("7000.00"), asset_account="Nequi")
    await make(service, amount=Decimal("99000.00"), asset_account="Nequi", occurred_at=now)
    await make(service, transaction_type="deposit", amount=Decimal("99000.00"), asset_account="Bancolombia", occurred_at=now + timedelta(minutes=120))

    pending = (await client.get("/api/v1/transfers/pending", headers=ADMIN)).json()["items"]
    assert len(pending) == 1
    assert pending[0]["out_item"]["id"] == out.id and pending[0]["in_item"]["id"] == incoming.id
    assert (pending[0]["amount"], pending[0]["currency"], pending[0]["minutes_apart"]) == ("50000.00", "COP", 5.0)

    response = await client.post("/api/v1/transfers/pair", headers=ADMIN, json={"out_id": out.id, "in_id": incoming.id})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["out_item"]["transaction_type"] == "transfer" and body["out_item"]["destination_account"] == "Bancolombia"
    assert body["out_item"]["status"] == "created"
    assert body["in_item"]["status"] == "duplicate" and body["in_item"]["duplicate_of_id"] == out.id
    firefly.update_transaction.assert_awaited_once()
    split = firefly.update_transaction.call_args.args[1]["transactions"][0]
    assert (split["type"], split["source_id"], split["destination_id"]) == ("transfer", "1", "4")
    firefly.delete_transaction.assert_awaited_once_with("tx-in")
    firefly.create_transaction.assert_not_awaited()

    transfers = await db.scalar(select(func.count()).select_from(InboxItem).where(InboxItem.transaction_type == "transfer", InboxItem.status == "created"))
    assert transfers == 1
    assert (await client.get("/api/v1/transfers/pending", headers=ADMIN)).json() == {"items": []}
    again = await client.post("/api/v1/transfers/pair", headers=ADMIN, json={"out_id": out.id, "in_id": incoming.id})
    assert again.status_code == 422


async def test_pairing_needs_review_outgoing_is_approved(client, service, db, clients):
    out = await make(service, status="needs_review", firefly_transaction_id=None, amount=Decimal("30000.00"), asset_account="Nequi")
    incoming = await make(service, status="needs_review", firefly_transaction_id=None, transaction_type="deposit", amount=Decimal("30000.00"), asset_account="Bancolombia")
    response = await client.post("/api/v1/transfers/pair", headers=ADMIN, json={"out_id": out.id, "in_id": incoming.id})
    assert response.status_code == 200, response.text
    assert response.json()["out_item"]["status"] == "created"
    clients[0].create_transaction.assert_awaited_once()
    assert clients[0].create_transaction.call_args.args[0]["transactions"][0]["type"] == "transfer"
    clients[0].delete_transaction.assert_not_awaited()
    assert response.json()["in_item"]["status"] == "duplicate"


async def test_pairing_validates_input(client, service):
    out = await make(service, amount=Decimal("1000.00"), asset_account="Nequi")
    same = await make(service, transaction_type="deposit", amount=Decimal("1000.00"), asset_account="Nequi")
    other = await make(service, transaction_type="deposit", amount=Decimal("2000.00"), asset_account="Bancolombia")
    for in_id in (same.id, other.id):
        assert (await client.post("/api/v1/transfers/pair", headers=ADMIN, json={"out_id": out.id, "in_id": in_id})).status_code == 422
    assert (await client.post("/api/v1/transfers/pair", headers=ADMIN, json={"out_id": out.id, "in_id": 9999})).status_code == 404


async def test_pairing_leaves_the_incoming_leg_alone_when_the_outgoing_one_is_not_created(service, clients):
    clients[0].create_transaction.side_effect = PermanentError("Firefly 422: invalid destination")
    out = await make(service, status="needs_review", firefly_transaction_id=None, amount=Decimal("30000.00"), asset_account="Nequi")
    incoming = await make(service, transaction_type="deposit", amount=Decimal("30000.00"), asset_account="Bancolombia", firefly_transaction_id="tx-in")
    out_id, in_id = out.id, incoming.id
    with pytest.raises(transfers.PairConflictError):
        await transfers.pair(service, out_id, in_id)
    clients[0].delete_transaction.assert_not_awaited()
    kept = await service.inbox.get(in_id)
    assert kept.status == "created" and kept.duplicate_of_id is None and kept.firefly_transaction_id == "tx-in"
    assert (await service.inbox.get(out_id)).status == "needs_review"


async def test_pairing_conflict_is_a_409_conflict(client, service, clients):
    clients[0].create_transaction.side_effect = PermanentError("Firefly 422: invalid destination")
    out = await make(service, status="needs_review", firefly_transaction_id=None, amount=Decimal("30000.00"), asset_account="Nequi")
    incoming = await make(service, transaction_type="deposit", amount=Decimal("30000.00"), asset_account="Bancolombia", firefly_transaction_id="tx-in")
    response = await client.post("/api/v1/transfers/pair", headers=ADMIN, json={"out_id": out.id, "in_id": incoming.id})
    assert response.status_code == 409 and response.json()["code"] == "conflict"
    clients[0].delete_transaction.assert_not_awaited()
    assert (await service.inbox.get(incoming.id)).status == "created"


async def test_pairing_audits_every_review_step_as_the_caller(service, db, clients):
    out = await make(service, amount=Decimal("40000.00"), asset_account="Nequi")
    incoming = await make(service, transaction_type="deposit", amount=Decimal("40000.00"), asset_account="Bancolombia")
    principal = SimpleNamespace(name="alice", device_id=7)
    await transfers.pair(service, out.id, incoming.id, principal=principal)
    rows = (await db.execute(select(Audit.action, Audit.actor, Audit.device_id).where(Audit.action.in_(("inbox.edit", "inbox.void"))))).all()
    assert sorted(rows) == [("inbox.edit", "alice", 7), ("inbox.void", "alice", 7)]


# --- anomalies -----------------------------------------------------------------------------------


async def test_unusual_amount_detection(client, service, db, settings):
    now = now_utc()
    for index, amount in enumerate(("10000", "11000", "12000", "10500", "11500", "10000")):
        await make(service, amount=Decimal(amount), category="Alimentación", merchant=f"TIENDA {index}", occurred_at=now - timedelta(days=10 + index))
    normal = await make(service, amount=Decimal("11000"), category="Alimentación", merchant="TIENDA 1", occurred_at=now - timedelta(days=2))
    big = await make(service, amount=Decimal("90000"), category="Alimentación", merchant="TIENDA 2", occurred_at=now - timedelta(hours=1))
    few = await make(service, amount=Decimal("900000"), category="Otra", merchant="TIENDA 3", occurred_at=now)

    found = await anomalies.detect(db, settings)
    unusual = [entry for entry in found if entry["kind"] == "unusual_amount"]
    assert [entry["item_id"] for entry in unusual] == [big.id]
    entry = unusual[0]
    assert entry["key"] == f"unusual_amount:{big.id}" and entry["amount"] == "90000.00" and entry["currency"] == "COP"
    assert entry["merchant"] == "TIENDA 2" and entry["category"] == "Alimentación" and entry["score"] > 3
    assert entry["related_item_ids"] == [] and entry["occurred_at"].endswith("+00:00")
    assert normal.id not in {e["item_id"] for e in found} and few.id not in {e["item_id"] for e in found}
    assert [e["key"] for e in await anomalies.detect(db, settings)] == [e["key"] for e in found]

    response = await client.get("/api/v1/anomalies", headers=ADMIN)
    assert response.status_code == 200
    assert {e["key"] for e in response.json()["items"]} == {e["key"] for e in found}
    recent = await client.get("/api/v1/anomalies", headers=ADMIN, params={"since": (now - timedelta(hours=3)).isoformat()})
    assert big.id in {e["item_id"] for e in recent.json()["items"]}


async def test_new_merchant_above_category_p90(service, db, settings):
    now = now_utc()
    for index in range(6):
        await make(service, amount=Decimal(10000 + index * 1000), category="Alimentación", merchant="HABITUAL", occurred_at=now - timedelta(days=10 + index))
    new = await make(service, amount=Decimal("80000"), category="Alimentación", merchant="LUGAR NUEVO", occurred_at=now)
    cheap = await make(service, amount=Decimal("9000"), category="Alimentación", merchant="OTRO NUEVO", occurred_at=now)
    found = {entry["item_id"]: entry for entry in await anomalies.detect(db, settings) if entry["kind"] == "new_merchant"}
    assert set(found) == {new.id} and found[new.id]["key"] == f"new_merchant:{new.id}"
    assert cheap.id not in found


async def test_duplicate_charge_from_distinct_sources(client, service, db, settings):
    now = now_utc()
    first = await make(service, source="sms", merchant="TIENDA X", amount=Decimal("25000"), occurred_at=now - timedelta(minutes=3))
    second = await make(service, source="notification", merchant="TIENDA X", amount=Decimal("25000"), occurred_at=now)
    await make(service, source="sms", merchant="TIENDA Y", amount=Decimal("5000"), occurred_at=now - timedelta(minutes=2))
    await make(service, source="sms", merchant="TIENDA Y", amount=Decimal("5000"), occurred_at=now)  # same source
    await make(service, source="sms", merchant="TIENDA Z", amount=Decimal("6000"), occurred_at=now - timedelta(minutes=30))
    await make(service, source="email", merchant="TIENDA Z", amount=Decimal("6000"), occurred_at=now)  # too far apart
    duplicates = [entry for entry in await anomalies.detect(db, settings) if entry["kind"] == "duplicate_charge"]
    assert len(duplicates) == 1
    entry = duplicates[0]
    assert entry["item_id"] == second.id and entry["related_item_ids"] == [first.id]
    assert entry["key"] == f"duplicate_charge:{first.id}:{second.id}" and entry["merchant"] == "TIENDA X"
    listed = (await client.get("/api/v1/anomalies", headers=ADMIN)).json()["items"]
    assert [e["key"] for e in listed if e["kind"] == "duplicate_charge"] == [entry["key"]]


async def test_alert_check_emits_with_dedupe_key(service, db, settings, monkeypatch):
    alerts = pytest.importorskip("app.services.alerts")
    now = now_utc()
    await make(service, source="sms", merchant="TIENDA X", amount=Decimal("25000"), occurred_at=now - timedelta(minutes=3))
    await make(service, source="notification", merchant="TIENDA X", amount=Decimal("25000"), occurred_at=now)
    calls = []

    async def emit(session, type, **kwargs):
        calls.append((type, kwargs))
        return object()

    monkeypatch.setattr(alerts, "emit", emit)
    assert await anomalies.alert_check(db, settings, None) == 1
    kind, kwargs = calls[0]
    assert kind == "duplicate_suspect" and kwargs["dedupe_key"].startswith("duplicate_charge:")
    assert kwargs["currency"] == "COP" and kwargs["amount"] == Decimal("25000.00")


async def test_merchant_count_and_rule_hits_increment_atomically(service, db):
    from sqlalchemy import update

    rule = await rules.create_rule(db, match="merchant_exact", pattern="SUPERMERCADO CENTRAL", category="Transporte")
    merchant = Merchant(name="SUPERMERCADO CENTRAL", count=5)
    db.add(merchant)
    await db.commit()
    # Another worker bumps both counters behind this session's back; a read-modify-write would lose it.
    await db.execute(update(Merchant).values(count=10).execution_options(synchronize_session=False))
    await db.execute(update(Rule).values(hits=10).execution_options(synchronize_session=False))
    item = await make(service, category=None)
    await rules.apply(db, item)
    assert merchant.count == 11 and rule.hits == 11
    assert item.category == "Transporte"


async def test_text_contains_rule_matches_the_raw_text(service, db):
    await rules.create_rule(db, match="text_contains", pattern="pagaste", category="Transporte")
    await db.commit()
    item = await make(service, category=None, subject_or_title="Compra", raw_text="Pagaste $12.000 en tienda")
    await rules.apply(db, item)
    assert item.category == "Transporte"


async def test_merchants_limit_accepts_up_to_500(client):
    assert (await client.get("/api/v1/merchants?limit=500", headers=ADMIN)).status_code == 200
    assert (await client.get("/api/v1/merchants?limit=501", headers=ADMIN)).status_code == 422
    assert (await client.get("/api/v1/merchants?limit=0", headers=ADMIN)).status_code == 422
