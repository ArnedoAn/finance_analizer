"""Assistant bulk proposals, fast and idempotent apply, and rules that add tags (Firefly simulated).

The owner's request, «cambia a Transporte todo lo de DiDi y crea la regla que además pone la etiqueta transporte»,
becomes many proposals in ONE call (``/proposals/bulk``) and one ``/apply``.
"""
import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.db.models_rules import Rule
from app.services import processor as pipeline
from tests.test_assistant_actions_v4 import World, act, batch_state
from tests.test_assistant_v4 import ADMIN, API, apply, make, open_batch
from tests.test_catalog_v4 import email_item


@pytest.fixture
def world(clients):
    return World(clients[0])


def entry(field, target=None, value=None, **extra):
    return {"field": field, "target_ref": target, "new_value": value, "reason": "porque sí", **extra}


async def bulk(client, batch_id, proposals, status=200):
    response = await client.post(f"{API}/{batch_id}/proposals/bulk", headers=ADMIN, json={"proposals": proposals})
    assert response.status_code == status, response.text
    return response.json()


# --- bulk proposals ------------------------------------------------------------------------------------------------------


async def test_bulk_creates_the_valid_ones_in_order_and_reports_the_invalid_one(client, service, world):
    item = await make(service)
    movement = world.movement()
    batch_id = (await open_batch(client))["id"]
    body = await bulk(client, batch_id, [
        {"target_id": item.id, "field": "category", "new_value": "Ocio", "reason": "porque sí"},
        entry("ledger_edit", "9999", {"category": "Ocio"}),
        entry("ledger_edit", movement, {"category": "Ocio"}),
        entry("create_category", None, "Transporte"),
    ])
    assert (body["created"], body["failed"]) == (3, 1)
    assert [row["ok"] for row in body["items"]] == [True, False, True, True]
    assert body["items"][1]["error"] == {"detail": "No existe ese movimiento.", "code": "not_found"}
    assert [row["proposal"]["field"] for row in body["items"] if row["ok"]] == ["category", "ledger_edit", "create_category"]
    proposals = (await batch_state(client, batch_id))["proposals"]
    assert [proposal["id"] for proposal in proposals] == [row["proposal"]["id"] for row in body["items"] if row["ok"]]
    # The session stays usable after the failure: a single proposal still works.
    await act(client, batch_id, "create_category", None, "Viajes")


async def test_bulk_isolates_malformed_elements_and_validates_like_the_single_endpoint(client, world):
    batch_id = (await open_batch(client))["id"]
    body = await bulk(client, batch_id, [
        {"field": "create_category", "new_value": "Transporte"},  # no reason
        "no es un objeto",
        entry("create_category", None, "Con $ raro"),
        {"field": "category", "new_value": "Ocio", "reason": "x"},  # inbox edit without target_id
        entry("create_category", None, "Transporte"),
        entry("create_category", None, "transporte"),  # the batch already has it
    ])
    assert [row["ok"] for row in body["items"]] == [False, False, False, False, True, False]
    assert [row["error"]["code"] for row in body["items"] if not row["ok"]] == ["validation_error", "validation_error", "validation_error", "validation_error", "conflict"]
    assert (body["created"], body["failed"]) == (1, 5)
    assert all(set(row["error"]) == {"detail", "code"} for row in body["items"] if not row["ok"])


async def test_bulk_size_limits_and_the_batch_cap(client, world):
    batch_id = (await open_batch(client))["id"]
    await bulk(client, batch_id, [], status=422)
    await bulk(client, batch_id, [entry("create_category", None, f"C{n}") for n in range(51)], status=422)
    first = await bulk(client, batch_id, [entry("create_category", None, f"Categoría {n}") for n in range(49)])
    assert first["created"] == 49
    second = await bulk(client, batch_id, [entry("create_category", None, "Una"), entry("create_category", None, "Otra")])
    assert [row["ok"] for row in second["items"]] == [True, False] and second["items"][1]["error"]["code"] == "batch_limit"


async def test_bulk_on_a_closed_or_unknown_batch_is_a_coded_error(client, world):
    batch_id = (await open_batch(client))["id"]
    await act(client, batch_id, "create_category", None, "Temprano")
    await client.post(f"{API}/{batch_id}/apply", headers=ADMIN, json={"approved_ids": [], "discard_others": True})
    closed = await bulk(client, batch_id, [entry("create_category", None, "Tarde")], status=409)
    assert closed["code"] == "conflict"
    missing = await bulk(client, 999, [entry("create_category", None, "Tarde")], status=404)
    assert missing["code"] == "not_found"


