"""Phase 4: assistant batches, proposals, apply/revert and the audit log (contract sections 9 and 14)."""
import json
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.api.dependencies import hash_secret
from app.core.exceptions import FireflyError
from app.core.timezone import now_utc
from app.db.models_assistant import ProposalBatch
from app.db.models_devices import Device

ADMIN = {"X-API-Key": "test-admin-key-01234567890123456789"}
PHONE = {"X-API-Key": "test-phone-key-01234567890123456789"}
API = "/api/v1/assistant/batches"


async def make(service, **changes):
    values = dict(
        source="sms", source_ref=str(uuid4()), sender="85954", subject_or_title="Compra", raw_text="Compra 1234567890123 en SUPERMERCADO",
        occurred_at=now_utc() - timedelta(hours=1), status="created", method="parser", transaction_type="withdrawal",
        amount=Decimal("12000.00"), currency="COP", merchant="SUPERMERCADO CENTRAL", asset_account="Nequi",
        category="Alimentación", firefly_transaction_id=str(uuid4()),
    )
    values.update(changes)
    row = await service.inbox.add(**values)
    await service.session.commit()
    return row


async def device(db, name, scopes):
    key = f"{name}-key-" + "0123456789" * 3
    db.add(Device(name=name, key_hash=hash_secret(key), scopes=scopes))
    await db.commit()
    return {"X-API-Key": key}


async def open_batch(client, headers=ADMIN, hours=48, instruction="recategoriza"):
    now = now_utc()
    response = await client.post(API, headers=headers, json={"window": {"from": (now - timedelta(hours=hours)).isoformat(), "to": now.isoformat()}, "instruction": instruction})
    assert response.status_code == 201, response.text
    return response.json()


async def propose(client, batch_id, target_id, field, new_value=None, headers=ADMIN):
    return await client.post(f"{API}/{batch_id}/proposals", headers=headers, json={"target_id": target_id, "field": field, "new_value": new_value, "reason": "porque sí"})


async def proposal_id(client, batch_id, target_id, field, new_value=None, headers=ADMIN):
    response = await propose(client, batch_id, target_id, field, new_value, headers)
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def apply(client, batch_id, ids, headers=ADMIN, **extra):
    return await client.post(f"{API}/{batch_id}/apply", headers=headers, json={"approved_ids": ids, **extra})


async def audit_actions(client):
    items = (await client.get("/api/v1/audit", headers=ADMIN, params={"limit": 500})).json()["items"]
    return {item["action"] for item in items}


# --- batches ---------------------------------------------------------------------------------------


async def test_batch_window_is_validated_and_fixed(client):
    now = now_utc()
    body = {"instruction": "x"}
    too_long = await client.post(API, headers=ADMIN, json={**body, "window": {"from": (now - timedelta(days=32)).isoformat(), "to": now.isoformat()}})
    assert too_long.status_code == 422 and too_long.json()["code"] == "invalid_window"
    future = await client.post(API, headers=ADMIN, json={**body, "window": {"from": now.isoformat(), "to": (now + timedelta(hours=1)).isoformat()}})
    assert future.status_code == 422 and future.json()["code"] == "invalid_window"
    backwards = await client.post(API, headers=ADMIN, json={**body, "window": {"from": now.isoformat(), "to": (now - timedelta(hours=1)).isoformat()}})
    assert backwards.status_code == 422 and backwards.json()["code"] == "invalid_window"
    naive = await client.post(API, headers=ADMIN, json={**body, "window": {"from": "2026-01-01T00:00:00", "to": "2026-01-02T00:00:00"}})
    assert naive.status_code == 422 and naive.json()["code"] == "validation_error"

    exactly = await client.post(API, headers=ADMIN, json={**body, "window": {"from": (now - timedelta(days=31)).isoformat(), "to": now.isoformat()}})
    assert exactly.status_code == 201
    batch = exactly.json()
    assert batch["status"] == "open" and batch["proposals"] == [] and batch["applied_at"] is None and batch["revert_deadline"] is None
    assert set(batch["window"]) == {"from", "to"} and batch["instruction"] == "x" and batch["id"]


async def test_unknown_batch_is_a_coded_404(client):
    response = await client.get(f"{API}/999", headers=ADMIN)
    assert response.status_code == 404 and response.json()["code"] == "not_found"


# --- proposals -------------------------------------------------------------------------------------