async def test_bulk_needs_the_same_scope_as_single_proposals(client, db, world):
    from tests.test_assistant_v4 import device

    phone = await device(db, "reader", ["read"])
    batch_id = (await open_batch(client))["id"]
    response = await client.post(f"{API}/{batch_id}/proposals/bulk", headers=phone, json={"proposals": [entry("create_category", None, "Viajes")]})
    assert response.status_code == 200
    none = await device(db, "ingest", ["ingest"])
    denied = await client.post(f"{API}/{batch_id}/proposals/bulk", headers=none, json={"proposals": [entry("create_category", None, "Otra")]})
    assert denied.status_code == 403


# --- apply -------------------------------------------------------------------------------------------------------------


async def test_fifty_ledger_edits_apply_without_blocking(client, world):
    refs = [world.movement(f"DiDi {number}", hours=2 + number % 20) for number in range(50)]
    batch_id = (await open_batch(client, hours=72))["id"]
    created = await bulk(client, batch_id, [entry("ledger_edit", ref, {"category": "Ocio"}) for ref in refs])
    assert (created["created"], created["failed"]) == (50, 0)
    ids = [row["proposal"]["id"] for row in created["items"]]

    response = await asyncio.wait_for(apply(client, batch_id, ids), timeout=60)
    body = response.json()
    assert response.status_code == 200 and body["failed"] == [] and body["applied"] == ids
    assert body["batch"]["status"] == "applied" and {p["status"] for p in body["batch"]["proposals"]} == {"applied"}
    assert all(world.category_of(ref) == "Ocio" for ref in refs)
    assert len(world.ledger.writes("update")) == 50


async def test_repeating_apply_with_the_same_idempotency_key_does_not_reapply(client, world):
    refs = [world.movement(f"DiDi {number}", hours=2 + number) for number in range(3)]
    batch_id = (await open_batch(client))["id"]
    created = await bulk(client, batch_id, [entry("ledger_edit", ref, {"category": "Ocio"}) for ref in refs])
    ids = [row["proposal"]["id"] for row in created["items"]]
    headers = {**ADMIN, "Idempotency-Key": "apply-7c1f"}

    first = (await apply(client, batch_id, ids, headers=headers)).json()
    writes = len(world.ledger.writes("update"))
    assert first["applied"] == ids and writes == 3
    again = (await apply(client, batch_id, ids, headers=headers)).json()
    assert again["applied"] == ids and again["failed"] == []  # the same answer the first call gave
    assert len(world.ledger.writes("update")) == writes
    other = (await apply(client, batch_id, ids, headers={**ADMIN, "Idempotency-Key": "otra"})).json()
    assert other["applied"] == [] and other["failed"] == [] and len(world.ledger.writes("update")) == writes
    plain = (await apply(client, batch_id, ids)).json()
    assert plain["applied"] == [] and len(world.ledger.writes("update")) == writes
    assert {p["status"] for p in plain["batch"]["proposals"]} == {"applied"}


async def test_a_retry_under_the_same_key_finishes_what_failed_and_reports_everything(client, service, world):
    first, second = world.movement(), world.movement(hours=3)
    batch_id = (await open_batch(client))["id"]
    good = await act(client, batch_id, "ledger_edit", first, {"category": "Ocio"})
    late = await act(client, batch_id, "ledger_edit", second, {"category": "Transporte"})  # not in Firefly yet
    headers = {**ADMIN, "Idempotency-Key": "retry-1"}
    body = (await apply(client, batch_id, [good["id"], late["id"]], headers=headers)).json()
    assert body["applied"] == [good["id"]] and [row["id"] for row in body["failed"]] == [late["id"]]
    world.categories.append("Transporte")
    service.invalidate_categories()
    body = (await apply(client, batch_id, [good["id"], late["id"]], headers=headers)).json()
    assert body["applied"] == [good["id"], late["id"]] and body["failed"] == []
    assert len(world.ledger.writes("update")) == 2


# --- rules with tags ---------------------------------------------------------------------------------------------------------


async def test_rule_tags_are_added_when_the_rule_matches_a_processed_movement(client, service, clients, db, monkeypatch, extraction):
    monkeypatch.setattr(pipeline, "parse", lambda *args, **kwargs: replace(extraction, merchant="DIDI CO RIDE"))
    batch_id = (await open_batch(client))["id"]
    rule = await act(client, batch_id, "create_rule", None, {"match": "merchant_contains", "pattern": "didi", "category": "transporte", "tags": [" transporte ", "transporte"]})
    assert rule["new_value"] == {"match": "merchant_contains", "pattern": "didi", "category": "transporte", "tags": ["transporte"]}
    assert rule["subject"].endswith("· transporte")
    body = (await apply(client, batch_id, [rule["id"]])).json()
    assert body["failed"] == [] and body["applied"] == [rule["id"]]
    stored = (await db.scalars(select(Rule))).one()
    assert (stored.category, stored.tags) == ("Transporte", ["transporte"])

    row = await service.process(await email_item(service, "mail:didi"))
    assert row.status == "created" and row.category == "Transporte" and row.tags == ["transporte"]
    split = clients[0].create_transaction.call_args.args[0]["transactions"][0]
    assert split["category_name"] == "Transporte" and "transporte" in split["tags"]

    other = await service.process(await email_item(service, "mail:didi-2"))
    assert other.tags == ["transporte"]  # never repeated, never doubled