async def test_proposal_outside_the_window_is_rejected(client, service):
    old = await make(service, occurred_at=now_utc() - timedelta(days=10))
    batch = await open_batch(client, hours=48)
    response = await propose(client, batch["id"], old.id, "category", "Transporte")
    assert response.status_code == 422
    assert response.json()["code"] == "outside_window"


async def test_proposal_field_and_value_validation(client, service):
    item = await make(service)
    batch_id = (await open_batch(client))["id"]
    response = await propose(client, batch_id, item.id, "status", "created")
    assert response.status_code == 422 and response.json()["code"] == "field_not_allowed"
    for field, value in (
        ("amount", "abc"), ("amount", -5), ("amount", "10.555"), ("category", 12), ("category", ""), ("tags", "casa"), ("tags", ["x"] * 11),
        ("occurred_at", "2026-01-01T10:00:00"), ("transaction_type", "gift"), ("void", "yes"), ("split", [{"amount": "12000", "category": "Transporte"}]),
        ("split", [{"amount": "5000", "category": "Transporte"}, {"amount": "5000", "category": "Transporte"}]),
    ):
        response = await propose(client, batch_id, item.id, field, value)
        assert response.status_code == 422 and response.json()["code"] == "validation_error", (field, value)
    assert (await propose(client, batch_id, 999999, "category", "Transporte")).json()["code"] == "not_found"
    assert (await client.get(f"{API}/{batch_id}", headers=ADMIN)).json()["proposals"] == []


async def test_proposal_shape_and_risk_flags(client, service):
    item = await make(service, tags=["casa"])
    batch_id = (await open_batch(client))["id"]
    cases = {
        "category": ("Transporte", False, "Alimentación"),
        "merchant": ("OTRA TIENDA", False, "SUPERMERCADO CENTRAL"),
        "description": ("Mercado", False, None),
        "firefly_account": ("Bancolombia", False, "Nequi"),
        "occurred_at": ("2026-11-20T10:00:00-05:00", False, None),
        "amount": (25000, True, "12000.00"),
        "transaction_type": ("refund", False, "withdrawal"),
        "tags": ([" viaje ", "viaje", "casa"], False, ["casa"]),
        "void": (None, True, "created"),
        "split": ([{"amount": "5000", "category": "Transporte", "description": " taxi "}, {"amount": 7000.0, "category": "Alimentación"}], True, "12000.00"),
    }
    for field, (value, risky, old) in cases.items():
        response = await propose(client, batch_id, item.id, field, value)
        assert response.status_code == 201, (field, response.text)
        body = response.json()
        assert set(body) == {"id", "batch_id", "target_id", "field", "old_value", "new_value", "reason", "risky", "status", "error", "applied_at", "reverted_at"}
        assert body["risky"] is risky and body["status"] == "proposed" and body["batch_id"] == batch_id and body["target_id"] == item.id
        if field != "occurred_at":
            assert body["old_value"] == old, field
    assert (await propose(client, batch_id, item.id, "amount", 25000)).json()["new_value"] == "25000.00"
    assert (await propose(client, batch_id, item.id, "tags", [" viaje ", "viaje", "casa"])).json()["new_value"] == ["viaje", "casa"]
    split = (await propose(client, batch_id, item.id, "split", [{"amount": "5000", "category": "Transporte", "description": " taxi "}, {"amount": 7000.0, "category": "Alimentación"}])).json()
    assert split["new_value"] == [{"amount": "5000.00", "category": "Transporte", "description": "taxi"}, {"amount": "7000.00", "category": "Alimentación"}]


async def test_batch_holds_at_most_fifty_proposals(client, service):
    item = await make(service)
    batch_id = (await open_batch(client))["id"]
    for number in range(50):
        assert (await propose(client, batch_id, item.id, "description", f"nota {number}")).status_code == 201
    response = await propose(client, batch_id, item.id, "description", "una más")
    assert response.status_code == 422 and response.json()["code"] == "batch_limit"
    assert len((await client.get(f"{API}/{batch_id}", headers=ADMIN)).json()["proposals"]) == 50


async def test_batch_detail_shows_redacted_target(client, service):
    item = await make(service)
    batch_id = (await open_batch(client))["id"]
    await proposal_id(client, batch_id, item.id, "category", "Transporte")
    proposal = (await client.get(f"{API}/{batch_id}", headers=ADMIN)).json()["proposals"][0]
    target = proposal["target"]
    assert target["id"] == item.id and target["merchant"] == "SUPERMERCADO CENTRAL" and "tags" in target and "description" in target
    assert "1234567890123" not in target["raw_text_redacted"] and "raw_text" not in target


# --- apply and revert ------------------------------------------------------------------------------


async def test_apply_stores_the_old_value_and_revert_restores_it(client, service, clients):
    item = await make(service, category="Alimentación")
    item_id, ledger_id = item.id, item.firefly_transaction_id
    batch_id = (await open_batch(client))["id"]
    pid = await proposal_id(client, batch_id, item_id, "category", "Transporte")

    response = await apply(client, batch_id, [pid])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] == [pid] and body["failed"] == []
    assert body["batch"]["status"] == "applied"
    applied_at, deadline = datetime.fromisoformat(body["batch"]["applied_at"]), datetime.fromisoformat(body["batch"]["revert_deadline"])
    assert deadline - applied_at == timedelta(days=7)
    proposal = body["batch"]["proposals"][0]
    assert proposal["status"] == "applied" and proposal["old_value"] == "Alimentación" and proposal["new_value"] == "Transporte"
    assert proposal["applied_at"] and proposal["target"]["category"] == "Transporte"
    firefly = clients[0]
    firefly.update_transaction.assert_awaited_once()
    assert firefly.update_transaction.call_args.args[0] == ledger_id
    assert firefly.update_transaction.call_args.args[1]["transactions"][0]["category_name"] == "Transporte"

    reverted = await client.post(f"{API}/{batch_id}/revert", headers=ADMIN)
    assert reverted.status_code == 200, reverted.text
    result = reverted.json()
    assert result["reverted"] == [pid] and result["failed"] == [] and result["batch"]["status"] == "reverted"
    assert result["batch"]["proposals"][0]["status"] == "reverted" and result["batch"]["proposals"][0]["reverted_at"]
    assert firefly.update_transaction.await_count == 2
    assert firefly.update_transaction.call_args.args[1]["transactions"][0]["category_name"] == "Alimentación"
    assert (await service.inbox.get(item_id)).category == "Alimentación"

    again = await client.post(f"{API}/{batch_id}/revert", headers=ADMIN)
    assert again.status_code == 200 and again.json()["reverted"] == []
    assert firefly.update_transaction.await_count == 2


async def test_apply_is_idempotent(client, service, clients):
    item = await make(service)
    batch_id = (await open_batch(client))["id"]
    pid = await proposal_id(client, batch_id, item.id, "description", "Mercado")
    assert (await apply(client, batch_id, [pid])).json()["applied"] == [pid]
    second = await apply(client, batch_id, [pid])
    assert second.status_code == 200 and second.json()["applied"] == [] and second.json()["failed"] == []
    assert second.json()["batch"]["status"] == "applied"
    clients[0].update_transaction.assert_awaited_once()


async def test_a_risky_proposal_must_be_applied_alone(client, service, clients):
    item = await make(service)
    item_id = item.id
    batch_id = (await open_batch(client))["id"]
    risky = await proposal_id(client, batch_id, item_id, "amount", "25000")
    safe = await proposal_id(client, batch_id, item_id, "category", "Transporte")

    response = await apply(client, batch_id, [safe, risky])
    assert response.status_code == 422 and response.json()["code"] == "risky_requires_single"
    clients[0].update_transaction.assert_not_awaited()
    assert (await service.inbox.get(item_id)).amount == Decimal("12000.00")

    response = await apply(client, batch_id, [risky])
    assert response.status_code == 200 and response.json()["applied"] == [risky]
    assert response.json()["batch"]["status"] == "partially_applied"
    assert clients[0].update_transaction.call_args.args[1]["transactions"][0]["amount"] == "25000.00"
    assert (await service.inbox.get(item_id)).amount == Decimal("25000.00")
    assert (await apply(client, batch_id, [safe])).json()["batch"]["status"] == "applied"

    assert (await client.post(f"{API}/{batch_id}/revert", headers=ADMIN)).json()["reverted"] == [safe, risky]
    assert (await service.inbox.get(item_id)).amount == Decimal("12000.00")
    assert clients[0].update_transaction.call_args_list[-1].args[1]["transactions"][0]["amount"] == "12000.00"