async def test_a_rule_without_tags_leaves_the_tags_alone(client, service, db, monkeypatch, extraction):
    monkeypatch.setattr(pipeline, "parse", lambda *args, **kwargs: replace(extraction, merchant="DIDI CO RIDE"))
    created = await client.post("/api/v1/rules", headers=ADMIN, json={"match": "merchant_contains", "pattern": "DIDI", "category": "Transporte"})
    assert created.status_code == 201 and created.json()["tags"] == []
    row = await service.process(await email_item(service, "mail:didi-plain"))
    assert row.category == "Transporte" and not row.tags


async def test_rules_endpoints_accept_and_return_tags(client):
    created = await client.post("/api/v1/rules", headers=ADMIN, json={"match": "merchant_contains", "pattern": "didi", "category": "transporte", "tags": [" viaje ", "viaje", "transporte"]})
    assert created.status_code == 201 and created.json()["tags"] == ["viaje", "transporte"]
    listing = (await client.get("/api/v1/rules", headers=ADMIN)).json()
    assert [rule["tags"] for rule in listing["items"]] == [["viaje", "transporte"]]
    # Posting the same rule again without tags keeps them; with a list it replaces them.
    kept = await client.post("/api/v1/rules", headers=ADMIN, json={"match": "merchant_contains", "pattern": "didi", "category": "Transporte"})
    assert kept.json()["tags"] == ["viaje", "transporte"]
    replaced = await client.post("/api/v1/rules", headers=ADMIN, json={"match": "merchant_contains", "pattern": "didi", "category": "Transporte", "tags": ["taxi"]})
    assert replaced.json()["tags"] == ["taxi"]
    for tags in (["x" * 41], [""], ["a"] * 11, [3]):
        assert (await client.post("/api/v1/rules", headers=ADMIN, json={"match": "text_contains", "pattern": "uber", "category": "Transporte", "tags": tags})).status_code == 422, tags


async def test_rule_tags_are_validated_when_proposing(client):
    batch_id = (await open_batch(client))["id"]
    for tags in (["x" * 41], [""], ["a"] * 11, "transporte"):
        await act(client, batch_id, "create_rule", None, {"match": "merchant_contains", "pattern": "didi", "category": "Transporte", "tags": tags}, status=422)


# --- the owner's request, end to end ---------------------------------------------------------------------------------------


async def test_the_owners_request_as_bulk_proposals_applies_everything(client, service, world, db):
    refs = [world.movement("DiDi viaje", hours=hours) for hours in (2, 3, 4)]
    other = world.movement("Mercado", hours=5, destination="2")
    batch_id = (await open_batch(client))["id"]
    body = await bulk(client, batch_id, [
        entry("create_rule", None, {"match": "merchant_contains", "pattern": "DIDI", "category": "Transporte", "tags": ["transporte"]}),
        *[entry("ledger_edit", ref, {"category": "Transporte"}) for ref in refs],
        entry("create_category", None, "Transporte"),
    ])
    assert (body["created"], body["failed"]) == (5, 0)
    ids = [row["proposal"]["id"] for row in body["items"]]

    response = (await apply(client, batch_id, ids, headers={**ADMIN, "Idempotency-Key": "owner-1"})).json()
    assert response["failed"] == [] and sorted(response["applied"]) == sorted(ids)
    assert response["applied"][0] == ids[-1]  # the creation runs first, whatever the order of the ids
    assert response["batch"]["status"] == "applied"
    assert "Transporte" in world.categories
    assert [world.category_of(ref) for ref in refs] == ["Transporte"] * 3 and world.category_of(other) == "Alimentación"
    rule = (await db.scalars(select(Rule))).one()
    assert (rule.match, rule.pattern, rule.category, rule.tags) == ("merchant_contains", "DIDI", "Transporte", ["transporte"])
    listed = (await client.get("/api/v1/rules", headers=ADMIN)).json()["items"]
    assert [(item["pattern"], item["category"], item["tags"]) for item in listed] == [("DIDI", "Transporte", ["transporte"])]