async def test_applying_to_a_pending_item_approves_it_and_revert_restores_the_empty_value(client, service, clients):
    item = await make(service, status="needs_review", firefly_transaction_id=None, category=None, review_reason="merchant is not clean")
    item_id = item.id
    batch_id = (await open_batch(client))["id"]
    pid = await proposal_id(client, batch_id, item_id, "category", "Transporte")
    response = await apply(client, batch_id, [pid])
    assert response.json()["applied"] == [pid], response.text
    clients[0].create_transaction.assert_awaited_once()
    assert clients[0].create_transaction.call_args.args[0]["transactions"][0]["category_name"] == "Transporte"
    created = await service.inbox.get(item_id)
    assert created.status == "created" and created.category == "Transporte"

    reverted = await client.post(f"{API}/{batch_id}/revert", headers=ADMIN)
    assert reverted.json()["reverted"] == [pid], reverted.text
    payload = clients[0].update_transaction.call_args.args[1]["transactions"][0]
    assert payload["category_name"] is None
    assert (await service.inbox.get(item_id)).category is None


async def test_failed_proposals_are_reported_and_change_nothing(client, service, clients):
    item = await make(service, category="Alimentación")
    item_id = item.id
    batch_id = (await open_batch(client))["id"]
    unknown = await proposal_id(client, batch_id, item_id, "category", "Inexistente")
    response = await apply(client, batch_id, [unknown])
    assert response.status_code == 200
    body = response.json()
    assert body["applied"] == [] and body["failed"] == [{"id": unknown, "detail": "Unknown category"}]
    assert body["batch"]["status"] == "open"
    failed = body["batch"]["proposals"][0]
    assert failed["status"] == "failed" and failed["error"] == "Unknown category" and failed["applied_at"] is None
    clients[0].update_transaction.assert_not_awaited()

    # An upstream outage rolls the local change back and is retryable.
    clients[0].update_transaction.side_effect = FireflyError("Firefly temporarily unavailable (503)")
    outage = await proposal_id(client, batch_id, item_id, "category", "Transporte")
    response = await apply(client, batch_id, [outage])
    assert response.json()["failed"] == [{"id": outage, "detail": "Firefly temporarily unavailable (503)"}]
    assert (await service.inbox.get(item_id)).category == "Alimentación"
    clients[0].update_transaction.side_effect = None
    retry = await apply(client, batch_id, [outage])
    assert retry.json()["applied"] == [outage] and retry.json()["batch"]["status"] == "partially_applied"
    assert (await service.inbox.get(item_id)).category == "Transporte"


async def test_discarding_the_rest_closes_the_batch(client, service):
    first, second = await make(service), await make(service)
    batch_id = (await open_batch(client))["id"]
    keep = await proposal_id(client, batch_id, first.id, "description", "uno")
    drop = await proposal_id(client, batch_id, second.id, "description", "dos")
    assert (await apply(client, batch_id, [])).status_code == 422
    assert (await apply(client, batch_id, [99999])).json()["code"] == "validation_error"
    response = await apply(client, batch_id, [keep], discard_others=True)
    body = response.json()
    assert body["applied"] == [keep] and body["batch"]["status"] == "applied"
    assert {p["id"]: p["status"] for p in body["batch"]["proposals"]} == {keep: "applied", drop: "discarded"}
    assert (await propose(client, batch_id, first.id, "description", "tres")).json()["code"] == "conflict"

    other = (await open_batch(client))["id"]
    await proposal_id(client, other, first.id, "description", "cuatro")
    nothing = await apply(client, other, [], discard_others=True)
    assert nothing.json()["batch"]["status"] == "discarded"
    assert (await apply(client, other, [1])).status_code in {409, 422}


async def test_void_is_risky_and_revert_recreates_the_movement(client, service, clients):
    item = await make(service, firefly_transaction_id="tx-void", description="nota", tags=["casa"])
    batch_id = (await open_batch(client))["id"]
    pid = await proposal_id(client, batch_id, item.id, "void")
    item_id = item.id
    response = await apply(client, batch_id, [pid])
    assert response.json()["applied"] == [pid], response.text
    clients[0].delete_transaction.assert_awaited_once_with("tx-void")
    assert (await service.inbox.get(item_id)).status == "voided"

    reverted = await client.post(f"{API}/{batch_id}/revert", headers=ADMIN)
    assert reverted.json()["reverted"] == [pid], reverted.text
    clients[0].create_transaction.assert_awaited_once()
    split = clients[0].create_transaction.call_args.args[0]["transactions"][0]
    assert split["external_id"] == f"fa:manual:revert:{pid}" and split["amount"] == "12000.00"
    restored = await service.inbox.by_ref(f"manual:revert:{pid}")
    assert restored.status == "created" and restored.merchant == "SUPERMERCADO CENTRAL" and restored.category == "Alimentación"
    assert restored.description == "nota" and restored.tags == ["casa"]
    assert "casa" in clients[0].update_transaction.call_args.args[1]["transactions"][0]["tags"]
    assert (await service.inbox.get(item_id)).status == "voided"


async def test_void_of_an_unwritten_item_reverts_to_review(client, service, clients):
    item = await make(service, status="needs_review", firefly_transaction_id=None)
    item_id = item.id
    batch_id = (await open_batch(client))["id"]
    pid = await proposal_id(client, batch_id, item_id, "void")
    assert (await apply(client, batch_id, [pid])).json()["applied"] == [pid]
    clients[0].delete_transaction.assert_not_awaited()
    assert (await service.inbox.get(item_id)).status == "voided"
    assert (await client.post(f"{API}/{batch_id}/revert", headers=ADMIN)).json()["reverted"] == [pid]
    assert (await service.inbox.get(item_id)).status == "needs_review"
    clients[0].create_transaction.assert_not_awaited()


async def test_split_creates_deterministic_children_and_revert_restores_the_original(client, service, clients):
    item = await make(service, amount=Decimal("50000.00"), firefly_transaction_id="tx-orig")
    item_id = item.id
    batch_id = (await open_batch(client))["id"]
    wrong = await propose(client, batch_id, item_id, "split", [{"amount": "20000", "category": "Alimentación"}, {"amount": "20000", "category": "Transporte"}])
    assert wrong.status_code == 422 and wrong.json()["code"] == "validation_error"
    parts = [{"amount": "20000", "category": "Alimentación"}, {"amount": "30000", "category": "Transporte", "description": "gasolina"}]
    pid = await proposal_id(client, batch_id, item_id, "split", parts)

    response = await apply(client, batch_id, [pid])
    assert response.json()["applied"] == [pid], response.text
    firefly = clients[0]
    payloads = [call.args[0]["transactions"][0] for call in firefly.create_transaction.call_args_list]
    assert [(p["amount"], p["category_name"], p["external_id"]) for p in payloads] == [
        ("20000.00", "Alimentación", f"fa:manual:split:{pid}:1"),
        ("30000.00", "Transporte", f"fa:manual:split:{pid}:2"),
    ]
    assert payloads[1]["description"] == "gasolina"
    firefly.delete_transaction.assert_awaited_once_with("tx-orig")
    assert (await service.inbox.get(item_id)).status == "voided"
    children = [await service.inbox.by_ref(f"manual:split:{pid}:{n}") for n in (1, 2)]
    assert [c.status for c in children] == ["created", "created"] and [c.amount for c in children] == [Decimal("20000.00"), Decimal("30000.00")]

    assert (await apply(client, batch_id, [pid])).json()["applied"] == []
    assert firefly.create_transaction.await_count == 2

    reverted = await client.post(f"{API}/{batch_id}/revert", headers=ADMIN)
    assert reverted.json()["reverted"] == [pid], reverted.text
    assert firefly.create_transaction.await_count == 3
    assert firefly.create_transaction.call_args.args[0]["transactions"][0]["amount"] == "50000.00"
    assert firefly.delete_transaction.await_count == 3
    restored = await service.inbox.by_ref(f"manual:revert:{pid}")
    assert restored.status == "created" and restored.amount == Decimal("50000.00")
    assert [(await service.inbox.by_ref(f"manual:split:{pid}:{n}")).status for n in (1, 2)] == ["voided", "voided"]


async def test_split_with_an_unknown_category_changes_nothing(client, service, clients):
    item = await make(service, amount=Decimal("50000.00"))
    item_id = item.id
    batch_id = (await open_batch(client))["id"]
    pid = await proposal_id(client, batch_id, item_id, "split", [{"amount": "20000", "category": "Alimentación"}, {"amount": "30000", "category": "Inexistente"}])
    body = (await apply(client, batch_id, [pid])).json()
    assert body["applied"] == [] and body["failed"][0]["detail"] == "Unknown category: Inexistente"
    clients[0].create_transaction.assert_not_awaited()
    assert (await service.inbox.get(item_id)).status == "created"


async def test_revert_expires_after_seven_days(client, service, db, clients):
    item = await make(service)
    batch_id = (await open_batch(client))["id"]
    never = await client.post(f"{API}/{batch_id}/revert", headers=ADMIN)
    assert never.status_code == 409 and never.json()["code"] == "conflict"
    pid = await proposal_id(client, batch_id, item.id, "category", "Transporte")
    assert (await apply(client, batch_id, [pid])).json()["applied"] == [pid]
    batch = await db.get(ProposalBatch, batch_id)
    batch.applied_at = now_utc() - timedelta(days=8)
    await db.commit()
    expired = await client.post(f"{API}/{batch_id}/revert", headers=ADMIN)
    assert expired.status_code == 409 and expired.json()["code"] == "revert_expired"
    assert clients[0].update_transaction.await_count == 1

    batch.applied_at = now_utc() - timedelta(days=6, hours=23)
    await db.commit()
    assert (await client.post(f"{API}/{batch_id}/revert", headers=ADMIN)).json()["reverted"] == [pid]


# --- scopes ----------------------------------------------------------------------------------------


async def test_scopes(client, service, db, clients):
    reader = await device(db, "reader", ["read"])
    reviewer = await device(db, "reviewer", ["read", "review"])
    item = await make(service)
    item_id = item.id

    # `read` creates batches and proposals and reads them back, but cannot touch the ledger.
    batch_id = (await open_batch(client, headers=reader))["id"]
    pid = await proposal_id(client, batch_id, item_id, "category", "Transporte", headers=reader)
    assert (await client.get(f"{API}/{batch_id}", headers=reader)).status_code == 200
    assert (await client.get("/api/v1/audit", headers=reader)).status_code == 200
    for response in (await apply(client, batch_id, [pid], headers=reader), await client.post(f"{API}/{batch_id}/revert", headers=reader)):
        assert response.status_code == 403 and response.json()["code"] == "insufficient_scope"
    clients[0].update_transaction.assert_not_awaited()
    assert (await service.inbox.get(item_id)).category == "Alimentación"

    # The ingest-only phone key reads nothing.
    for response in (await client.post(API, headers=PHONE, json={}), await client.get(f"{API}/{batch_id}", headers=PHONE), await client.get("/api/v1/audit", headers=PHONE)):
        assert response.status_code == 403

    assert (await apply(client, batch_id, [pid], headers=reviewer)).json()["applied"] == [pid]
    entry = next(e for e in (await client.get("/api/v1/audit", headers=ADMIN, params={"action": "assistant.apply"})).json()["items"])
    assert entry["actor"] == "reviewer" and entry["device_id"] is not None


# --- audit -----------------------------------------------------------------------------------------


async def test_audit_trail_and_endpoint(client, service):
    item = await make(service)
    batch_id = (await open_batch(client, instruction="top secret instruction"))["id"]
    pid = await proposal_id(client, batch_id, item.id, "category", "Transporte")
    await apply(client, batch_id, [pid])
    await client.post(f"{API}/{batch_id}/revert", headers=ADMIN)
    assert {"assistant.batch_created", "assistant.proposal_created", "assistant.apply", "assistant.revert", "inbox.edit"} <= await audit_actions(client)

    listing = (await client.get("/api/v1/audit", headers=ADMIN, params={"limit": 500})).json()
    assert set(listing) == {"items", "total"} and listing["total"] == len(listing["items"])
    assert set(listing["items"][0]) == {"id", "at", "device_id", "actor", "action", "target_type", "target_id", "detail"}
    assert [e["id"] for e in listing["items"]] == sorted((e["id"] for e in listing["items"]), reverse=True)
    dumped = json.dumps(listing)
    assert "top secret instruction" not in dumped and "1234567890123" not in dumped

    only = (await client.get("/api/v1/audit", headers=ADMIN, params={"action": "assistant.apply"})).json()
    assert only["total"] == 1 and only["items"][0]["target_id"] == str(batch_id)
    assert only["items"][0]["detail"] == {"applied": [pid], "failed": [], "discarded": []}
    created = (await client.get("/api/v1/audit", headers=ADMIN, params={"action": "assistant.batch_created"})).json()["items"][0]
    assert created["detail"]["instruction_length"] == len("top secret instruction")

    page = (await client.get("/api/v1/audit", headers=ADMIN, params={"limit": 2, "offset": 1})).json()
    assert len(page["items"]) == 2 and page["total"] == listing["total"]
    future = (now_utc() + timedelta(hours=1)).isoformat()
    assert (await client.get("/api/v1/audit", headers=ADMIN, params={"since": future})).json() == {"items": [], "total": 0}
    assert (await client.get("/api/v1/audit", headers=ADMIN, params={"since": (now_utc() - timedelta(hours=1)).isoformat()})).json()["total"] == listing["total"]
